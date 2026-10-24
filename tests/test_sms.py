from urllib.parse import urlencode

import pytest

from maestro.commands import parse_command
from maestro.models import Status
from maestro.observability import METRICS
from maestro.sms import WEBHOOK_PATH, SmsHandler, twilio_signature, twiml
from maestro.store import Store

TOKEN = "s3cr3t"
OWNER = "+15552223333"


def test_signature_matches_twilios_sdk():
    # Expected values computed with twilio-python's RequestValidator.compute_signature.
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert twilio_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params) \
        == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    sms = {"From": OWNER, "To": "+15550001111", "Body": "call mom tomorrow at 5pm & buy milk",
           "MessageSid": "SM1"}
    assert twilio_signature(TOKEN, "https://maestro.example.com/webhooks/twilio/sms", sms) \
        == "9Xkx/Ly/dRU8H5CYRPoQ6+wmXe8="


@pytest.mark.parametrize("text, expected", [
    ("list", ("list", [])),
    ("/list@MaestroBot", ("list", [])),
    ("Done 3", ("done", ["3"])),
    ("done #3", ("done", ["3"])),
    ("delete 4", ("rm", ["4"])),
    ("snooze 3 1h", ("snooze", ["3", "1h"])),
    ("help", ("help", [])),
    ("done with taxes by friday", None),
    ("snooze 3", None),
    ("list my errands", None),
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.fixture
def handler():
    return SmsHandler(Store(), TOKEN, OWNER)


def sms(body, sender=OWNER):
    return {"From": sender, "To": "+15550001111", "Body": body, "MessageSid": "SM1"}


def test_capture_replies_with_twiml(handler):
    reply = handler.handle(sms("call mom tomorrow at 5pm & bring cake"))
    (item,) = handler.store.list()
    assert item.text == "Call mom & bring cake"
    assert reply.startswith('<?xml version="1.0" encoding="UTF-8"?><Response><Message>')
    assert "Saved ⏰ #1 Call mom &amp; bring cake" in reply  # XML-escaped
    assert "Reply 'done 1' or 'snooze 1 1h'." in reply
    assert METRICS.get("maestro_captures_total", source="sms") == 1


def test_commands(handler):
    handler.handle(sms("call mom tomorrow at 5pm"))
    assert "#1 Call mom" in handler.handle(sms("list"))
    assert "Snoozed" in handler.handle(sms("snooze 1 2h"))
    assert "Done: Call mom" in handler.handle(sms("done 1"))
    assert handler.store.get(1).status == Status.DONE
    assert "No item #9" in handler.handle(sms("rm 9"))
    assert "Send anything to remember" in handler.handle(sms("help"))


def test_strangers_and_empty_messages_get_no_reply(handler):
    assert handler.handle(sms("call mom at 5pm", sender="+19998887777")) == twiml()
    assert handler.handle(sms("   ")) == twiml()
    assert handler.store.list() == []


def test_owner_number_formatting_is_ignored():
    handler = SmsHandler(Store(), TOKEN, "+1 (555) 222-3333")
    handler.handle(sms("buy milk"))
    assert len(handler.store.list()) == 1


# --- webhook endpoint -------------------------------------------------------------------------

PUBLIC = "https://maestro.example.com"


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    store = Store()
    with TestClient(create_app(store, api_token="api", sms=SmsHandler(store, TOKEN, OWNER),
                               public_url=PUBLIC)) as c:
        c.store = store
        yield c


def post(client, params, signature=None, url=PUBLIC + WEBHOOK_PATH):
    signature = signature if signature is not None else twilio_signature(TOKEN, url, params)
    return client.post(WEBHOOK_PATH, content=urlencode(params),
                       headers={"Content-Type": "application/x-www-form-urlencoded",
                                "X-Twilio-Signature": signature})


def test_webhook_accepts_signed_requests_without_api_token(client):
    res = post(client, sms("dentist next tuesday at 3pm"))
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/xml")
    assert "Saved ⏰ #1 Dentist" in res.text
    assert [i.text for i in client.store.list()] == ["Dentist"]


def test_webhook_rejects_bad_or_missing_signatures(client):
    assert post(client, sms("buy milk"), signature="forged=").status_code == 403
    assert post(client, sms("buy milk"), signature="").status_code == 403
    # Signed for a different URL (e.g. proxy rewrote the host): rejected.
    assert post(client, sms("buy milk"), url="http://localhost:8000" + WEBHOOK_PATH).status_code == 403
    # Body tampered with after signing.
    params = sms("buy milk")
    signature = twilio_signature(TOKEN, PUBLIC + WEBHOOK_PATH, params)
    assert post(client, {**params, "Body": "delete everything"}, signature=signature).status_code == 403
    assert client.store.list() == []


def test_webhook_is_404_when_not_configured():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with TestClient(create_app(Store())) as c:
        assert c.post(WEBHOOK_PATH, content="Body=hi").status_code == 404
