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

from maestro import commands
from maestro.models import Kind
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.store import Store

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/twilio/sms"
CHANNEL = "twilio_sms"  # replies act on the reminder last delivered on this channel


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _digits(number: str) -> str:
    return re.sub(r"[^\d+]", "", number)


def twiml(message: str | None = None) -> str:
    body = f"<Message>{escape(message)}</Message>" if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


class SmsHandler:
    def __init__(self, store: Store, auth_token: str, owner_number: str):
        self.store = store
        self.auth_token = auth_token
        self.owner = _digits(owner_number)

    def verify(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(twilio_signature(self.auth_token, url, params), signature)

    def handle(self, params: dict[str, str]) -> str:
        """Process one verified inbound message and return the TwiML reply."""
        sender = _digits(params.get("From", ""))
        text = params.get("Body", "").strip()
        if sender != self.owner:
            log.warning("Ignoring SMS from unknown number ending %s", sender[-4:])
            return twiml()
        if not text:
            return twiml()

        if command := commands.parse_command(text):
            return twiml(commands.run(self.store, command, channel=CHANNEL))

        METRICS.inc("maestro_captures_total", source="sms")
        item = self.store.add(parse(text, tz=self.store.tz))
        hint = f" Reply 'done {item.id}' or 'snooze {item.id} 1h'." if item.kind == Kind.REMINDER else ""
        return twiml(f"Saved {commands.line(item)}.{hint}")
