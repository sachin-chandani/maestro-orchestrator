"""Delivery channels for reminders that come due.

A channel is anything with `send(item)`. Built-in channels are registered in CHANNELS and
switched on by name with MAESTRO_CHANNELS, e.g. `MAESTRO_CHANNELS=console,ntfy,twilio_sms`.
"""

import base64
import platform
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
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
            {"From": self.from_number, "To": self.to_number, "Body": f"Reminder: {item.text}"}
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Authorization": self.auth_header,
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self.opener(request)


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


def _twilio_sms(config: Config) -> Notifier:
    required = {
        "TWILIO_ACCOUNT_SID": config.twilio_account_sid,
        "TWILIO_AUTH_TOKEN": config.twilio_auth_token,
        "TWILIO_FROM_NUMBER": config.twilio_from,
        "MAESTRO_SMS_TO": config.sms_to,
    }
    if missing := [name for name, value in required.items() if not value]:
        raise ConfigError(f"twilio_sms channel needs {', '.join(missing)}")
    return TwilioSMSNotifier(config.twilio_account_sid, config.twilio_auth_token,
                             config.twilio_from, config.sms_to)


CHANNELS: dict[str, Callable[[Config], Notifier]] = {
    "console": lambda _: ConsoleNotifier(),
    "macos": lambda _: MacNotifier(),
    "ntfy": _ntfy,
    "twilio_sms": _twilio_sms,
}


def build_notifier(config: Config) -> MultiNotifier:
    unknown = [name for name in config.channels if name not in CHANNELS]
    if unknown:
        raise ConfigError(f"Unknown channel(s): {', '.join(unknown)}. Available: {', '.join(CHANNELS)}")
    return MultiNotifier({name: CHANNELS[name](config) for name in config.channels})


def default_channels() -> list[str]:
    return ["console", "macos"] if platform.system() == "Darwin" else ["console"]
