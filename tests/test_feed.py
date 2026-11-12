"""#22: the upcoming feed — one call with every occurrence of the next days, tasks and needs-a-look."""

from datetime import timedelta

import pytest
from conftest import TZ, dt

from maestro import feed
from maestro.config import Config
from maestro.escalation import build_stages
from maestro.models import Delivery, Item, Kind
from maestro.notifier import CHANNELS
from maestro.quiet import parse_quiet_hours
from maestro.storage import SQLiteStore

NOW = dt(2026, 10, 27, 8, 0)  # Tuesday 27 Oct 2026; US clocks go back on Sunday 1 Nov
QUIET = parse_quiet_hours("22:00-07:00")


@pytest.fixture
def store():
    with SQLiteStore(":memory:", tz=TZ) as s:
        yield s


def reminder(store, text, due, **extra):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=NOW - timedelta(days=1),
                          due_at=due, source_text=text, **extra))


def task(store, text, created=None):
    return store.add(Item(text=text, kind=Kind.TASK, created_at=created or NOW, source_text=text))


def rows(built):
    return [(o.item.text, o.at, o.state, o.acts) for o in built.upcoming]


# --- the window ------------------------------------------------------------------------------


def test_the_window_is_whole_days_in_the_users_timezone():
    assert feed.window_end(NOW, 7) == dt(2026, 11, 3, 0, 0)  # today and the six days after it
    # Across the DST change: still midnight on the calendar day, not 23:00 or 01:00.
    assert feed.window_end(dt(2026, 10, 31, 8, 0), 3) == dt(2026, 11, 3, 0, 0)


def test_reminders_past_the_window_are_counted_not_listed(store):
    reminder(store, "In the window", dt(2026, 11, 2, 9, 0))
    reminder(store, "Past it", dt(2026, 11, 3, 0, 0))  # the exclusive end
    reminder(store, "Far off", dt(2027, 1, 5, 9, 0))
    built = feed.build(store, NOW)
    assert [o.item.text for o in built.upcoming] == ["In the window"]
    assert built.later == 2
    assert feed.build(store, NOW, days=31).later == 1


def test_days_are_clamped_to_a_sane_range(store):
    assert feed.build(store, NOW, days=0).until == dt(2026, 10, 28, 0, 0)
    assert feed.build(store, NOW, days=999).until == feed.window_end(NOW, feed.MAX_DAYS)


# --- what goes where -------------------------------------------------------------------------


def test_each_kind_lands_in_its_part_of_the_feed(store):
    reminder(store, "Call mom", dt(2026, 10, 27, 17, 0))
    task(store, "Renew the passport", created=NOW - timedelta(days=2))
    task(store, "Buy stamps", created=NOW - timedelta(days=1))
    store.add(Item(text="The gate code is 4321", kind=Kind.NOTE, created_at=NOW, source_text="x"))
    store.add(Item(text="Call the plumber sometime next week", kind=Kind.TASK, created_at=NOW,
                   source_text="x", gap="when"))
    done = reminder(store, "Done already", dt(2026, 10, 27, 20, 0))
    store.complete(done.id, via="cli")

    built = feed.build(store, NOW)
    assert [o.item.text for o in built.upcoming] == ["Call mom"]
    assert [i.text for i in built.anytime] == ["Renew the passport", "Buy stamps"]  # oldest first
    assert [i.text for i in built.needs_look] == ["Call the plumber sometime next week"]
    # notes live in search, done is history, and a gapped item is never also upcoming
    assert built.later == 0


def test_upcoming_is_ordered_by_when_each_row_goes_out(store):
    reminder(store, "Later today", dt(2026, 10, 27, 17, 0))
    reminder(store, "Tomorrow", dt(2026, 10, 28, 9, 0))
    reminder(store, "Soon", dt(2026, 10, 27, 9, 0))
    assert [o.item.text for o in feed.build(store, NOW).upcoming] == ["Soon", "Later today", "Tomorrow"]


# --- states ----------------------------------------------------------------------------------


def test_states_follow_the_scheduler(store):
    reminder(store, "Overdue", dt(2026, 10, 27, 7, 30))  # due, not yet sent
    reminder(store, "Scheduled", dt(2026, 10, 27, 17, 0))
    reminder(store, "Held", dt(2026, 10, 27, 23, 0))  # in quiet hours
    reminder(store, "!Urgent at night", dt(2026, 10, 27, 23, 30), urgent=True)
    built = feed.build(store, NOW, quiet=QUIET)
    by_text = {o.item.text: o for o in built.upcoming}
    assert by_text["Overdue"].state == "overdue"
    assert by_text["Scheduled"].state == "scheduled"
    assert (by_text["Held"].state, by_text["Held"].held_until) == ("held", dt(2026, 10, 28, 7, 0))
    assert by_text["!Urgent at night"].state == "scheduled"  # urgent ignores quiet hours
    assert feed.build(store, NOW).upcoming[2].state == "scheduled"  # no quiet hours configured


def test_a_delivered_reminder_waits_for_an_answer_at_the_time_it_went_out(store):
    item = reminder(store, "Take pills", dt(2026, 10, 27, 7, 0))
    store.record_delivery(item.id, item.due_at,
                          Delivery(channel="console", attempts=1, delivered_at=dt(2026, 10, 27, 7, 0)))
    (row,) = feed.build(store, NOW).upcoming
    assert (row.at, row.state, row.acts) == (dt(2026, 10, 27, 7, 0), "awaiting", True)
    assert row.escalation_label is None  # no stages configured


def test_a_snoozed_one_off_is_at_its_snoozed_time(store):
    item = reminder(store, "Call back", dt(2026, 10, 27, 7, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 30), via="cli", now=NOW)
    (row,) = feed.build(store, NOW).upcoming
    # A one-off moved by snooze simply has a new due time (storage/logic.py); it reads as scheduled.
    assert (row.at, row.state) == (dt(2026, 10, 27, 9, 30), "scheduled")


# --- recurring series ------------------------------------------------------------------------


def test_a_series_is_expanded_to_its_occurrences_and_only_the_first_acts(store):
    reminder(store, "Water the plants", dt(2026, 10, 27, 18, 0), recurrence="daily")
    built = feed.build(store, NOW, days=3)
    assert rows(built) == [
        ("Water the plants", dt(2026, 10, 27, 18, 0), "scheduled", True),
        ("Water the plants", dt(2026, 10, 28, 18, 0), "scheduled", False),
        ("Water the plants", dt(2026, 10, 29, 18, 0), "scheduled", False),
    ]


def test_a_series_keeps_its_wall_clock_time_across_the_dst_change(store):
    reminder(store, "Pills", dt(2026, 10, 31, 8, 0), recurrence="daily")
    built = feed.build(store, dt(2026, 10, 31, 7, 0), days=3)
    assert [o.at for o in built.upcoming] == [dt(2026, 10, 31, 8, 0), dt(2026, 11, 1, 8, 0),
                                              dt(2026, 11, 2, 8, 0)]
    assert {o.at.utcoffset() for o in built.upcoming} == {timedelta(hours=-4), timedelta(hours=-5)}


def test_an_interval_series_shows_one_row_per_day(store):
    reminder(store, "Stretch", dt(2026, 10, 27, 9, 0), recurrence="interval:120")
    built = feed.build(store, NOW, days=2)
    assert [o.at for o in built.upcoming] == [dt(2026, 10, 27, 9, 0), dt(2026, 10, 28, 1, 0)]
    assert [o.acts for o in built.upcoming] == [True, False]


def test_a_snoozed_series_shows_the_re_send_first_then_its_own_times(store):
    item = reminder(store, "Stretch", dt(2026, 10, 27, 7, 30), recurrence="daily")
    store.snooze(item.id, dt(2026, 10, 27, 9, 0), via="cli", now=NOW)  # this morning's, pushed back
    built = feed.build(store, NOW, days=3)
    assert rows(built) == [
        ("Stretch", dt(2026, 10, 27, 9, 0), "snoozed", True),
        ("Stretch", dt(2026, 10, 28, 7, 30), "scheduled", False),
        ("Stretch", dt(2026, 10, 29, 7, 30), "scheduled", False),
    ]


def test_a_delivered_series_shows_the_awaiting_occurrence_then_the_next(store):
    item = reminder(store, "Pills", dt(2026, 10, 27, 7, 0), recurrence="daily")
    store.record_delivery(item.id, item.due_at,
                          Delivery(channel="console", attempts=1, delivered_at=dt(2026, 10, 27, 7, 0)))
    # the scheduler moves the series on once it has sent
    store.reschedule(item.id, dt(2026, 10, 28, 7, 0))
    built = feed.build(store, NOW, days=2)
    assert built.upcoming[0].state == "awaiting" and built.upcoming[0].acts
    assert all(not o.acts for o in built.upcoming[1:])
    assert all(o.at > dt(2026, 10, 27, 7, 0) for o in built.upcoming[1:])


# --- escalation ------------------------------------------------------------------------------


@pytest.fixture
def channels(monkeypatch):
    class Quiet:
        def send(self, *_a, **_k):
            return None

    for name in ("sms", "call"):
        monkeypatch.setitem(CHANNELS, name, lambda _config: Quiet())


def test_the_label_names_the_next_stage_still_to_go_out(store, channels):
    stages = build_stages(Config(escalation="10m=sms, 30m=call+sms"))
    item = reminder(store, "Take pills", dt(2026, 10, 27, 7, 0))
    first = dt(2026, 10, 27, 7, 0)
    store.record_delivery(item.id, first, Delivery(channel="console", attempts=1, delivered_at=first))
    (row,) = feed.build(store, dt(2026, 10, 27, 7, 5), escalation=stages).upcoming
    assert row.escalation_label == "sms at 07:10"
    # once the first stage went out, the label moves on to the next
    store.record_delivery(item.id, first, Delivery(channel="escalation1:sms", attempts=1,
                                                   delivered_at=dt(2026, 10, 27, 7, 10)))
    (row,) = feed.build(store, dt(2026, 10, 27, 7, 12), escalation=stages).upcoming
    assert row.escalation_label == "call, sms at 07:30"


def test_escalation_is_timed_from_the_end_of_quiet_hours(store, channels):
    stages = build_stages(Config(escalation="10m=sms"))
    item = reminder(store, "Take pills", dt(2026, 10, 27, 6, 50))
    first = dt(2026, 10, 27, 6, 50)  # delivered inside quiet hours (an urgent-ish edge the scheduler allows)
    store.record_delivery(item.id, first, Delivery(channel="console", attempts=1, delivered_at=first))
    (row,) = feed.build(store, dt(2026, 10, 27, 7, 2), quiet=QUIET, escalation=stages).upcoming
    assert row.escalation_label == "sms at 07:10"  # 07:00 + 10m, not 06:50 + 10m


# --- the API ---------------------------------------------------------------------------------


def test_the_api_serves_the_feed_in_one_call(store):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    reminder(store, "Call mom", dt(2099, 1, 1, 17, 0))  # past any 7-day window: counted as later
    task(store, "Renew the passport")
    store.add(Item(text="Call the plumber sometime", kind=Kind.TASK, created_at=NOW,
                   source_text="x", gap="when"))
    with TestClient(create_app(store, api_token="t0ken")) as client:
        auth = {"Authorization": "Bearer t0ken"}
        assert client.get("/feed").status_code == 401
        body = client.get("/feed", headers=auth).json()
        assert set(body) == {"now", "until", "timezone", "needs_look", "upcoming", "anytime", "later"}
        assert body["timezone"] == TZ.key
        assert body["upcoming"] == [] and body["later"] == 1
        assert [i["text"] for i in body["anytime"]] == ["Renew the passport"]
        assert [(i["text"], i["gap"]) for i in body["needs_look"]] == [("Call the plumber sometime", "when")]
        assert client.get("/feed?days=0", headers=auth).status_code == 422
        assert client.get("/feed?days=40", headers=auth).status_code == 422
        assert client.get("/feed?days=14", headers=auth).status_code == 200


def test_the_api_feed_row_has_the_item_and_its_state(store):
    pytest.importorskip("fastapi")
    from datetime import datetime

    from fastapi.testclient import TestClient

    from maestro.api import create_app

    soon = (datetime.now(TZ) + timedelta(hours=1)).replace(microsecond=0)
    reminder(store, "Call mom", soon)
    with TestClient(create_app(store, api_token="t0ken")) as client:
        (row,) = client.get("/feed", headers={"Authorization": "Bearer t0ken"}).json()["upcoming"]
        assert row["item"]["text"] == "Call mom" and row["state"] == "scheduled" and row["acts"] is True
        assert row["held_until"] is None and row["escalation_label"] is None
        assert datetime.fromisoformat(row["at"]) == soon
