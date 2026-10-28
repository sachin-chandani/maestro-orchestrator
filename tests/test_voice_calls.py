"""#11: reminders as phone calls to your own number, with 1 = done and 2 = snooze."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from maestro.config import Config
from maestro.escalation import build_stages
from maestro.models import Item, Kind, Status
from maestro.notifier import CHANNELS, ConfigError, MultiNotifier, TwilioClient
from maestro.scheduler import fire_due
from maestro.sms import twilio_signature
from maestro.store import Store
from maestro.voice import VOICE_REMINDER_PATH, TwilioCallNotifier, reminder_twiml

TOKEN, OWNER, TWILIO_NUMBER, PUBLIC = "s3cr3t", "+15552223333", "+15550001111", "https://maestro.example.com"
TWILIO = dict(twilio_account_sid="AC123", twilio_auth_token=TOKEN, twilio_from=TWILIO_NUMBER, sms_to=OWNER)


class Recorder:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return 201

    def calls(self):
        return [{k: v[0] for k, v in parse_qs(r.data.decode()).items()} for r in self.requests]


class Sink:
    def send(self, item):
        pass


def caller(public_url=PUBLIC):
    opener = Recorder()
    client = TwilioClient("AC123", TOKEN, "Calls", opener)
    return TwilioCallNotifier(client, TWILIO_NUMBER, OWNER, public_url), opener


def item(text="Take pills", item_id=7, due=datetime(2026, 10, 28, 9, 0, tzinfo=UTC)):
    return Item(id=item_id, text=text, kind=Kind.REMINDER, created_at=due, due_at=due)


def test_a_call_is_placed_to_your_own_number():
    notifier, opener = caller()
    notifier.send(item())
    (request,) = opener.requests
    assert request.full_url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json"
    assert request.get_header("Authorization").startswith("Basic ")
    (fields,) = opener.calls()
    assert (fields["From"], fields["To"]) == (TWILIO_NUMBER, OWNER)
    gather = ET.fromstring(fields["Twiml"]).find("Gather")
    assert gather.get("input") == "dtmf" and gather.get("numDigits") == "1"
    assert gather.get("action") == f"{PUBLIC}{VOICE_REMINDER_PATH}?item=7&at=1793178000"
    assert gather.find("Say").text == ("Maestro reminder: Take pills. Press 1 to mark it done, "
                                       "or 2 to snooze it for 10 minutes.")


def test_reminder_text_cannot_break_the_twiml():
    root = ET.fromstring(reminder_twiml(item('Fix </Say><Hangup/> & "quotes"'), PUBLIC))
    assert root.find("Gather/Say").text.startswith('Maestro reminder: Fix </Say><Hangup/> & "quotes".')


def test_long_reminders_are_cut_for_reading_out():
    said = ET.fromstring(reminder_twiml(item("x" * 1000), PUBLIC)).find("Gather/Say").text
    assert "x" * 300 + "..." in said and "x" * 301 not in said


def test_without_a_public_url_the_reminder_is_read_out_twice():
    root = ET.fromstring(reminder_twiml(item(), None))
    assert root.find("Gather") is None
    said = [s.text for s in root.findall("Say")]
    assert said == ["Maestro reminder: Take pills.", "Again: Take pills. Goodbye."]


def test_channel_config():
    with pytest.raises(ConfigError, match="twilio_call channel needs TWILIO_ACCOUNT_SID"):
        CHANNELS["twilio_call"](Config())
    built = CHANNELS["twilio_call"](Config(**TWILIO, public_url=PUBLIC))
    assert isinstance(built, TwilioCallNotifier) and built.public_url == PUBLIC


# --- the key press, through the signed webhook --------------------------------------------------

@pytest.fixture
def setup():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app
    from maestro.voice import VoiceHandler

    store = Store()
    now = datetime.now(UTC).replace(microsecond=0)
    reminder = store.add(Item(text="Take pills", kind=Kind.REMINDER, created_at=now, due_at=now,
                              recurrence=None))
    notifier, opener = caller()
    fire_due(store, MultiNotifier({"twilio_call": notifier}), now=now)
    app = create_app(store, voice=VoiceHandler(store, TOKEN, OWNER), public_url=PUBLIC)
    with TestClient(app) as client:
        yield store, client, reminder, action_of(opener)


def action_of(opener):
    return ET.fromstring(opener.calls()[-1]["Twiml"]).find("Gather").get("action")


_sids = iter(range(1, 10_000))


def press(client, action, digits, signature=None, **params):
    params = {"From": TWILIO_NUMBER, "To": OWNER, "CallSid": f"CA{next(_sids)}", "Digits": digits, **params}
    signature = signature or twilio_signature(TOKEN, action, params)
    parts = urlsplit(action)
    response = client.post(f"{parts.path}?{parts.query}", content=urlencode(params),
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "X-Twilio-Signature": signature})
    return response, (ET.fromstring(response.text).findtext("Say") if response.status_code == 200 else None)


def ack_via(store):
    return [r["via"] for r in store.conn.execute("SELECT via FROM acknowledgements")]


def test_pressing_1_marks_it_done(setup):
    store, client, reminder, action = setup
    response, said = press(client, action, "1")
    assert response.headers["content-type"].startswith("application/xml")
    assert said == "Marked done. Goodbye."
    assert store.get(reminder.id).status == Status.DONE
    assert ack_via(store) == ["twilio_call"]


def test_pressing_2_snoozes_for_ten_minutes(setup):
    store, client, reminder, action = setup
    before = datetime.now(UTC)
    _, said = press(client, action, "2")
    assert said == "Snoozed for 10 minutes. Goodbye."
    due = store.get(reminder.id).due_at
    assert timedelta(minutes=9) < due - before <= timedelta(minutes=10, seconds=2)
    assert ack_via(store) == ["twilio_call"]


def test_a_retried_key_press_acts_once(setup):
    store, client, reminder, action = setup
    press(client, action, "2", CallSid="CA-retry")
    first = store.get(reminder.id).due_at
    _, said = press(client, action, "2", CallSid="CA-retry")
    assert said == "That was already handled. Goodbye."
    assert store.get(reminder.id).due_at == first


def test_other_keys_change_nothing(setup):
    store, client, reminder, action = setup
    _, said = press(client, action, "9")
    assert said == "Nothing was changed. Goodbye."
    assert store.get(reminder.id).status != Status.DONE


def test_a_press_about_an_earlier_occurrence_is_ignored(setup):
    store, client, reminder, action = setup
    later = store.get(reminder.id).due_at + timedelta(days=1)
    store.reschedule(reminder.id, later)
    fire_due(store, MultiNotifier({"push": Sink()}), now=later)  # delivered again since that call
    _, said = press(client, action, "1")
    assert said == "That reminder has changed since this call, so nothing was changed."
    assert store.get(reminder.id).status != Status.DONE


def test_forged_or_altered_requests_are_refused(setup):
    store, client, reminder, action = setup
    assert press(client, action, "1", signature="forged")[0].status_code == 403
    signed_for_original = twilio_signature(TOKEN, action, {"From": TWILIO_NUMBER, "To": OWNER,
                                                           "CallSid": "CA1", "Digits": "1"})
    other = action.replace(f"item={reminder.id}", "item=999")
    assert press(client, other, "1", signature=signed_for_original, CallSid="CA1")[0].status_code == 403
    assert store.get(reminder.id).status != Status.DONE


def test_only_calls_to_the_owner_count(setup):
    store, client, reminder, action = setup
    response, said = press(client, action, "1", To="+19998887777")
    assert response.status_code == 200 and said is None
    assert store.get(reminder.id).status != Status.DONE


def test_calls_can_be_an_escalation_stage(monkeypatch):
    notifier, opener = caller()
    monkeypatch.setitem(CHANNELS, "twilio_call", lambda _config: notifier)
    store = Store()
    due = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    store.add(Item(text="Take pills", kind=Kind.REMINDER, created_at=due, due_at=due))
    stages = build_stages(Config(escalation="30m=twilio_call"))
    fire_due(store, MultiNotifier({"push": Sink()}), now=due, escalation=stages)
    fire_due(store, MultiNotifier({"push": Sink()}), now=due + timedelta(minutes=30), escalation=stages)
    said = ET.fromstring(opener.calls()[0]["Twiml"]).find("Gather/Say").text
    assert said.startswith("Maestro reminder: Still waiting: Take pills.")
