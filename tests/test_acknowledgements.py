"""#19: acknowledgements of delivered reminders, the basis for escalation (#12)."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import TZ, dt

from maestro import commands
from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.scheduler import fire_due
from maestro.store import Store

# Acknowledgements only apply to recent deliveries (ACK_WINDOW), and some paths use the real clock,
# so deliveries in these tests happen "just now".
NOW = datetime.now(TZ).replace(second=0, microsecond=0)
DUE = NOW - timedelta(minutes=5)


class Sink:
    def send(self, item):
        pass


def deliver(store, now):
    fire_due(store, MultiNotifier({"push": Sink()}), now=now)


def acks(store):
    sql = "SELECT item_id, occurrence, via FROM acknowledgements ORDER BY item_id"
    return store.conn.execute(sql).fetchall()


@pytest.fixture
def store():
    return Store()


def reminder(store, text="Call mom", due=DUE):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=due - timedelta(days=1), due_at=due))


def test_delivered_reminders_await_acknowledgement_until_done(store):
    item = reminder(store)
    assert store.unacknowledged() == []  # nothing delivered yet
    deliver(store, DUE)
    assert [(i.id, occurrence) for i, occurrence in store.unacknowledged()] == [(item.id, DUE)]

    assert commands.act(store, "done", [str(item.id)], via="twilio_sms").startswith("✅")
    assert store.unacknowledged() == []
    expected = DUE.astimezone(UTC).isoformat(timespec="seconds")
    assert [tuple(r) for r in acks(store)] == [(item.id, expected, "twilio_sms")]
    assert METRICS.get("maestro_acknowledgements_total", via="twilio_sms") == 1


def test_snooze_acknowledges_and_the_next_delivery_needs_its_own(store):
    item = reminder(store)
    deliver(store, DUE)
    store.snooze(item.id, DUE + timedelta(minutes=10), via="telegram")
    assert store.unacknowledged() == []
    assert store.get(item.id).status == Status.PENDING

    deliver(store, DUE + timedelta(minutes=10))  # the snoozed reminder fires again
    assert [occ for _, occ in store.unacknowledged()] == [DUE + timedelta(minutes=10)]


def test_recurring_reminders_acknowledge_the_occurrence_that_was_sent(store):
    item = store.add(parse("take vitamins every day at 8am", now=dt(2026, 10, 23, 7, 0)))
    deliver(store, dt(2026, 10, 23, 8, 0))
    assert store.get(item.id).due_at == dt(2026, 10, 24, 8, 0)  # already moved on to tomorrow
    assert [occ for _, occ in store.unacknowledged()] == [dt(2026, 10, 23, 8, 0)]

    store.snooze(item.id, dt(2026, 10, 23, 8, 30), via="api", now=dt(2026, 10, 23, 8, 5))
    assert store.unacknowledged() == []
    (row,) = acks(store)
    assert row["occurrence"] == "2026-10-23T12:00:00+00:00"  # today's 08:00, not tomorrow's


def test_acting_before_delivery_records_nothing(store):
    item = reminder(store)
    store.complete(item.id, via="cli")
    assert acks(store) == []
    assert store.get(item.id).status == Status.DONE


def test_acknowledging_twice_counts_once(store):
    item = reminder(store)
    deliver(store, DUE)
    store.snooze(item.id, DUE + timedelta(hours=1), via="api")
    store.complete(item.id, via="cli")  # same delivered occurrence
    assert len(acks(store)) == 1
    assert METRICS.get("maestro_acknowledgements_total", via="cli") == 0


def test_done_items_are_never_listed_and_delete_cleans_up(store):
    item = reminder(store)
    deliver(store, DUE)
    store.set_status(item.id, Status.DONE)  # e.g. a status change that isn't an acknowledgement
    assert store.unacknowledged() == []
    store.delete(item.id)
    assert store.conn.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0] == 0


def test_every_interface_records_who_acknowledged(tmp_path, monkeypatch):
    from maestro.cli import main
    from maestro.telegram import TelegramBot, TelegramClient

    db = tmp_path / "m.db"
    with Store(db) as store:
        first, second = reminder(store, "A"), reminder(store, "B")
        deliver(store, DUE)
        bot = TelegramBot(TelegramClient(lambda method, params, timeout: {}), store, "42")
        message = {"message_id": 9, "chat": {"id": 42}, "text": "A"}
        query = {"id": "cb", "data": f"done:{first.id}", "message": message}
        bot.handle({"update_id": 1, "callback_query": query})
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(db), "snooze", str(second.id), "1h"]) == 0
    with Store(db) as store:
        assert {r["item_id"]: r["via"] for r in acks(store)} == {first.id: "telegram", second.id: "cli"}


def test_api_exposes_acknowledgement_state(store):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    first, second = reminder(store, "A"), reminder(store, "B")
    deliver(store, DUE)
    with TestClient(create_app(store)) as client:
        assert [i["text"] for i in client.get("/items?unacknowledged=true").json()] == ["A", "B"]
        assert client.get(f"/items/{first.id}").json()["awaiting_acknowledgement"] is True
        client.post(f"/items/{first.id}/done")
        assert [i["text"] for i in client.get("/items?unacknowledged=true").json()] == ["B"]
        assert client.get(f"/items/{second.id}").json()["awaiting_acknowledgement"] is True
        assert "maestro_unacknowledged 1" in client.get("/metrics").text
        assert 'maestro_acknowledgements_total{via="api"} 1' in client.get("/metrics").text


def test_old_deliveries_are_not_acknowledged_by_unrelated_actions(store):
    # Yesterday's delivery was ignored; snoozing today's upcoming reminder doesn't acknowledge it.
    item = reminder(store, due=dt(2026, 10, 22, 9, 0))
    deliver(store, dt(2026, 10, 22, 9, 0))
    store.snooze(item.id, dt(2026, 10, 23, 10, 0), via="api", now=dt(2026, 10, 23, 8, 0))
    assert acks(store) == []
    # Within the window it does: replying "done" a few hours later still counts.
    other = reminder(store, "B", due=dt(2026, 10, 23, 9, 0))
    deliver(store, dt(2026, 10, 23, 9, 0))
    store.complete(other.id, via="twilio_sms", now=dt(2026, 10, 23, 17, 0))
    assert [r["item_id"] for r in acks(store)] == [other.id]


def test_the_scheduler_never_overwrites_a_done_or_snooze(store):
    item = reminder(store)
    # The user taps Done between the scheduler recording the delivery and finishing the occurrence.
    store.complete(item.id, via="telegram")
    assert not store.finish_occurrence(item.id, DUE, None)
    assert store.get(item.id).status == Status.DONE
    # Same for a snooze racing a recurring item moving on.
    other = reminder(store, "B")
    store.snooze(other.id, NOW + timedelta(hours=1), via="api")
    assert not store.finish_occurrence(other.id, DUE, DUE + timedelta(days=1))
    assert store.get(other.id).due_at == NOW + timedelta(hours=1)


def test_existing_deliveries_count_as_acknowledged_after_upgrade(tmp_path):
    from maestro.store import MIGRATIONS

    path = tmp_path / "v6.db"
    with Store(path) as store:
        reminder(store)
        deliver(store, DUE)
        store.conn.execute("DROP TABLE acknowledgements")
        store.conn.execute("ALTER TABLE items DROP COLUMN snoozed_until")
        store.conn.execute("PRAGMA user_version = 6")  # as a 0.3 database
        store.conn.commit()
    with Store(path) as store:
        assert store.schema_version == len(MIGRATIONS)
        assert store.unacknowledged() == []  # history isn't escalated all at once
        assert [r["via"] for r in acks(store)] == ["migration"]


def test_acknowledgement_metric_counts_only_committed_writes(store, monkeypatch):
    import sqlite3

    item = reminder(store)
    deliver(store, DUE)

    def failing_commit():
        raise sqlite3.OperationalError("disk I/O error")

    store.conn = _ConnProxy(store.conn, failing_commit)
    with pytest.raises(sqlite3.OperationalError):
        store.complete(item.id, via="api")
    assert METRICS.get("maestro_acknowledgements_total", via="api") == 0
    store.conn = store.conn.inner


class _ConnProxy:
    """A connection whose commit fails, everything else passed through."""

    def __init__(self, inner, commit):
        self.inner, self.commit = inner, commit

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_web_ui_actions_are_recorded_as_web_and_filters_combine(store):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    first, second = reminder(store, "Call mom"), reminder(store, "Pay rent")
    deliver(store, DUE)
    with TestClient(create_app(store)) as client:
        assert [i["text"] for i in client.get("/items?unacknowledged=true&q=rent").json()] == ["Pay rent"]
        client.post(f"/items/{first.id}/done", headers={"X-Maestro-Client": "web"})
        client.post(f"/items/{second.id}/snooze", json={"duration": "1h"})
    assert {r["item_id"]: r["via"] for r in acks(store)} == {first.id: "web", second.id: "api"}
