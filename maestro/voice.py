"""Voice through Twilio, both ways.

Inbound: call your Twilio number and say what to remember. Point the number's "A call comes in"
webhook at `<MAESTRO_PUBLIC_URL>/webhooks/twilio/voice`. Maestro answers with a prompt and Twilio's
speech recognition (<Gather input="speech">); the transcript is posted, signed, to
`/webhooks/twilio/voice/capture`, parsed like any other capture, and read back. Calls from any number
other than MAESTRO_SMS_TO are rejected without being answered (no charge).

Outbound (the `twilio_call` channel): Maestro calls MAESTRO_SMS_TO and reads the reminder out. With
MAESTRO_PUBLIC_URL set, pressing 1 marks it done and 2 snoozes it for 10 minutes; the key press comes
back, signed, to `/webhooks/twilio/voice/reminder`, with the reminder and occurrence in the URL. That
route is served by `maestro serve` (any instance sharing the database), not by `maestro run`.
"""

import logging
import urllib.parse
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from maestro import commands, recurrence
from maestro.models import Item, Kind
from maestro.notifier import TwilioClient
from maestro.sms import TwilioHandler

log = logging.getLogger(__name__)

VOICE_PATH = "/webhooks/twilio/voice"
VOICE_CAPTURE_PATH = "/webhooks/twilio/voice/capture"
VOICE_REMINDER_PATH = "/webhooks/twilio/voice/reminder"
# key -> (command, its arguments, what to say once it's done)
KEYS = {"1": ("done", [], "Marked done. Goodbye."),
        "2": ("snooze", ["10m"], "Snoozed for 10 minutes. Goodbye.")}
MAX_SPOKEN = 300  # characters of reminder text read out; Twilio caps inline TwiML at 4000
# Relative to VOICE_PATH, so Twilio keeps any path prefix in MAESTRO_PUBLIC_URL (e.g. /maestro).
CAPTURE_ACTION = "voice/capture"
LANGUAGE = "en-US"
# Below this, Twilio's transcript is more likely wrong than right: ask again instead of saving it.
MIN_CONFIDENCE = 0.3
# STIR/SHAKEN results that mean the caller ID was checked by the carrier and failed.
FAILED_ATTESTATION = ("TN-Validation-Failed",)


def _twiml(*verbs: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(verbs)}</Response>'


def _say(text: str) -> str:
    return f"<Say language={quoteattr(LANGUAGE)}>{escape(text)}</Say>"


def spoken_time(when: datetime) -> str:
    """'Wednesday 28 October at 5 PM', or '5:30 PM' with minutes: easy for text-to-speech to read."""
    hour = when.hour % 12 or 12
    clock = f"{hour}:{when.minute:02d}" if when.minute else f"{hour}"
    return f"{when:%A} {when.day} {when:%B} at {clock} {'AM' if when.hour < 12 else 'PM'}"


def confirmation(item: Item) -> str:
    if item.kind == Kind.NOTE or item.due_at is None:
        return f"Saved a note: {item.text}."
    repeat = f", {recurrence.describe(item.recurrence)}" if item.recurrence else ""
    return f"Got it: {item.text}, {spoken_time(item.due_at)}{repeat}."


class VoiceHandler(TwilioHandler):
    """Caller ID can be spoofed. Calls whose caller ID failed the carrier's STIR/SHAKEN check
    (StirVerstat) are rejected; see the README for the residual risk."""

    kind = "call"

    def _is_trusted_owner(self, params: dict[str, str]) -> bool:
        if not self.is_owner(params):
            return False
        verdict = params.get("StirVerstat", "")
        if verdict.startswith(FAILED_ATTESTATION):
            log.warning("Rejecting a call from the owner's number whose caller ID failed verification (%s)",
                        verdict)
            return False
        return True

    def incoming(self, params: dict[str, str]) -> str:
        """Answer the owner's call with a prompt and speech recognition."""
        if not self._is_trusted_owner(params):
            return _twiml('<Reject reason="rejected"/>')
        gather = (f'<Gather input="speech" action={quoteattr(CAPTURE_ACTION)} method="POST" '
                  f'speechTimeout="auto" language={quoteattr(LANGUAGE)}>'
                  f"{_say('What should I remember?')}</Gather>")
        return _twiml(gather, _say("I didn't hear anything. Goodbye."), "<Hangup/>")

    def capture(self, params: dict[str, str]) -> str:
        """Save what was said (once per call) and read back how it was understood."""
        if not self._is_trusted_owner(params):
            return _twiml("<Hangup/>")
        speech = params.get("SpeechResult", "").strip()
        if not speech:
            return _twiml(_say("Sorry, I didn't catch that. Please call again."), "<Hangup/>")
        try:
            confidence = float(params.get("Confidence") or 1)
        except ValueError:
            confidence = 1.0
        if confidence < MIN_CONFIDENCE:
            return _twiml(_say(f"I'm not sure I heard that right. I heard: {speech}. Nothing was saved. "
                               "Please call again."), "<Hangup/>")
        item = self.save(speech, params.get("CallSid"), source="voice")
        return _twiml(_say(confirmation(item)), "<Hangup/>")


def reminder_action_url(public_url: str, item: Item) -> str:
    """Where the key press for this call goes. The reminder and occurrence are in the URL, which
    Twilio's signature covers, so they can't be altered."""
    stamp = int(item.due_at.timestamp()) if item.due_at else 0
    query = urllib.parse.urlencode({"item": item.id, "at": stamp})
    return f"{public_url.rstrip('/')}{VOICE_REMINDER_PATH}?{query}"


def reminder_twiml(item: Item, public_url: str | None) -> str:
    text = item.text if len(item.text) <= MAX_SPOKEN else item.text[:MAX_SPOKEN] + "..."
    if not public_url or not item.id:  # nowhere for key presses to go (or a test call): read it twice
        return _twiml(_say(f"Maestro reminder: {text}."), '<Pause length="1"/>',
                      _say(f"Again: {text}. Goodbye."), "<Hangup/>")
    prompt = _say(f"Maestro reminder: {text}. Press 1 to mark it done, or 2 to snooze it for 10 minutes.")
    gather = (f'<Gather input="dtmf" numDigits="1" timeout="8" method="POST" '
              f"action={quoteattr(reminder_action_url(public_url, item))}>{prompt}</Gather>")
    return _twiml(gather, _say("No key pressed. Goodbye."), "<Hangup/>")


class TwilioCallNotifier:
    """Calls your own phone and reads the reminder out. A placed call counts as delivered even if it
    isn't answered; pair it with escalation, or with another channel, for things that must not be missed."""

    def __init__(self, client: TwilioClient, from_number: str, to_number: str, public_url: str | None):
        self.client = client
        self.from_number = from_number
        self.to_number = to_number
        self.public_url = public_url

    def send(self, item: Item) -> None:
        self.client.post({"From": self.from_number, "To": self.to_number,
                          "Twiml": reminder_twiml(item, self.public_url)})


class CallReminderHandler(TwilioHandler):
    """The key press on a reminder call: acts only on the owner's call, about the reminder's latest
    delivered occurrence, at most once per call (commands.act_on_delivery)."""

    kind = "reminder call"

    def keypress(self, query: dict[str, str], params: dict[str, str]) -> str:
        if not self.is_owner(params, field="To"):  # Maestro placed this call: the owner was called
            return _twiml("<Hangup/>")
        try:
            item_id, stamp = int(query["item"]), int(query["at"])
        except (KeyError, ValueError):
            return _twiml(_say("Sorry, something went wrong. Goodbye."), "<Hangup/>")
        digit = params.get("Digits", "")
        if digit not in KEYS:
            return _twiml(_say("Nothing was changed. Goodbye."), "<Hangup/>")
        action, args, reply = KEYS[digit]
        key = params.get("CallSid") or f"{item_id}:{stamp}:{digit}"
        outcome, _ = commands.act_on_delivery(self.store, "call-keypress", key, item_id, stamp, action, args,
                                              via="twilio_call")
        if outcome == commands.ReplyOutcome.ALREADY_HANDLED:
            reply = "That was already handled. Goodbye."
        elif outcome == commands.ReplyOutcome.STALE:
            log.info("Ignoring a key press for reminder #%s: not its latest delivery", item_id)
            reply = "That reminder has changed since this call, so nothing was changed."
        else:
            log.info("Reminder call key %s on reminder #%s", digit, item_id)
        return _twiml(_say(reply), "<Hangup/>")
