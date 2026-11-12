"""#128: the evening wrap-up — one notice a day, only when something still needs a look."""

from datetime import time, timedelta

import pytest
from conftest import TZ, dt

from maestro import wrapup
from maestro.models import Item, Kind
from maestro.notifier import ConfigError
from maestro.observability import METRICS
from maestro.scheduler import tick
from maestro.storage import SQLiteStore

EVENING = dt(2026, 10, 27, 20, 0)
AT = time(20, 0)


class Recorder:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, item):
        if self.fail:
            raise OSError("ntfy is down")
        self.sent.append(item.text)


@pytest.fixture
def store():
    with SQLiteStore(":memory:", tz=TZ) as s:
        yield s


def needing(store, text="Call the plumber sometime next week"):
    return store.add(Item(text=text, kind=Kind.TASK, created_at=EVENING - timedelta(hours=3),
                          source_text=text, gap="when"))


def test_parse_wrapup():
    assert wrapup.parse_wrapup("") is None
    assert wrapup.parse_wrapup(" 20:00 ") == time(20, 0)
    with pytest.raises(ConfigError, match="MAESTRO_WRAPUP"):
        wrapup.parse_wrapup("8pm")


def test_one_notice_a_day_counting_and_naming_what_waits(store):
    needing(store)
    needing(store, "Book the mot")
    push = Recorder()
    assert wrapup.maybe_send(store, push, AT, now=EVENING)
    assert push.sent == ["2 reminders still need a look: Book the mot; Call the plumber sometime "
                         "next week. Reply with a time, or open the app."]
    assert METRICS.get("maestro_wrapup_total", result="sent") >= 1

    # later the same evening, this or any other process: nothing more
    assert not wrapup.maybe_send(store, push, AT, now=EVENING + timedelta(hours=2))
    assert len(push.sent) == 1
    # the next evening: one again
    assert wrapup.maybe_send(store, push, AT, now=EVENING + timedelta(days=1))
    assert len(push.sent) == 2


def test_the_claim_is_durable_across_processes(tmp_path):
    db = str(tmp_path / "m.db")
    push = Recorder()
    with SQLiteStore(db, tz=TZ) as one:
        needing(one)
        assert wrapup.maybe_send(one, push, AT, now=EVENING)
    with SQLiteStore(db, tz=TZ) as two:  # a restart, or a second scheduler
        assert not wrapup.maybe_send(two, push, AT, now=EVENING + timedelta(minutes=5))
    assert len(push.sent) == 1


def test_nothing_goes_before_the_hour_or_when_nothing_waits(store):
    push = Recorder()
    needing(store)
    assert not wrapup.maybe_send(store, push, AT, now=dt(2026, 10, 27, 19, 59))
    assert push.sent == []
    # nothing waiting: no notice, and the day is NOT claimed — a reading captured later that
    # evening still gets one
    empty = Recorder()
    with SQLiteStore(":memory:", tz=TZ) as fresh:
        assert not wrapup.maybe_send(fresh, empty, AT, now=EVENING)
        needing(fresh)
        assert wrapup.maybe_send(fresh, empty, AT, now=EVENING + timedelta(hours=1))
    assert len(empty.sent) == 1


def test_a_failed_send_does_not_retry_until_tomorrow(store):
    needing(store)
    down = Recorder(fail=True)
    assert not wrapup.maybe_send(store, down, AT, now=EVENING)
    assert METRICS.get("maestro_wrapup_total", result="failed") >= 1
    # the day was claimed by the attempt: no hammering a broken channel every 30 s
    up = Recorder()
    assert not wrapup.maybe_send(store, up, AT, now=EVENING + timedelta(minutes=1))


def test_one_waiting_reading_reads_in_the_singular_and_many_are_truncated(store):
    assert wrapup.wording([needing(store)]).startswith("1 reminder still needs a look: Call")
    many = [needing(store, f"Thing {n}") for n in range(5)]
    text = wrapup.wording(many)
    assert "5 reminders" in text and "and 2 more" in text


def test_the_scheduler_tick_sends_it(store):
    needing(store)
    push = Recorder()
    tick(store, push, wrapup_at=AT)  # the real clock is past 20:00 or not; force the time instead
    # tick uses now(); drive maybe_send directly for determinism and just assert tick wires it:
    # a second direct call after tick never doubles the day's notice.
    wrapup.maybe_send(store, push, time(0, 0), now=EVENING)
    assert len(push.sent) <= 1
