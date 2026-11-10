"""#113 / ADR 0015: every capture path reads text with `interpret` and saves it with `apply`.

Each path is checked twice over: the words it sends back are the ones it has always sent, and it
really does go through the shared flow — swap the interpreter and the path saves what the swapped
one decided, which only holds if nothing parses or saves on its own any more. Paths that dedupe a
provider's retry (SMS, voice, email) are checked to still capture once and answer the same.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_email_in import OWNER as EMAIL_OWNER
from test_email_in import PROVIDER, FakeMailbox, mail  # noqa: F401  (FakeMailbox is used via the box fixture)
from test_email_in import poller as email_poller
from test_sms import OWNER as SMS_OWNER
from test_sms import TOKEN as SMS_TOKEN
from test_telegram import CHAT, FakeTransport
from test_telegram import message as tg_message
from test_voice import call as voice_call

from maestro.actions import CreateNote
from maestro.cli import main
from maestro.email_in import EmailPoller, EmailSettings
from maestro.interpret import Context
from maestro.models import Kind
from maestro.observability import METRICS
from maestro.sms import SmsHandler
from maestro.store import Store
from maestro.telegram import TelegramBot, TelegramClient
from maestro.voice import VoiceHandler

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro.api import create_app  # noqa: E402

AUTH = {"Authorization": "Bearer t0ken"}
STUB_TEXT = "The interpreter decided this"


class StubInterpreter:
    """Stands in for the rules: returns one note, whatever was said."""

    def __init__(self):
        self.seen: list[tuple[str, Context]] = []

    def __call__(self, text, context=None, interpreter=None):
        self.seen.append((text, context))
        return [CreateNote(text=STUB_TEXT, source_text=text)]


@pytest.fixture
def stub(monkeypatch):
    """Replace `interpret_assessed` in whichever capture module the test names."""
    from maestro.confidence import Confidence

    def install(module: str) -> StubInterpreter:
        import importlib

        fake = StubInterpreter()
        if hasattr(importlib.import_module(f"maestro.{module}"), "interpret_assessed"):
            monkeypatch.setattr(f"maestro.{module}.interpret_assessed",
                                lambda text, context=None, interpreter=None: (fake(text, context),
                                                                              Confidence()))
        else:  # a module that never confirms (email_in) still calls plain interpret
            monkeypatch.setattr(f"maestro.{module}.interpret", fake)
        return fake

    return install


@pytest.fixture
def box():
    return FakeMailbox()


# --- CLI -------------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)  # keep load_dotenv away from any real .env
    return str(tmp_path / "m.db")


def test_cli_add_still_confirms_the_same_way(db, capsys):
    assert main(["--db", db, "add", "call", "mom", "tomorrow", "at", "5pm"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Saved: 1  ⏰ ") and out.endswith("  Call mom\n")

    assert main(["--db", db, "add", "remember", "the", "gate", "code", "is", "4321"]) == 0
    assert capsys.readouterr().out == "Saved: 2  📝 —                 The gate code is 4321\n"


def test_cli_add_still_warns_about_an_unconfigured_channel(db, capsys):
    assert main(["--db", db, "add", "pay", "rent", "friday,", "text", "me"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "Note: twilio_sms isn't configured; using the default channels.\n"
    assert captured.out.startswith("Saved: 1  ⏰ ") and captured.out.endswith("  Pay rent\n")


def test_cli_add_goes_through_the_interpreter(db, stub, capsys):
    fake = stub("cli")
    assert main(["--db", db, "add", "anything", "at", "all"]) == 0
    assert [text for text, _ in fake.seen] == ["anything at all"]
    assert capsys.readouterr().out == f"Saved: 1  📝 —                 {STUB_TEXT}\n"


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(create_app(Store(), api_token="t0ken")) as c:
        yield c


def test_api_capture_still_answers_the_same_way(client):
    created = client.post("/items", json={"text": "gym every monday at 7am"}, headers=AUTH)
    assert created.status_code == 201
    body = created.json()
    assert (body["text"], body["kind"], body["recurrence_label"]) == ("Gym", "reminder", "every Monday")
    assert METRICS.get("maestro_captures_total", source="api") == 1


def test_api_capture_goes_through_the_interpreter(client, stub):
    fake = stub("api")
    body = client.post("/items", json={"text": "anything at all"}, headers=AUTH).json()
    assert [text for text, _ in fake.seen] == ["anything at all"]
    assert (body["text"], body["kind"]) == (STUB_TEXT, "note")


# --- SMS -------------------------------------------------------------------------------------


@pytest.fixture
def sms_handler():
    return SmsHandler(Store(), SMS_TOKEN, SMS_OWNER)


def sms_params(body, message_id="SM1"):
    return {"From": SMS_OWNER, "To": "+15550001111", "Body": body, "MessageSid": message_id}


def test_sms_capture_still_replies_the_same_way(sms_handler):
    reply = sms_handler.handle(sms_params("call mom tomorrow at 5pm"))
    assert "Saved ⏰ #1 Call mom" in reply
    assert "Reply 'done 1' or 'snooze 1 1h'." in reply
    assert METRICS.get("maestro_captures_total", source="sms") == 1


def test_a_retried_sms_is_captured_once_and_answered_the_same(sms_handler):
    params = sms_params("dentist on friday at 3pm")
    assert sms_handler.handle(params) == sms_handler.handle(params)  # Twilio retries the same SID
    assert len(sms_handler.store.list()) == 1
    assert METRICS.get("maestro_captures_total", source="sms") == 1


def test_sms_capture_goes_through_the_interpreter(sms_handler, stub):
    fake = stub("sms")
    reply = sms_handler.handle(sms_params("anything at all"))
    assert [text for text, _ in fake.seen] == ["anything at all"]
    assert f"Saved 📝 #1 {STUB_TEXT} (note)." in reply
    assert "Reply 'done" not in reply  # a note gets no action hint


# --- voice -----------------------------------------------------------------------------------


@pytest.fixture
def voice():
    return VoiceHandler(Store(), SMS_TOKEN, SMS_OWNER)


def said(twiml: str) -> str:
    return ET.fromstring(twiml).find("Say").text


def test_voice_capture_still_reads_back_the_same_way(voice):
    spoken = voice_call(SpeechResult="Remind me to call mom tomorrow at 5 p.m.", Confidence="0.93")
    assert said(voice.capture(spoken)).startswith("Got it: Call mom, ")
    assert METRICS.get("maestro_captures_total", source="voice") == 1


def test_a_retried_call_is_captured_once_and_answered_the_same(voice):
    spoken = voice_call(SpeechResult="Call mom at 5 p.m.")
    assert said(voice.capture(spoken)) == said(voice.capture(spoken))  # same CallSid
    assert len(voice.store.list()) == 1
    assert METRICS.get("maestro_captures_total", source="voice") == 1


def test_voice_capture_goes_through_the_interpreter(voice, stub):
    fake = stub("sms")  # voice captures through TwilioHandler.save, which lives in maestro.sms
    voice.capture(voice_call(SpeechResult="anything at all", Confidence="0.9"))
    assert [text for text, _ in fake.seen] == ["anything at all"]
    (item,) = voice.store.list()
    assert (item.text, item.kind) == (STUB_TEXT, Kind.NOTE)


# --- Telegram --------------------------------------------------------------------------------


@pytest.fixture
def telegram():
    transport = FakeTransport()
    store = Store()
    return transport, store, TelegramBot(TelegramClient(transport), store, str(CHAT))


def test_telegram_capture_still_confirms_the_same_way(telegram):
    transport, store, bot = telegram
    bot.handle(tg_message("call mom tomorrow at 5pm"))
    (reply,) = transport.sent()
    assert reply["text"].startswith("Saved ⏰ #1 Call mom (")
    assert reply["reply_markup"]  # a reminder still gets its action buttons
    assert METRICS.get("maestro_captures_total", source="telegram") == 1


def test_telegram_capture_goes_through_the_interpreter(telegram, stub):
    transport, store, bot = telegram
    fake = stub("telegram")
    bot.handle(tg_message("anything at all"))
    assert [text for text, _ in fake.seen] == ["anything at all"]
    (reply,) = transport.sent()
    assert reply["text"] == f"Saved 📝 #1 {STUB_TEXT} (note)"
    assert "reply_markup" not in reply  # a note gets no buttons


# --- email -----------------------------------------------------------------------------------


def test_email_capture_still_saves_the_same_item(box):
    box.add(mail(subject="dentist next tuesday at 3pm"))
    (item,) = email_poller(box).poll_once()
    assert (item.text, item.kind) == ("Dentist", Kind.REMINDER)
    assert METRICS.get("maestro_captures_total", source="email") == 1


def test_an_email_arriving_twice_is_captured_once(box):
    raw = mail(subject="dentist next tuesday at 3pm")
    box.add(raw)
    poll = email_poller(box)
    (first,) = poll.poll_once()
    box.seen.clear()  # the provider (or a failed STORE) shows the same message again
    (again,) = poll.poll_once()
    assert again.id == first.id
    assert len(poll.store.list()) == 1
    assert METRICS.get("maestro_captures_total", source="email") == 1


def test_email_capture_keeps_only_the_configured_channels(box, caplog):
    box.add(mail(subject="pay rent friday, text me and call me"))
    settings = EmailSettings(host="imap.example.com", user="maestro", password="pw", folder="INBOX",
                             allowed_senders=frozenset({EMAIL_OWNER}), require_dmarc=True,
                             authserv_id=PROVIDER, poll_seconds=10)
    poll = EmailPoller(Store(), settings, connect=lambda _: box, requestable=frozenset({"twilio_sms"}))
    with caplog.at_level("WARNING"):
        (item,) = poll.poll_once()
    assert item.channels == ["twilio_sms"]
    assert "asked for twilio_call, which isn't configured; keeping ['twilio_sms']" in caplog.text


def test_email_capture_goes_through_the_interpreter(box, stub):
    fake = stub("email_in")
    box.add(mail(subject="anything at all"))
    (item,) = email_poller(box).poll_once()
    assert [text for text, _ in fake.seen] == ["anything at all"]
    assert (item.text, item.kind) == (STUB_TEXT, Kind.NOTE)


# --- the invariant ----------------------------------------------------------------------------


def test_only_apply_turns_a_capture_into_store_calls():
    """ADR 0015: one place reads text, one place saves it. Both are easy to bypass by accident."""
    package = Path(__file__).resolve().parent.parent / "maestro"
    parses, saves = [], []
    for path in sorted(package.rglob("*.py")):
        source = path.read_text()
        name = path.relative_to(package.parent).as_posix()
        if "parse(" in source.replace("parse_command(", "").replace("parse_duration(", "") \
                .replace("parse_url(", "").replace("parse_qs(", "").replace("def parse(", "") \
                .replace("urlparse(", ""):  # urllib.parse.urlparse is not the capture parser
            parses.append(name)
        if ".add_once(" in source or "store.add(" in source:
            saves.append(name)
    assert parses == ["maestro/interpret.py"], f"the parser is called outside interpret: {parses}"
    assert saves == ["maestro/actions.py"], f"items are saved outside apply: {saves}"


# --- a provider that sends no id (#113 review) ------------------------------------------------


@pytest.mark.parametrize("missing", [{}, {"MessageSid": ""}], ids=["absent", "empty"])
def test_an_sms_without_a_message_id_is_captured_every_time(sms_handler, missing):
    """No id, nothing to dedupe on: each arrival is its own capture, as it was before ADR 0015.
    An empty id must never be used as a key, or every keyless SMS would collapse into one item."""
    params = {"From": SMS_OWNER, "To": "+15550001111", "Body": "dentist on friday at 3pm", **missing}
    first, second = sms_handler.handle(params), sms_handler.handle(params)
    assert "#1 Dentist" in first and "#2 Dentist" in second
    assert len(sms_handler.store.list()) == 2
    assert not sms_handler.store.is_claimed("twilio_sms", "")
    assert METRICS.get("maestro_captures_total", source="sms") == 2


@pytest.mark.parametrize("missing", [{}, {"CallSid": ""}], ids=["absent", "empty"])
def test_a_call_without_a_call_id_is_captured_every_time(voice, missing):
    spoken = {"From": SMS_OWNER, "To": "+15550001111", "SpeechResult": "gym at 7am",
              "Confidence": "0.93", **missing}
    voice.capture(spoken)
    voice.capture(spoken)
    assert len(voice.store.list()) == 2
    assert not voice.store.is_claimed("twilio_voice", "")
