"""Long-running loops must survive bad data: one broken item or update never stops the others."""

import logging

import pytest
from conftest import dt

from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.observability import METRICS
from maestro.scheduler import fire_due, tick
from maestro.store import Store

DUE = dt(2026, 10, 23, 9, 0)


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item.text)


def reminder(store, text, recurrence=None):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=DUE, due_at=DUE, recurrence=recurrence))


def test_corrupt_item_does_not_block_the_others(caplog):
    store = Store()
    broken = reminder(store, "Broken", recurrence="fortnightly-ish")  # not a valid rule
    good = reminder(store, "Call mom")
    push = Recorder()

    with caplog.at_level(logging.ERROR):
        finished = fire_due(store, MultiNotifier({"push": push}), now=DUE)
    assert [i.id for i in finished] == [good.id]
    assert store.get(good.id).status == Status.FIRED
    assert store.get(broken.id).status == Status.PENDING
    assert METRICS.get("maestro_scheduler_errors_total") == 1
    assert f"Failed to process reminder #{broken.id}" in caplog.text


def test_malformed_due_rows_are_skipped(monkeypatch, caplog):
    store = Store()
    good = reminder(store, "Call mom")
    real_due = store.due
    monkeypatch.setattr(store, "due", lambda now: [Item(text="ghost", kind=Kind.REMINDER, created_at=DUE),
                                                   *real_due(now)])
    with caplog.at_level(logging.ERROR):
        finished = fire_due(store, MultiNotifier({"push": Recorder()}), now=DUE)
    assert [i.id for i in finished] == [good.id]
    assert "Skipping malformed due item" in caplog.text


def test_tick_never_raises(monkeypatch, caplog):
    store = Store()

    def broken(now):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "due", broken)
    with caplog.at_level(logging.ERROR):
        tick(store, MultiNotifier({}))  # must not raise
    assert "Scheduler tick failed" in caplog.text
    assert METRICS.get("maestro_scheduler_errors_total") == 1


@pytest.mark.parametrize("update", ["x", None, 42, {"message": {"chat": {}, "text": "hi"}}])
def test_bot_survives_malformed_updates(update, caplog):
    from maestro.telegram import TelegramBot, TelegramClient

    bot = TelegramBot(TelegramClient(lambda m, p, t: {}), Store(), "42")
    with caplog.at_level(logging.ERROR):
        bot.handle(update)  # must not raise
    assert "Failed to handle Telegram update" in caplog.text


# --- items deleted between an update and the read-back ------------------------------------

def _vanish_on(monkeypatch, store_or_cls, method):
    original = getattr(store_or_cls, method)

    def and_delete(self, item_id, *args, **kwargs):
        original(self, item_id, *args, **kwargs)
        self.delete(item_id)

    monkeypatch.setattr(store_or_cls, method, and_delete)


def test_chat_snooze_reports_a_vanished_item(monkeypatch):
    from maestro import commands

    store = Store()
    reminder(store, "Call mom")
    _vanish_on(monkeypatch, Store, "snooze")
    assert commands.act(store, "snooze", ["1", "1h"]) == "No item #1"


def test_cli_snooze_fails_for_a_vanished_item(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", "call", "mom", "tomorrow", "at", "5pm"])
    _vanish_on(monkeypatch, Store, "snooze")
    assert main(["--db", db, "snooze", "1", "1h"]) == 1
    assert "No item #1" in capsys.readouterr().err


@pytest.mark.parametrize("route, method, body", [
    ("/items/1/done", "complete", None),
    ("/items/1/snooze", "snooze", {"duration": "10m"}),
])
def test_api_returns_404_for_a_vanished_item(monkeypatch, route, method, body):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    store = Store()
    reminder(store, "Call mom")
    _vanish_on(monkeypatch, Store, method)
    with TestClient(create_app(store)) as client:
        assert client.post(route, json=body).status_code == 404
