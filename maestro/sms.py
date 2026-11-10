"""Inbound SMS: text your Twilio number to capture reminders or run commands.

Point your Twilio number's "A message comes in" webhook at `<MAESTRO_PUBLIC_URL>/webhooks/twilio/sms`.
Every request is checked against Twilio's X-Twilio-Signature (HMAC-SHA1 with your auth token), and
only messages from your own number (MAESTRO_SMS_TO) are acted on. Replies go back as TwiML in the
webhook response, so no extra API call or credentials are needed to answer.
"""

import base64
import hashlib
import hmac
import logging
import re
from xml.sax.saxutils import escape

from maestro import captures as confirmations
from maestro import commands
from maestro.actions import apply
from maestro.interpret import Context, Interpreter, interpret_assessed
from maestro.models import Item, Kind
from maestro.observability import METRICS
from maestro.storage import Store

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/twilio/sms"
CHANNEL = "twilio_sms"  # replies act on the reminder last delivered on this channel


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_signature(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    """Constant-time check of an X-Twilio-Signature header."""
    if not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


def normalise_number(number: str) -> str:
    return re.sub(r"[^\d+]", "", number)


def twiml(message: str | None = None) -> str:
    body = f"<Message>{escape(message)}</Message>" if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


class TwilioHandler:
    """Shared by the SMS and voice webhooks: signature check and owner check."""

    kind = "message"

    def __init__(self, store: Store, auth_token: str, owner_number: str,
                 requestable: frozenset[str] | None = None,
                 interpreter: "Interpreter | None" = None,
                 confirm: "confirmations.Confirmation | None" = None):
        self.store = store
        self.auth_token = auth_token
        # How a capture is read; None means the rules (maestro.interpret). Injected, never global.
        self.interpreter = interpreter
        # Ask before acting on an unsure interpretation (#117); None means never, as before.
        self.confirm = confirm
        self.owner = normalise_number(owner_number)
        # The per-item channels this deployment can honour; None skips the capture-time check (#20).
        self.requestable = requestable

    def verify(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        return verify_signature(self.auth_token, url, params, signature)

    def save(self, text: str, external_id: str | None, source: str) -> tuple[Item, str]:
        """Read and save text from an inbound message, once per provider message/call id. Also
        returns a note for the sender when a chosen channel isn't configured (#20), else ""."""
        captured, _ = interpret_assessed(text, Context(tz=self.store.tz), self.interpreter)
        results = apply(self.store, captured, source=f"twilio_{source}", dedupe_key=external_id or None,
                        requestable=self.requestable)
        # One message usually asks for one thing. A model interpreter (#115) can read several out
        # of one, and they are all saved; the reply is about the first, and says how many there were.
        result = results[0]
        item, created, note = result.saved, result.created, ""
        if dropped := sorted({c for r in results for c in r.dropped}):
            kept = "the other requested channels" if item.channels else "the default channels"
            note = f"{', '.join(dropped)} isn't configured; using {kept}."
            log.warning("%s capture: %s", source, note)
        if len(results) > 1:
            note = f"Saved {len(results)} things. {note}".strip()
        if created:
            for _ in results:
                METRICS.inc("maestro_captures_total", source=source)
        else:
            log.info("Twilio retried %s %s; returning the item already saved", source, external_id)
        return item, note

    def is_owner(self, params: dict[str, str], field: str = "From") -> bool:
        """Whether the owner is on the other end: the sender, or for calls Maestro placed, `To`."""
        number = normalise_number(params.get(field, ""))
        if number == self.owner:
            return True
        log.warning("Ignoring %s with unknown number ending %s", self.kind, number[-4:])
        return False


class SmsHandler(TwilioHandler):
    kind = "SMS"

    def handle(self, params: dict[str, str]) -> str:
        """Process one verified inbound message and return the TwiML reply."""
        text = params.get("Body", "").strip()
        if not self.is_owner(params):
            return twiml()
        if not text:
            return twiml()

        if command := commands.parse_command(text):
            return twiml(commands.run(self.store, command, channel=CHANNEL))

        # "YES", "NO 12" or "UNDO" answers a confirmation — but only while the owner has a capture
        # the word could answer; otherwise it is captured as any other text (ADR 0005, #117).
        if (reply := confirmations.match_reply(text)) and (
                target := confirmations.target(self.store, CHANNEL, self.owner, *reply)):
            outcome, items, removed = confirmations.answer(self.store, target, reply[0], via=CHANNEL,
                                                           requestable=self.requestable)
            if outcome == confirmations.Outcome.APPLIED:  # ALREADY echoes items already counted
                for _ in items:
                    METRICS.inc("maestro_captures_total", source="sms")
            hint = " Reply UNDO within 10 min to take it back." \
                if outcome == confirmations.Outcome.APPLIED else ""
            return twiml(confirmations.worded(outcome, target, items, removed, hint))

        if self.confirm:
            captured, confidence = interpret_assessed(text, Context(tz=self.store.tz), self.interpreter)
            if confirmations.needs_confirmation(captured, confidence):
                proposal, created = confirmations.propose(
                    self.store, captured, confidence, CHANNEL, self.owner, text,
                    ttl=self.confirm.ttl, dedupe_key=params.get("MessageSid") or None)
                if not created:
                    log.info("Twilio retried sms %s; repeating the confirmation question",
                             params.get("MessageSid"))
                what = "; ".join(confirmations.proposed_lines(proposal))
                return twiml(f"Did you mean: {what}? Reply YES or NO (#{proposal.id}, "
                             f"expires {proposal.expires_at:%a %H:%M}).")

        item, note = self.save(text, params.get("MessageSid"), source="sms")
        hint = f" Reply 'done {item.id}' or 'snooze {item.id} 1h'." if item.kind == Kind.REMINDER else ""
        return twiml(f"Saved {commands.line(item)}.{hint}" + (f" Note: {note}" if note else ""))
