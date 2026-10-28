"""#80: done and snooze on a recurring reminder are about one occurrence, not the series (ADR 0009)."""


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
