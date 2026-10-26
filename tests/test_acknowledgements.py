"""#19: acknowledgements of delivered reminders, the basis for escalation (#12)."""

from datetime import timedelta

import pytest
from conftest import dt

from maestro import commands
from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.scheduler import fire_due
from maestro.store import Store

DUE = dt(2026, 10, 23, 9, 0)


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
    assert [tuple(r) for r in acks(store)] == [(item.id, "2026-10-23T13:00:00+00:00", "twilio_sms")]
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

    store.snooze(item.id, dt(2026, 10, 23, 8, 30), via="api")
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
