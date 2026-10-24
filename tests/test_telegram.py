from datetime import timedelta

import pytest
from conftest import dt

from maestro.config import Config
from maestro.models import Item, Kind, Status
from maestro.notifier import ConfigError, MultiNotifier, build_notifier
from maestro.scheduler import fire_due
from maestro.store import Store
from maestro.telegram import TelegramBot, TelegramClient, TelegramError, TelegramNotifier

CHAT = 4242
STRANGER = 666


class FakeTransport:
    """Records Bot API calls; getUpdates returns queued batches."""

    def __init__(self):
        self.calls = []
        self.batches = []

    def __call__(self, method, params, timeout):
        self.calls.append((method, params))
        if method == "getUpdates":
            return self.batches.pop(0) if self.batches else []
        return {"message_id": len(self.calls)}

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def message(text, chat=CHAT, update_id=1):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat}, "text": text}}


def button(data, chat=CHAT, update_id=1):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data,
        "message": {"message_id": 99, "chat": {"id": chat}, "text": "⏰ Call mom"},
    }}


@pytest.fixture
def setup():
    transport = FakeTransport()
    store = Store()
    bot = TelegramBot(TelegramClient(transport), store, str(CHAT))
    return transport, store, bot


def test_plain_message_is_captured_and_confirmed_with_buttons(setup):
    transport, store, bot = setup
    bot.handle(message("call mom tomorrow at 5pm"))

    (item,) = store.list()
    assert item.text == "Call mom"
    (reply,) = transport.sent()
    assert reply["chat_id"] == CHAT
    assert reply["text"].startswith("Saved ⏰ #1 Call mom")
    callbacks = [b["callback_data"] for b in reply["reply_markup"]["inline_keyboard"][0]]
    assert callbacks == ["done:1", "snooze:1:10m", "snooze:1:1h", "snooze:1:1d"]


def test_notes_are_confirmed_without_buttons(setup):
    transport, store, bot = setup
    bot.handle(message("remember the gate code is 4321"))
    (reply,) = transport.sent()
    assert reply["text"] == "Saved 📝 #1 The gate code is 4321 (note)"
    assert "reply_markup" not in reply


def test_strangers_are_ignored(setup):
    transport, store, bot = setup
    bot.handle(message("call mom at 5pm", chat=STRANGER))
    bot.handle(button("done:1", chat=STRANGER))
    assert store.list() == []
    assert transport.sent() == []


def test_start_reveals_chat_id_only_when_unconfigured():
    transport = FakeTransport()
    bot = TelegramBot(TelegramClient(transport), Store(), None)
    bot.handle(message("/start", chat=STRANGER))
    bot.handle(message("buy milk", chat=STRANGER))
    (reply,) = transport.sent()
    assert "TELEGRAM_CHAT_ID=666" in reply["text"]


def test_commands(setup):
    transport, store, bot = setup
    bot.handle(message("call mom tomorrow at 5pm"))
    bot.handle(message("remember the gate code is 4321"))
    bot.handle(message("/list@MaestroBot"))
    assert transport.sent()[-1]["text"].splitlines()[0].startswith("⏰ #1 Call mom")

    bot.handle(message("/snooze 1 2h"))
    assert transport.sent()[-1]["text"].startswith("😴 Snoozed")
    bot.handle(message("/done #1"))
    assert store.get(1).status == Status.DONE
    bot.handle(message("/rm 2"))
    assert store.get(2) is None

    for bad in ["/done", "/done abc", "/snooze 1 soon", "/done 99", "/frobnicate"]:
        bot.handle(message(bad))
    replies = [r["text"] for r in transport.sent()[-5:]]
    assert replies == ["Which one? Add its number, e.g. /done 3 (see /list).", "Usage: /done <id>",
                       "Can't read duration 'soon'; try 10m, 2h or 1d", "No item #99",
                       "Unknown command /frobnicate. Try /help"]


def test_buttons_act_and_replace_the_keyboard(setup):
    transport, store, bot = setup
    store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 23),
                   due_at=dt(2026, 10, 23, 9)))

    bot.handle(button("snooze:1:10m"))
    assert store.get(1).status == Status.PENDING
    assert transport.sent("answerCallbackQuery")[-1]["text"].startswith("😴 Snoozed")

    bot.handle(button("done:1", update_id=2))
    assert store.get(1).status == Status.DONE
    edited = transport.sent("editMessageText")[-1]
    assert edited["text"] == "⏰ Call mom\n✅ Done: Call mom"
    assert "reply_markup" not in edited


def test_fetch_advances_offset_so_updates_are_not_redelivered(setup):
    transport, store, bot = setup
    transport.batches = [[message("buy milk", update_id=7), message("buy eggs", update_id=8)], []]
    bot.poll_once(timeout=1)
    bot.poll_once(timeout=1)
    offsets = [p.get("offset") for m, p in transport.calls if m == "getUpdates"]
    assert offsets == [None, 9]
    assert [i.text for i in store.list()] == ["Buy milk", "Buy eggs"]


def test_reminder_delivery_via_telegram_channel():
    transport = FakeTransport()
    store = Store()
    item = store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 23),
                          due_at=dt(2026, 10, 23, 9)))
    notifier = MultiNotifier({"telegram": TelegramNotifier(TelegramClient(transport), str(CHAT))})

    fire_due(store, notifier, now=dt(2026, 10, 23, 9) + timedelta(seconds=1))
    (sent,) = transport.sent()
    assert sent["text"] == "⏰ Call mom"
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"done:{item.id}"


def test_telegram_errors_during_handling_are_contained(setup):
    _, store, bot = setup

    def broken(method, params, timeout):
        raise TelegramError("sendMessage failed: Forbidden: bot was blocked by the user")

    bot.client.transport = broken
    bot.handle(message("buy milk"))  # must not raise
    assert [i.text for i in store.list()] == ["Buy milk"]


def test_server_runs_the_bot(setup):
    import time

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    transport, store, bot = setup
    real_transport = transport.__call__

    def slow_when_idle(method, params, timeout):
        result = real_transport(method, params, timeout)
        if method == "getUpdates" and not result:
            time.sleep(0.05)  # stand-in for the long poll
        return result

    bot.client.transport = slow_when_idle
    transport.batches = [[message("call mom at 5pm", update_id=1)]]
    with TestClient(create_app(store, bot=bot)):
        for _ in range(100):
            if store.list():
                break
            time.sleep(0.02)
    assert [i.text for i in store.list()] == ["Call mom"]
    assert transport.sent()[0]["text"].startswith("Saved ⏰")


def test_channel_config():
    with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
        build_notifier(Config(channels=["telegram"], telegram_bot_token="123:abc"))
    built = build_notifier(Config(channels=["telegram"], telegram_bot_token="123:abc", telegram_chat_id="42"))
    assert isinstance(built.channels["telegram"], TelegramNotifier)
