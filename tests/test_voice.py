"""#7: call your Twilio number and say what to remember."""

import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import pytest

from maestro.observability import METRICS
from maestro.sms import twilio_signature
from maestro.store import Store
from maestro.voice import VOICE_CAPTURE_PATH, VOICE_PATH, VoiceHandler, spoken_time

TOKEN, OWNER, PUBLIC = "s3cr3t", "+15552223333", "https://maestro.example.com"


def xml(twiml: str) -> ET.Element:
    return ET.fromstring(twiml)  # Twilio rejects malformed TwiML, so every response must parse


@pytest.fixture
def voice():
    return VoiceHandler(Store(), TOKEN, OWNER)


_call_ids = iter(range(1, 10_000))


def call(**params):
    """A fresh call (unique CallSid) unless one is given."""
    return {"From": OWNER, "To": "+15550001111", "CallSid": f"CA{next(_call_ids)}", **params}


def test_owner_is_asked_what_to_remember(voice):
    root = xml(voice.incoming(call()))
    gather = root.find("Gather")
    assert gather.get("input") == "speech"
    # Relative, so Twilio keeps any path prefix of the public URL when it resolves it.
    resolved = urljoin(f"{PUBLIC}/maestro{VOICE_PATH}", gather.get("action"))
    assert resolved == f"{PUBLIC}/maestro{VOICE_CAPTURE_PATH}"
    assert gather.find("Say").text == "What should I remember?"
    assert root.find("Hangup") is not None  # after the fallback message if nothing is said


def test_strangers_are_rejected_unanswered(voice):
    root = xml(voice.incoming(call(From="+19998887777")))
    assert root.find("Reject").get("reason") == "rejected"
    assert root.find("Gather") is None


def test_speech_is_captured_and_read_back(voice):
    speech = call(SpeechResult="Remind me to call mom tomorrow at 5 p.m.", Confidence="0.93")
    root = xml(voice.capture(speech))
    (item,) = voice.store.list()
    assert item.text == "Call mom"
    said = root.find("Say").text
    assert said.startswith("Got it: Call mom, ") and said.endswith(" at 5 PM.")
    assert METRICS.get("maestro_captures_total", source="voice") == 1


def test_recurring_and_notes_are_described(voice):
    def said(speech):
        return xml(voice.capture(call(SpeechResult=speech))).find("Say").text

    assert said("Pay rent on the 1st of every month.").endswith(", monthly on day 1.")
    note = said("Remember that the spare key is under the mat.")
    assert note == "Saved a note: The spare key is under the mat."


def test_nothing_heard(voice):
    root = xml(voice.capture(call(SpeechResult="  ")))
    assert "didn't catch that" in root.find("Say").text
    assert voice.store.list() == []


def test_strangers_cannot_capture(voice):
    root = xml(voice.capture(call(From="+19998887777", SpeechResult="delete everything")))
    assert root.find("Hangup") is not None
    assert voice.store.list() == []


def test_special_characters_are_escaped(voice):
    root = xml(voice.capture(call(SpeechResult="Buy fish & chips <large> at 7 p.m.")))
    assert "fish & chips <large>" in root.find("Say").text


@pytest.mark.parametrize("hour, minute, expected", [
    (17, 0, "Wednesday 28 October at 5 PM"),
    (9, 30, "Wednesday 28 October at 9:30 AM"),
    (0, 0, "Wednesday 28 October at 12 AM"),
    (12, 5, "Wednesday 28 October at 12:05 PM"),
])
def test_spoken_time(hour, minute, expected):
    assert spoken_time(datetime(2026, 10, 28, hour, minute, tzinfo=ZoneInfo("UTC"))) == expected


# --- through the server ------------------------------------------------------------------------

@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    store = Store()
    with TestClient(create_app(store, api_token="api", voice=VoiceHandler(store, TOKEN, OWNER),
                               public_url=PUBLIC)) as c:
        c.store = store
        yield c


def post(client, path, params, signature=None):
    signature = signature if signature is not None else twilio_signature(TOKEN, PUBLIC + path, params)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
    return client.post(path, content=urlencode(params), headers=headers)


def test_full_call_through_the_webhooks(client):
    answered = post(client, VOICE_PATH, call())
    assert answered.status_code == 200 and answered.headers["content-type"].startswith("application/xml")
    action = urljoin(VOICE_PATH, xml(answered.text).find("Gather").get("action"))  # as Twilio resolves it
    assert action == VOICE_CAPTURE_PATH
    captured = post(client, action, call(SpeechResult="Dentist on Friday at 10:30 a.m."))
    assert captured.status_code == 200
    assert "Got it: Dentist" in xml(captured.text).find("Say").text
    assert [i.text for i in client.store.list()] == ["Dentist"]


def test_voice_webhooks_require_a_valid_signature(client):
    for path in (VOICE_PATH, VOICE_CAPTURE_PATH):
        assert post(client, path, call(SpeechResult="hi"), signature="forged=").status_code == 403
    assert client.store.list() == []


def test_voice_webhooks_are_404_when_not_configured():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with TestClient(create_app(Store())) as c:
        assert c.post(VOICE_PATH, content="From=x").status_code == 404


def test_doctor_prints_both_webhook_urls(tmp_path):
    from maestro import doctor
    from maestro.config import Config

    lines = []
    doctor.run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                      twilio_auth_token="t", sms_to=OWNER, public_url=PUBLIC), out=lines.append)
    assert f"✓ inbound voice: set the number's voice webhook to {PUBLIC}{VOICE_PATH}" in lines


def test_a_retried_capture_is_saved_once(voice):
    params = call(SpeechResult="Call mom at 5 p.m.")
    first = xml(voice.capture(params)).find("Say").text
    second = xml(voice.capture(params)).find("Say").text  # Twilio retries the same CallSid
    assert first == second
    assert len(voice.store.list()) == 1
    assert METRICS.get("maestro_captures_total", source="voice") == 1


def test_low_confidence_is_not_saved(voice):
    unsure = call(SpeechResult="call bomb tomorrow at 15", Confidence="0.21")
    said = xml(voice.capture(unsure)).find("Say").text
    assert "not sure I heard that right" in said and "call bomb tomorrow at 15" in said
    assert voice.store.list() == []
    sure = call(SpeechResult="call mom at 5 p.m.", Confidence="0.85")
    assert xml(voice.capture(sure)).find("Say") is not None
    assert len(voice.store.list()) == 1


@pytest.mark.parametrize("verdict, answered", [
    ("TN-Validation-Passed-A", True),
    ("", True),                              # carrier didn't check: allowed
    ("TN-Validation-Failed-A", False),       # spoofed caller ID
    ("TN-Validation-Failed-B", False),
])
def test_calls_whose_caller_id_failed_verification_are_rejected(voice, verdict, answered):
    root = xml(voice.incoming(call(StirVerstat=verdict)))
    assert (root.find("Gather") is not None) == answered
    assert (root.find("Reject") is not None) == (not answered)
