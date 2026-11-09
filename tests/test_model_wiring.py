"""#115: the interpreter reaches every capture path, by injection and never by a global.

The rules stay the default: each test that wants a model passes one in. What is checked here is
that the injected interpreter is actually used, that a capture reading several things saves all
of them once, and that a slow model doesn't stop the server answering other requests.
"""

import threading
import time

import pytest
from conftest import TZ, dt

from maestro.actions import apply
from maestro.interpret import Context, ModelInterpreter, RulesInterpreter
from maestro.models import Kind
from maestro.store import Store

NOW = dt(2026, 10, 27, 8, 0)
THREE = [
    {"type": "create_task", "text": "Book flights"},
    {"type": "create_task", "text": "Add sunscreen to the packing list"},
    {"type": "create_note", "text": "The hotel code is 4471"},
]
MESSAGE = "book flights, add sunscreen to the packing list, the hotel code is 4471"


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, reply, delay: float = 0.0):
        self.reply = reply
        self.delay = delay
        self.calls = 0

    def propose(self, prompt, timeout):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.reply


def model(reply=THREE, delay: float = 0.0) -> ModelInterpreter:
    return ModelInterpreter(FakeProvider(reply, delay))


@pytest.fixture
def store():
    with Store(":memory:", tz=TZ) as s:
        yield s


# --- several things from one message ------------------------------------------------------------


def test_apply_saves_every_action_a_capture_asked_for(store):
    results = apply(store, model().interpret(MESSAGE, Context(tz=TZ, now=NOW)), source="cli")
    assert [r.saved.text for r in results] == ["Book flights", "Add sunscreen to the packing list",
                                               "The hotel code is 4471"]
    assert [i.kind for i in store.list()] == [Kind.TASK, Kind.TASK, Kind.NOTE]


def test_a_retried_message_that_saved_several_things_repeats_none_of_them(store):
    """Each action gets its own key derived from the message's, so a provider retry is still once."""
    read = model().interpret(MESSAGE, Context(tz=TZ, now=NOW))
    first = apply(store, read, source="twilio_sms", dedupe_key="SM123")
    again = apply(store, read, source="twilio_sms", dedupe_key="SM123")
    assert [r.created for r in first] == [True, True, True]
    assert [r.created for r in again] == [False, False, False]
    assert [r.saved.id for r in again] == [r.saved.id for r in first]
    assert len(store.list()) == 3


def test_one_action_keeps_the_key_it_always_had(store):
    """A single-action capture is stored exactly as before several were possible."""
    read = RulesInterpreter().interpret("call mom tomorrow at 5pm", Context(tz=TZ, now=NOW))
    apply(store, read, source="twilio_sms", dedupe_key="SM1")
    apply(store, read, source="twilio_sms", dedupe_key="SM1")
    assert len(store.list()) == 1
    assert store.conn.execute(
        "SELECT external_id FROM inbound_messages").fetchone()["external_id"] == "SM1"


# --- the CLI ------------------------------------------------------------------------------------


def test_the_cli_add_uses_the_interpreter_it_is_given(tmp_path, monkeypatch, capsys):
    from maestro.cli import _build_parser, _dispatch
    from maestro.config import Config

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    config = Config(channels=["console"])
    args = _build_parser(config).parse_args(["add", *MESSAGE.split()])
    with Store(":memory:", tz=TZ) as store:
        assert _dispatch(args, config, store, TZ, model()) == 0
        assert len(store.list()) == 3
    out = capsys.readouterr().out
    assert out.count("Saved:") == 3 and "Book flights" in out and "The hotel code is 4471" in out


# --- Telegram -------------------------------------------------------------------------------------


def test_telegram_uses_the_interpreter_and_reports_everything_it_saved():
    from test_telegram import CHAT, FakeTransport, message

    from maestro.telegram import TelegramBot, TelegramClient

    transport = FakeTransport()
    with Store(":memory:", tz=TZ) as store:
        bot = TelegramBot(TelegramClient(transport), store, str(CHAT), interpreter=model())
        bot.handle(message(MESSAGE))
        assert len(store.list()) == 3
    (reply,) = transport.sent()
    assert "Saved 3 things:" in reply["text"]
    assert "Book flights" in reply["text"] and "The hotel code is 4471" in reply["text"]
    assert "reply_markup" not in reply  # buttons act on one reminder; this saved three things


def test_telegram_still_says_saved_and_offers_buttons_for_one_reminder():
    from test_telegram import CHAT, FakeTransport, message

    from maestro.telegram import TelegramBot, TelegramClient

    transport = FakeTransport()
    with Store(":memory:", tz=TZ) as store:
        bot = TelegramBot(TelegramClient(transport), store, str(CHAT))  # the rules, as always
        bot.handle(message("call mom tomorrow at 5pm"))
    (reply,) = transport.sent()
    assert reply["text"].startswith("Saved ⏰") and "reply_markup" in reply


# --- Twilio ---------------------------------------------------------------------------------------


def test_sms_uses_the_interpreter_and_answers_about_what_it_saved():
    from maestro.sms import SmsHandler

    with Store(":memory:", tz=TZ) as store:
        handler = SmsHandler(store, "token", "+15551112222", interpreter=model())
        item, note = handler.save(MESSAGE, external_id="SM9", source="sms")
        assert item.text == "Book flights"
        assert note.startswith("Saved 3 things.")
        assert len(store.list()) == 3
        # a Twilio retry of the same message repeats nothing
        handler.save(MESSAGE, external_id="SM9", source="sms")
        assert len(store.list()) == 3


# --- the API ---------------------------------------------------------------------------------------


def test_the_api_captures_through_the_interpreter_it_was_built_with():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with Store(":memory:", tz=TZ) as store:
        with TestClient(create_app(store, api_token="t", interpreter=model())) as client:
            auth = {"Authorization": "Bearer t"}
            created = client.post("/items", json={"text": MESSAGE}, headers=auth)
            assert created.status_code == 201
            assert created.json()["text"] == "Book flights"  # the response names the first
            listed = client.get("/items", headers=auth).json()
            assert [i["text"] for i in listed] == [
                "Book flights", "Add sunscreen to the packing list", "The hotel code is 4471"]
            assert [i["kind"] for i in listed] == ["task", "task", "note"]


def test_a_slow_model_does_not_stop_the_api_answering_other_requests():
    """The capture waits on a worker thread, so the event loop keeps serving (#115)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with Store(":memory:", tz=TZ) as store:
        app = create_app(store, api_token="t", interpreter=model(delay=0.4))
        with TestClient(app) as client:
            auth = {"Authorization": "Bearer t"}
            done = threading.Event()

            def capture():
                client.post("/items", json={"text": MESSAGE}, headers=auth)
                done.set()

            worker = threading.Thread(target=capture)
            worker.start()
            time.sleep(0.05)  # the capture is now inside the slow provider
            started = time.monotonic()
            assert client.get("/health").status_code == 200
            assert time.monotonic() - started < 0.3, "the event loop was blocked by the model"
            assert not done.is_set()
            worker.join(timeout=5)
        assert len(store.list()) == 3


def test_the_api_without_an_interpreter_still_uses_the_rules():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with Store(":memory:", tz=TZ) as store:
        with TestClient(create_app(store, api_token="t")) as client:
            created = client.post("/items", json={"text": "dentist tomorrow at 3pm"},
                                  headers={"Authorization": "Bearer t"})
            assert created.json()["kind"] == "reminder"
            assert len(store.list()) == 1
