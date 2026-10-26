"""Inbound voice: call your Twilio number and say what to remember.

Point the number's "A call comes in" webhook at `<MAESTRO_PUBLIC_URL>/webhooks/twilio/voice`. Maestro
answers with a prompt and Twilio's speech recognition (<Gather input="speech">); the transcript is
posted, signed, to `/webhooks/twilio/voice/capture`, parsed like any other capture, and read back.
Calls from any number other than MAESTRO_SMS_TO are rejected without being answered (no charge).
"""

import logging
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from maestro import recurrence
from maestro.models import Item, Kind
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.sms import normalise_number, verify_signature
from maestro.store import Store

log = logging.getLogger(__name__)

VOICE_PATH = "/webhooks/twilio/voice"
VOICE_CAPTURE_PATH = "/webhooks/twilio/voice/capture"
LANGUAGE = "en-US"


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


class VoiceHandler:
    def __init__(self, store: Store, auth_token: str, owner_number: str):
        self.store = store
        self.auth_token = auth_token
        self.owner = normalise_number(owner_number)

    def verify(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        return verify_signature(self.auth_token, url, params, signature)

    def _is_owner(self, params: dict[str, str]) -> bool:
        caller = normalise_number(params.get("From", ""))
        if caller == self.owner:
            return True
        log.warning("Rejecting call from unknown number ending %s", caller[-4:])
        return False

    def incoming(self, params: dict[str, str]) -> str:
        """Answer the owner's call with a prompt and speech recognition."""
        if not self._is_owner(params):
            return _twiml('<Reject reason="rejected"/>')
        gather = (f'<Gather input="speech" action={quoteattr(VOICE_CAPTURE_PATH)} method="POST" '
                  f'speechTimeout="auto" language={quoteattr(LANGUAGE)}>'
                  f"{_say('What should I remember?')}</Gather>")
        return _twiml(gather, _say("I didn't hear anything. Goodbye."), "<Hangup/>")

    def capture(self, params: dict[str, str]) -> str:
        """Save what was said and read back how it was understood."""
        if not self._is_owner(params):
            return _twiml("<Hangup/>")
        speech = params.get("SpeechResult", "").strip()
        if not speech:
            return _twiml(_say("Sorry, I didn't catch that. Please call again."), "<Hangup/>")
        METRICS.inc("maestro_captures_total", source="voice")
        item = self.store.add(parse(speech, tz=self.store.tz))
        return _twiml(_say(confirmation(item)), "<Hangup/>")
