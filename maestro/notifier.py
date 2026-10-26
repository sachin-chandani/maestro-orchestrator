"""Delivery channels for reminders that come due.

A channel is anything with `send(item)`. Built-in channels are registered in CHANNELS and
switched on by name with MAESTRO_CHANNELS, e.g. `MAESTRO_CHANNELS=console,ntfy,twilio_sms`.
"""

import base64
import platform
import re
import smtplib
import ssl
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol

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


class TwilioSMSNotifier:
    """SMS to your own phone through your Twilio account."""

    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_number: str,
                 opener: Opener = _default_opener):
        self.url = self.API.format(sid=account_sid)
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self.auth_header = f"Basic {credentials}"
        self.from_number = from_number
        self.to_number = to_number
        self.opener = opener

    def send(self, item: Item) -> None:
        body = urllib.parse.urlencode(
            {"From": self.from_number, "To": self.to_number,
             "Body": f"Reminder: {item.text} (reply done or snooze 1h)"}
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Authorization": self.auth_header,
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self.opener(request)


REMINDER_MESSAGE_ID_RE = re.compile(r"<reminder-(\d+)-\d+@maestro>")


def reminder_message_id(item: Item) -> str:
    """Message-ID for a reminder email. Replies quote it (In-Reply-To), which lets email-in act on
    exactly this reminder; see maestro.email_in."""
    stamp = int(item.due_at.timestamp()) if item.due_at else 0
    return f"<reminder-{item.id}-{stamp}@maestro>"


SmtpFactory = Callable[[Config], smtplib.SMTP]


def _smtp(config: Config) -> smtplib.SMTP:
    if config.smtp_security == "ssl":
        return smtplib.SMTP_SSL(config.smtp_host or "", config.smtp_port, timeout=15,
                                context=ssl.create_default_context())
    client = smtplib.SMTP(config.smtp_host or "", config.smtp_port, timeout=15)
    if config.smtp_security == "starttls":
        client.starttls(context=ssl.create_default_context())
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
        message["Subject"] = f"Reminder: {item.text}"
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


def _telegram(config: Config) -> Notifier:
    from maestro.telegram import TelegramClient, TelegramNotifier

    s = _require("telegram", {"TELEGRAM_BOT_TOKEN": config.telegram_bot_token,
                              "TELEGRAM_CHAT_ID": config.telegram_chat_id})
    return TelegramNotifier(TelegramClient.for_token(s["TELEGRAM_BOT_TOKEN"]), s["TELEGRAM_CHAT_ID"])


def _email(config: Config) -> Notifier:
    _require("email", {"SMTP_HOST": config.smtp_host, "SMTP_FROM": config.smtp_from,
                       "MAESTRO_EMAIL_TO": config.email_to})
    if config.smtp_security not in ("starttls", "ssl", "none"):
        raise ConfigError(f"SMTP_SECURITY must be starttls, ssl or none, not {config.smtp_security!r}")
    if config.smtp_security == "none" and config.smtp_password:
        raise ConfigError("SMTP_SECURITY=none would send SMTP_PASSWORD in clear text; use starttls or ssl")
    return EmailNotifier(config)


CHANNELS: dict[str, Callable[[Config], Notifier]] = {
    "console": lambda _: ConsoleNotifier(),
    "macos": lambda _: MacNotifier(),
    "ntfy": _ntfy,
    "twilio_sms": _twilio_sms,
    "telegram": _telegram,
    "email": _email,
}


def build_notifier(config: Config) -> MultiNotifier:
    unknown = [name for name in config.channels if name not in CHANNELS]
    if unknown:
        raise ConfigError(f"Unknown channel(s): {', '.join(unknown)}. Available: {', '.join(CHANNELS)}")
    return MultiNotifier({name: CHANNELS[name](config) for name in config.channels})


def default_channels() -> list[str]:
    return ["console", "macos"] if platform.system() == "Darwin" else ["console"]
