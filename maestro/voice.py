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
from maestro.sms import TwilioHandler

log = logging.getLogger(__name__)

VOICE_PATH = "/webhooks/twilio/voice"
VOICE_CAPTURE_PATH = "/webhooks/twilio/voice/capture"
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
