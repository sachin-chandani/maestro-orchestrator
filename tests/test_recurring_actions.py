"""#80: done and snooze on a recurring reminder are about one occurrence, not the series (ADR 0009)."""

from datetime import timedelta

import pytest
from conftest import dt

from maestro import commands
from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.parser import parse
from maestro.scheduler import fire_due
from maestro.store import Store


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append((item.text, item.due_at))


@pytest.fixture
def store():
    return Store()


def daily(store):
    return store.add(parse("take pills every day at 9am", now=dt(2026, 10, 27, 7, 0)))


def tick(store, now, push=None):
    fire_due(store, MultiNotifier({"push": push or Recorder()}), now=now)


def acks(store):
    return [(r["occurrence"], r["via"]) for r in store.conn.execute("SELECT * FROM acknowledgements")]


def test_done_after_a_reminder_acknowledges_it_and_the_series_goes_on(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.complete(item.id, via="twilio_sms", now=dt(2026, 10, 27, 9, 5))
    after = store.get(item.id)
    assert after.status == Status.PENDING and after.due_at == dt(2026, 10, 28, 9, 0)
    assert acks(store) == [("2026-10-27T13:00:00+00:00", "twilio_sms")]


def test_done_before_the_reminder_skips_that_occurrence(store):
    item = daily(store)
    store.complete(item.id, via="web", now=dt(2026, 10, 27, 8, 30))  # took them early
    assert store.get(item.id).due_at == dt(2026, 10, 28, 9, 0)
    tick(store, dt(2026, 10, 27, 9, 0), push := Recorder())
    assert push.sent == []
    assert acks(store) == []  # nothing was delivered, so nothing to acknowledge


def test_snooze_resends_the_occurrence_without_moving_the_series(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 15), via="telegram", now=dt(2026, 10, 27, 9, 5))
    snoozed = store.get(item.id)
    assert (snoozed.snoozed_until, snoozed.due_at) == (dt(2026, 10, 27, 9, 15), dt(2026, 10, 28, 9, 0))

    push = Recorder()
    tick(store, dt(2026, 10, 27, 9, 14), push)
    assert push.sent == []
    tick(store, dt(2026, 10, 27, 9, 15), push)
    assert push.sent == [("Take pills", dt(2026, 10, 27, 9, 15))]  # channels see the snoozed time
    assert store.get(item.id).snoozed_until is None
    tick(store, dt(2026, 10, 28, 9, 0), push)
    assert push.sent[-1] == ("Take pills", dt(2026, 10, 28, 9, 0))  # still 09:00, no drift to 09:15
    assert store.get(item.id).due_at == dt(2026, 10, 29, 9, 0)


def test_the_snoozed_occurrence_needs_its_own_acknowledgement(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 15), via="api", now=dt(2026, 10, 27, 9, 5))
    tick(store, dt(2026, 10, 27, 9, 15))
    assert [occ for _, occ in store.unacknowledged()] == [dt(2026, 10, 27, 9, 15)]
    store.complete(item.id, via="api", now=dt(2026, 10, 27, 9, 20))
    assert store.unacknowledged() == []
    assert store.get(item.id).status == Status.PENDING


def test_snoozing_past_the_next_occurrence_skips_the_ones_in_between(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 29, 12, 0), via="api", now=dt(2026, 10, 27, 9, 5))  # "snooze 2d"
    after = store.get(item.id)
    assert (after.snoozed_until, after.due_at) == (dt(2026, 10, 29, 12, 0), dt(2026, 10, 30, 9, 0))
    push = Recorder()
    for day in (28, 29):
        tick(store, dt(2026, 10, day, 9, 0), push)
    assert push.sent == []


def test_snoozing_an_upcoming_occurrence_moves_just_that_one(store):
    item = daily(store)
    store.snooze(item.id, dt(2026, 10, 27, 9, 30), via="web", now=dt(2026, 10, 27, 8, 30))
    after = store.get(item.id)
    assert (after.snoozed_until, after.due_at) == (dt(2026, 10, 27, 9, 30), dt(2026, 10, 28, 9, 0))
    push = Recorder()
    tick(store, dt(2026, 10, 27, 9, 0), push)
    tick(store, dt(2026, 10, 27, 9, 30), push)
    assert push.sent == [("Take pills", dt(2026, 10, 27, 9, 30))]


def test_done_drops_a_pending_snooze(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 15), via="api", now=dt(2026, 10, 27, 9, 5))
    store.complete(item.id, via="api", now=dt(2026, 10, 27, 9, 10))
    tick(store, dt(2026, 10, 27, 9, 15), push := Recorder())
    assert push.sent == []
    assert store.get(item.id).due_at == dt(2026, 10, 28, 9, 0)


def test_stop_ends_the_series(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 15), via="api", now=dt(2026, 10, 27, 9, 5))
    store.stop(item.id, via="cli", now=dt(2026, 10, 27, 9, 6))
    stopped = store.get(item.id)
    assert stopped.status == Status.DONE and stopped.snoozed_until is None
    tick(store, dt(2026, 10, 28, 9, 0), push := Recorder())
    assert push.sent == []


def test_one_off_reminders_are_unchanged(store):
    item = store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 27, 8, 0),
                          due_at=dt(2026, 10, 27, 9, 0)))
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 10, 0), via="api", now=dt(2026, 10, 27, 9, 5))
    snoozed = store.get(item.id)
    assert (snoozed.due_at, snoozed.snoozed_until) == (dt(2026, 10, 27, 10, 0), None)
    store.complete(item.id, via="api", now=dt(2026, 10, 27, 9, 6))
    assert store.get(item.id).status == Status.DONE


def test_chat_replies(store):
    item = daily(store)
    reply = commands.act(store, "done", [str(item.id)], via="telegram")
    assert reply.startswith("✅ Done for now: Take pills. Next: ")
    assert commands.act(store, "stop", [str(item.id)], via="telegram") == "⏹ Stopped: Take pills"
    assert store.get(item.id).status == Status.DONE
    # Ending a series is never inferred from "the last reminder".
    assert commands.parse_command("stop") == commands.Command("stop")
    assert commands.run(store, commands.Command("stop"), channel="twilio_sms").startswith("Which one?")


def test_listings_show_the_snoozed_time(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 15), via="api", now=dt(2026, 10, 27, 9, 5))
    assert commands.when(store.get(item.id)) == "Tue 27 Oct 09:15 (snoozed), then Wed 28 Oct 09:00, daily"


def test_api_and_cli_stop(tmp_path, monkeypatch, capsys):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app
    from maestro.cli import main

    db = tmp_path / "m.db"
    with Store(db) as s:
        first, second = daily(s), daily(s)
        with TestClient(create_app(s)) as client:
            body = client.post(f"/items/{first.id}/stop", headers={"X-Maestro-Client": "web"}).json()
            assert body["status"] == "done" and body["snoozed_until"] is None
            assert client.post("/items/999/stop").status_code == 404
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(db), "done", str(second.id)]) == 0
    assert "as done for now; next:" in capsys.readouterr().out
    assert main(["--db", str(db), "stop", str(second.id)]) == 0
    with Store(db) as s:
        assert s.get(second.id).status == Status.DONE


# --- review of #80: which occurrence an action is about ------------------------------------------

def test_done_after_snoozing_an_upcoming_one_skips_only_that_one(store):
    item = daily(store)
    store.snooze(item.id, dt(2026, 10, 27, 9, 30), via="web", now=dt(2026, 10, 27, 8, 30))
    store.complete(item.id, via="web", now=dt(2026, 10, 27, 8, 45))
    after = store.get(item.id)
    assert (after.snoozed_until, after.due_at) == (None, dt(2026, 10, 28, 9, 0))  # tomorrow still comes


def test_snoozing_again_before_it_was_sent_keeps_tomorrow(store):
    item = daily(store)
    store.snooze(item.id, dt(2026, 10, 27, 9, 30), via="web", now=dt(2026, 10, 27, 8, 30))
    store.snooze(item.id, dt(2026, 10, 27, 10, 0), via="web", now=dt(2026, 10, 27, 8, 40))
    after = store.get(item.id)
    assert (after.snoozed_until, after.due_at) == (dt(2026, 10, 27, 10, 0), dt(2026, 10, 28, 9, 0))


def test_a_late_reply_acknowledges_the_reminder_it_answers(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    # 12.5 hours later, "done" in reply to that message (email, call, or a reply without a number).
    store.complete(item.id, via="email", now=dt(2026, 10, 27, 21, 30), occurrence=dt(2026, 10, 27, 9, 0))
    assert store.get(item.id).due_at == dt(2026, 10, 28, 9, 0)  # tomorrow isn't skipped
    assert acks(store) == [("2026-10-27T13:00:00+00:00", "email")]


def test_a_reply_without_a_number_is_about_the_delivered_occurrence():
    from datetime import UTC, datetime

    store = Store()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    item = store.add(Item(text="Take pills", kind=Kind.REMINDER, created_at=now - timedelta(days=2),
                          due_at=now - timedelta(hours=13), recurrence="daily"))
    fire_due(store, MultiNotifier({"twilio_sms": Recorder()}), now=now - timedelta(hours=13))
    reply = commands.run(store, commands.Command("done"), channel="twilio_sms")
    assert reply.startswith("✅ Done for now")
    next_due = store.get(item.id).due_at
    assert now < next_due < now + timedelta(hours=13)  # the next one (about 11h away), not the one after
    assert [via for _, via in acks(store)] == ["twilio_sms"]


def test_done_on_an_already_acknowledged_delivery_means_the_next_one(store):
    item = store.add(Item(text="Drink water", kind=Kind.REMINDER, created_at=dt(2026, 10, 27, 8, 0),
                          due_at=dt(2026, 10, 27, 9, 0), recurrence="interval:120"))
    tick(store, dt(2026, 10, 27, 9, 0))
    store.complete(item.id, via="api", now=dt(2026, 10, 27, 9, 5))  # that one, acknowledged
    assert store.get(item.id).due_at == dt(2026, 10, 27, 11, 0)
    store.complete(item.id, via="api", now=dt(2026, 10, 27, 10, 55))  # "done" again: skip 11:00
    assert store.get(item.id).due_at == dt(2026, 10, 27, 13, 0)


class Flaky:
    def __init__(self):
        self.sent, self.calls = [], 0

    def send(self, item):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("down")
        self.sent.append(item.text)


def test_done_stops_retries_of_that_occurrence(store):
    item = daily(store)
    sms = Flaky()
    channels = MultiNotifier({"push": Recorder(), "sms": sms})
    fire_due(store, channels, now=dt(2026, 10, 27, 9, 0))  # push delivered, sms retries at 09:01
    store.complete(item.id, via="telegram", now=dt(2026, 10, 27, 9, 0, 30))
    fire_due(store, channels, now=dt(2026, 10, 27, 9, 1))
    assert sms.sent == []
    assert store.get(item.id).due_at == dt(2026, 10, 28, 9, 0)


def test_after_downtime_a_stale_snooze_and_the_next_occurrence_send_one_notice(store):
    item = daily(store)
    tick(store, dt(2026, 10, 27, 9, 0))
    store.snooze(item.id, dt(2026, 10, 27, 9, 15), via="api", now=dt(2026, 10, 27, 9, 5))
    push = Recorder()
    tick(store, dt(2026, 10, 28, 10, 0), push)  # down from 09:10 yesterday until now
    tick(store, dt(2026, 10, 28, 10, 1), push)
    assert push.sent == [("Take pills", dt(2026, 10, 28, 9, 0))]
    assert store.get(item.id).snoozed_until is None


def test_due_reminders_are_found_through_indexes(store):
    sql = "EXPLAIN QUERY PLAN SELECT * FROM items WHERE status = ? AND (due_at <= ? OR snoozed_until <= ?)"
    plan = " ".join(r[3] for r in store.conn.execute(sql, ("pending", "x", "x")))
    assert "idx_items_due" in plan and "idx_items_snoozed" in plan and "SCAN" not in plan
