"""#8: reply "done" or "snooze 1h" to a reminder, without its number."""

from datetime import timedelta

import pytest
from conftest import dt

from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.scheduler import fire_due
from maestro.sms import SmsHandler
from maestro.store import Store
from maestro.telegram import TelegramBot, TelegramClient

OWNER = "+15552223333"


class Sink:
    def send(self, item):
        pass


def remind(store, text, due, channels=("twilio_sms",)):
    item = store.add(Item(text=text, kind=Kind.REMINDER, created_at=due - timedelta(days=1), due_at=due))
    fire_due(store, MultiNotifier({name: Sink() for name in channels}), now=due)
    return item


def reply(handler, body):
    return handler.handle({"From": OWNER, "To": "+15550001111", "Body": body})


@pytest.fixture
def setup():
    store = Store()
    return store, SmsHandler(store, "token", OWNER)


def test_done_applies_to_the_latest_reminder_on_the_channel(setup):
    store, sms = setup
    first = remind(store, "Call mom", dt(2026, 10, 23, 9, 0))
    second = remind(store, "Take pills", dt(2026, 10, 23, 10, 0))

    assert "Done: Take pills" in reply(sms, "done")
    assert store.get(second.id).status == Status.DONE
    assert store.get(first.id).status == Status.FIRED


def test_snooze_without_number(setup):
    store, sms = setup
    item = remind(store, "Call mom", dt(2026, 10, 23, 9, 0))

    assert "Snoozed" in reply(sms, "Snooze 1h")
    after_1h = store.get(item.id).due_at
    reply(sms, "snooze")  # default 10 minutes
    after_10m = store.get(item.id).due_at
    assert store.get(item.id).status == Status.PENDING
    assert timedelta(minutes=45) < after_1h - after_10m < timedelta(minutes=55)


def test_rm_is_never_inferred(setup):
    store, sms = setup
    item = remind(store, "Call mom", dt(2026, 10, 23, 9, 0))
    assert reply(sms, "rm").count("Which one?") == 1
    assert store.get(item.id) is not None


def test_nothing_delivered_yet_asks_which_one(setup):
    store, sms = setup
    store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 22), due_at=dt(2026, 10, 30)))
    assert "Which one? Add its number, e.g. done 3 (see list)." in reply(sms, "done")
    assert store.get(1).status == Status.PENDING


def test_channels_are_independent(setup):
    store, sms = setup
    remind(store, "Call mom", dt(2026, 10, 23, 9, 0), channels=("telegram",))
    assert "Which one?" in reply(sms, "done")


def test_deleted_items_are_skipped(setup):
    store, sms = setup
    older = remind(store, "Call mom", dt(2026, 10, 23, 9, 0))
    newer = remind(store, "Take pills", dt(2026, 10, 23, 10, 0))
    store.delete(newer.id)
    assert "Done: Call mom" in reply(sms, "done")
    assert store.get(older.id).status == Status.DONE


def test_explicit_number_still_wins(setup):
    store, sms = setup
    first = remind(store, "Call mom", dt(2026, 10, 23, 9, 0))
    remind(store, "Take pills", dt(2026, 10, 23, 10, 0))
    assert "Done: Call mom" in reply(sms, f"done {first.id}")


def test_telegram_plain_and_slash_replies():
    sent = []
    store = Store()
    bot = TelegramBot(TelegramClient(lambda method, params, timeout: sent.append(params) or {}), store, "42")
    item = remind(store, "Call mom", dt(2026, 10, 23, 9, 0), channels=("telegram",))

    def say(text):
        bot.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 42}, "text": text}})
        return sent[-1]["text"]

    assert say("snooze 2h").startswith("😴 Snoozed")
    assert say("/done") == "✅ Done: Call mom"
    assert store.get(item.id).status == Status.DONE
