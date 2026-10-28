"""Delivery channels for reminders that come due.

A channel is anything with `send(item)`. Built-in channels are registered in CHANNELS and
switched on by name with MAESTRO_CHANNELS, e.g. `MAESTRO_CHANNELS=console,ntfy,twilio_sms`.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import platform
import re
import secrets
import smtplib
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.message import EmailMessage, Message
from typing import IO, Protocol

from maestro.commands import when
from maestro.config import Config
from maestro.models import Item

# (request) -> response; swapped out in tests so no network calls are made.
Opener = Callable[[urllib.request.Request], object]


def _default_opener(request: urllib.request.Request) -> object:
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


class Notifier(Protocol):
    def send(self, item: Item) -> None: ...


class ConfigError(ValueError):
    pass


class ConsoleNotifier:
    def send(self, item: Item) -> None:
        print(f"⏰ Reminder #{item.id}: {item.text}", flush=True)


class MacNotifier:
    """Native macOS notification via osascript. Text is passed as an argument, never interpolated."""

    SCRIPT = 'on run argv\ndisplay notification (item 1 of argv) with title "Maestro"\nend run'

    def send(self, item: Item) -> None:
        subprocess.run(["osascript", "-e", self.SCRIPT, item.text], check=False)


class NtfyNotifier:
    """Push notifications to phones and desktops via ntfy (https://ntfy.sh or self-hosted)."""

    def __init__(self, url: str, topic: str, opener: Opener = _default_opener):
        self.endpoint = f"{url}/{urllib.parse.quote(topic)}"
        self.opener = opener

    def send(self, item: Item) -> None:
        request = urllib.request.Request(
            self.endpoint,
            data=item.text.encode(),
            headers={"Title": "Maestro reminder", "Tags": "alarm_clock"},
            method="POST",
        )
        self.opener(request)


class TwilioClient:
    """POSTs to one Twilio REST resource of your account (Messages, Calls)."""

    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{resource}.json"

    def __init__(self, account_sid: str, auth_token: str, resource: str, opener: Opener = _default_opener):
        self.url = self.API.format(sid=account_sid, resource=resource)
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self.auth_header = f"Basic {credentials}"
        self.opener = opener

    def post(self, fields: dict[str, str]) -> None:
        request = urllib.request.Request(
            self.url,
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Authorization": self.auth_header,
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self.opener(request)


class TwilioSMSNotifier:
    """SMS to your own phone through your Twilio account."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_number: str,
                 opener: Opener = _default_opener):
        self.client = TwilioClient(account_sid, auth_token, "Messages", opener)
        self.from_number = from_number
        self.to_number = to_number

    def send(self, item: Item) -> None:
        self.client.post({"From": self.from_number, "To": self.to_number,
                          "Body": f"Reminder: {item.text} (reply done or snooze 1h)"})


# <reminder-<item id>-<occurrence, unix seconds>-<unique>@maestro>
REMINDER_MESSAGE_ID_RE = re.compile(r"<reminder-(\d+)-(\d+)-[0-9a-f]+@maestro>")


def reminder_message_id(item: Item) -> str:
    """Message-ID for a reminder email. Replies quote it (In-Reply-To), which tells email-in exactly
    which reminder and occurrence a reply is about. Unique per send: escalation re-sends the same
    occurrence, and mail providers drop a second message with a Message-ID they've seen."""
    stamp = int(item.due_at.timestamp()) if item.due_at else 0
    return f"<reminder-{item.id}-{stamp}-{secrets.token_hex(4)}@maestro>"


SmtpFactory = Callable[[Config], smtplib.SMTP]


def smtp_port(config: Config) -> int:
    """SMTP_PORT, or the conventional port for the security mode: 465 for ssl, 587 otherwise."""
    return config.smtp_port or (465 if config.smtp_security == "ssl" else 587)


def _smtp(config: Config) -> smtplib.SMTP:
    if config.smtp_security == "ssl":
        return smtplib.SMTP_SSL(config.smtp_host or "", smtp_port(config), timeout=15,
                                context=ssl.create_default_context())
    client = smtplib.SMTP(config.smtp_host or "", smtp_port(config), timeout=15)
    if config.smtp_security == "starttls":
        try:
            client.starttls(context=ssl.create_default_context())
        except BaseException:
            client.close()  # don't leak the socket when the server can't do TLS
            raise
    return client


class EmailNotifier:
    """Reminder emails over SMTP to MAESTRO_EMAIL_TO. Reply 'done' or 'snooze 1h' (with email-in on)."""

    def __init__(self, config: Config, connect: SmtpFactory = _smtp):
        self.config = config
        self.connect = connect

    def message(self, item: Item) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.config.smtp_from or ""
        message["To"] = self.config.email_to or ""
        # Header values can't contain line breaks; reminders captured from chat or the API can.
        message["Subject"] = "Reminder: " + " ".join(item.text.split())
        message["Message-ID"] = reminder_message_id(item)
        lines = [item.text, ""]
        if self.config.imap_host:
            lines.append("Reply 'done', or 'snooze 1h', on the first line to act on this reminder.")
        if self.config.public_url:
            lines.append(f"Open Maestro: {self.config.public_url}/")
        message.set_content("\n".join(lines))
        return message

    def send(self, item: Item) -> None:
        client = self.connect(self.config)
        try:
            if self.config.smtp_user and self.config.smtp_password:
                client.login(self.config.smtp_user, self.config.smtp_password)
            client.send_message(self.message(item))
        finally:
            try:
                client.quit()
            except smtplib.SMTPException:
                client.close()


WEBHOOK_FORMATS = ("json", "slack", "discord")
DISCORD_LIMIT = 2000  # characters per message; longer is rejected with 400
SLACK_LIMIT = 40000


def _is_local(host: str) -> bool:
    """Hosts on the owner's own network, where plain HTTP is acceptable (e.g. Home Assistant): single-label
    names (Docker Compose services), local suffixes, and non-public addresses (private, loopback,
    link-local, Tailscale's 100.64.0.0/10). `host` is a parsed hostname (no brackets or port)."""
    host = host.rstrip(".")
    if "." not in host or host.endswith((".local", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped  # ::ffff:8.8.8.8 is the public 8.8.8.8
    return not address.is_global


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would turn a redirected POST into a body-less GET (possibly to plain http) and call it a
    success. A webhook that redirects is a misconfiguration: fail so it's retried and visible."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: Message, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, f"refusing to follow redirect ({msg})", headers, fp)


_webhook_opener = urllib.request.build_opener(_RefuseRedirects())


def _post_without_redirects(request: urllib.request.Request) -> object:
    with _webhook_opener.open(request, timeout=10) as response:
        return response.status


def _redacted(url: str) -> str:
    """Webhook URLs often carry a secret in the path (Slack, Discord): show only scheme and host."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}/…" if parts.hostname else "(unparseable URL)"


def _slack_escape(text: str) -> str:
    # Slack reads <...> as links and mentions (<!channel>); & < > must be entities.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class WebhookNotifier:
    """POSTs each reminder as JSON to MAESTRO_WEBHOOK_URL.

    `json` sends {"event", "id", "text", "due_at", "when", "recurrence"}; `slack` and `discord` send
    the message shape those services' incoming webhooks expect. With MAESTRO_WEBHOOK_SECRET, the
    request carries X-Maestro-Timestamp and X-Maestro-Signature: sha256=HMAC(secret, "<timestamp>.<body>"),
    so a receiver can check it came from this server and reject old (replayed) requests.
    """

    def __init__(self, url: str, fmt: str = "json", secret: str | None = None,
                 opener: Opener = _post_without_redirects, clock: Callable[[], float] = time.time):
        self.url = url
        self.format = fmt
        self.secret = secret
        self.opener = opener
        self.clock = clock

    def payload(self, item: Item) -> dict[str, object]:
        if self.format == "slack":
            return {"text": _slack_escape(f"⏰ {item.text}")[:SLACK_LIMIT]}
        if self.format == "discord":
            # No @everyone/@here/role pings from reminder text.
            return {"content": f"⏰ {item.text}"[:DISCORD_LIMIT], "allowed_mentions": {"parse": []}}
        stamp = int(item.due_at.timestamp()) if item.due_at else 0
        return {"event": "reminder.due", "id": item.id, "text": item.text,
                # Same for every send of this occurrence (retries, escalation): receivers can de-duplicate
                # retries by (occurrence_id, text); escalations carry different text ("Still waiting: ...").
                "occurrence_id": f"{item.id}:{stamp}",
                "due_at": item.due_at.isoformat() if item.due_at else None, "when": when(item),
                "recurrence": item.recurrence}

    def send(self, item: Item) -> None:
        body = json.dumps(self.payload(item), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "maestro-orchestrator"}
        if self.secret:
            timestamp = str(int(self.clock()))
            digest = hmac.new(self.secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
            headers["X-Maestro-Timestamp"] = timestamp
            headers["X-Maestro-Signature"] = f"sha256={digest.hexdigest()}"
        self.opener(urllib.request.Request(self.url, data=body, headers=headers, method="POST"))


class MultiNotifier:
    """Named channels. The scheduler delivers to each separately so one failure doesn't re-send the rest."""

    def __init__(self, channels: dict[str, Notifier]):
        self.channels = channels

    def send(self, item: Item) -> None:
        for notifier in self.channels.values():
            notifier.send(item)


def _ntfy(config: Config) -> Notifier:
    if not config.ntfy_topic:
        raise ConfigError("ntfy channel needs NTFY_TOPIC")
    return NtfyNotifier(config.ntfy_url, config.ntfy_topic)


def _require(channel: str, settings: dict[str, str | None]) -> dict[str, str]:
    """The settings a channel needs, all present, or a ConfigError naming every missing one."""
    if missing := [name for name, value in settings.items() if not value]:
        raise ConfigError(f"{channel} channel needs {', '.join(missing)}")
    return {name: value for name, value in settings.items() if value}


def _twilio_sms(config: Config) -> Notifier:
    s = _require("twilio_sms", {
        "TWILIO_ACCOUNT_SID": config.twilio_account_sid,
        "TWILIO_AUTH_TOKEN": config.twilio_auth_token,
        "TWILIO_FROM_NUMBER": config.twilio_from,
        "MAESTRO_SMS_TO": config.sms_to,
    })
    return TwilioSMSNotifier(s["TWILIO_ACCOUNT_SID"], s["TWILIO_AUTH_TOKEN"],
                             s["TWILIO_FROM_NUMBER"], s["MAESTRO_SMS_TO"])


def _twilio_call(config: Config) -> Notifier:
    from maestro.voice import TwilioCallNotifier

    s = _require("twilio_call", {
        "TWILIO_ACCOUNT_SID": config.twilio_account_sid,
        "TWILIO_AUTH_TOKEN": config.twilio_auth_token,
        "TWILIO_FROM_NUMBER": config.twilio_from,
        "MAESTRO_SMS_TO": config.sms_to,
    })
    client = TwilioClient(s["TWILIO_ACCOUNT_SID"], s["TWILIO_AUTH_TOKEN"], "Calls")
    return TwilioCallNotifier(client, s["TWILIO_FROM_NUMBER"], s["MAESTRO_SMS_TO"], config.public_url)


def _telegram(config: Config) -> Notifier:
    from maestro.telegram import TelegramClient, TelegramNotifier

    s = _require("telegram", {"TELEGRAM_BOT_TOKEN": config.telegram_bot_token,
                              "TELEGRAM_CHAT_ID": config.telegram_chat_id})
    return TelegramNotifier(TelegramClient.for_token(s["TELEGRAM_BOT_TOKEN"]), s["TELEGRAM_CHAT_ID"])


def _email(config: Config) -> Notifier:
    _require("email", {"SMTP_HOST": config.smtp_host, "SMTP_FROM": config.smtp_from,
                       "MAESTRO_EMAIL_TO": config.email_to})
    if bool(config.smtp_user) != bool(config.smtp_password):
        raise ConfigError("set both SMTP_USER and SMTP_PASSWORD, or neither")
    if config.smtp_security not in ("starttls", "ssl", "none"):
        raise ConfigError(f"SMTP_SECURITY must be starttls, ssl or none, not {config.smtp_security!r}")
    if config.smtp_security == "none" and config.smtp_password:
        raise ConfigError("SMTP_SECURITY=none would send SMTP_PASSWORD in clear text; use starttls or ssl")
    return EmailNotifier(config)


def _webhook(config: Config) -> Notifier:
    url = _require("webhook", {"MAESTRO_WEBHOOK_URL": config.webhook_url})["MAESTRO_WEBHOOK_URL"]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"MAESTRO_WEBHOOK_URL must be an http(s) URL, not {_redacted(url)}")
    if parts.scheme == "http" and not _is_local(parts.hostname):
        raise ConfigError("MAESTRO_WEBHOOK_URL must use https unless it points at your own network")
    if config.webhook_format not in WEBHOOK_FORMATS:
        raise ConfigError(f"MAESTRO_WEBHOOK_FORMAT must be one of {', '.join(WEBHOOK_FORMATS)}")
    return WebhookNotifier(url, config.webhook_format, config.webhook_secret)


CHANNELS: dict[str, Callable[[Config], Notifier]] = {
    "console": lambda _: ConsoleNotifier(),
    "macos": lambda _: MacNotifier(),
    "ntfy": _ntfy,
    "twilio_sms": _twilio_sms,
    "twilio_call": _twilio_call,
    "telegram": _telegram,
    "email": _email,
    "webhook": _webhook,
}


def build_notifier(config: Config) -> MultiNotifier:
    unknown = [name for name in config.channels if name not in CHANNELS]
    if unknown:
        raise ConfigError(f"Unknown channel(s): {', '.join(unknown)}. Available: {', '.join(CHANNELS)}")
    return MultiNotifier({name: CHANNELS[name](config) for name in config.channels})


def default_channels() -> list[str]:
    return ["console", "macos"] if platform.system() == "Darwin" else ["console"]
