"""`maestro doctor`: check a deployment's configuration, and optionally send a test on every channel.

This is the one place real credentials are exercised; the rest of the code base is tested with fakes.
Exit status is non-zero when any check fails, so it can gate a deploy.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from maestro.config import Config
from maestro.models import Item, Kind
from maestro.notifier import CHANNELS, ConfigError, Notifier
from maestro.sms import WEBHOOK_PATH
from maestro.store import MIGRATIONS, Store
from maestro.tz import get_timezone

OK, WARN, FAIL = "ok", "warn", "fail"
SYMBOLS = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{SYMBOLS[self.status]} {self.name}" + (f": {self.detail}" if self.detail else "")


def _timezone(config: Config) -> Check:
    try:
        tz = get_timezone(config.timezone)
    except ValueError as e:
        return Check(FAIL, "timezone", str(e))
    source = "MAESTRO_TIMEZONE" if config.timezone else "system default"
    return Check(OK, "timezone", f"{tz.key} ({source})")


def _database(config: Config) -> Check:
    path = config.db_path
    try:
        store = Store(path, tz=get_timezone(config.timezone))
    except (sqlite3.Error, OSError, ValueError) as e:
        return Check(FAIL, "database", f"{path}: {e}")
    if store.schema_version != len(MIGRATIONS):
        return Check(FAIL, "database", f"schema {store.schema_version}, expected {len(MIGRATIONS)}")
    total = sum(store.counts().values())
    where = "in memory" if path == ":memory:" else str(Path(path).expanduser())
    return Check(OK, "database", f"{where}, schema {store.schema_version}, {total} items")


def _api_token(config: Config) -> Check:
    if config.api_token:
        return Check(OK, "API token", "set")
    return Check(WARN, "API token", "not set: fine on localhost, required before exposing the server")


def _telegram_bot(config: Config) -> list[Check]:
    if not config.telegram_bot_token:
        return []
    if not config.telegram_chat_id:
        return [Check(WARN, "telegram bot", "TELEGRAM_CHAT_ID not set: send /start to the bot to get it")]
    return [Check(OK, "telegram bot", f"answers chat {config.telegram_chat_id}")]


def _inbound_sms(config: Config) -> list[Check]:
    if not (config.twilio_auth_token and config.sms_to):
        return []
    if not config.public_url:
        return [Check(WARN, "inbound SMS", "set MAESTRO_PUBLIC_URL so Twilio webhook signatures can be "
                                           "verified behind a proxy")]
    return [Check(OK, "inbound SMS", f"point Twilio's webhook at {config.public_url}{WEBHOOK_PATH}")]


def _channels(config: Config) -> tuple[list[Check], dict[str, Notifier]]:
    checks, built = [], {}
    if not config.channels:
        return [Check(FAIL, "channels", "MAESTRO_CHANNELS is empty")], built
    for name in config.channels:
        factory = CHANNELS.get(name)
        if factory is None:
            checks.append(Check(FAIL, f"channel {name}", f"unknown; available: {', '.join(CHANNELS)}"))
            continue
        try:
            built[name] = factory(config)
        except ConfigError as e:
            checks.append(Check(FAIL, f"channel {name}", str(e)))
        else:
            checks.append(Check(OK, f"channel {name}", "configured"))
    return checks, built


def _send_tests(built: dict[str, Notifier]) -> list[Check]:
    test = Item(id=0, text="Maestro test message: if you can read this, the channel works",
                kind=Kind.REMINDER, created_at=datetime.now())
    checks = []
    for name, channel in built.items():
        try:
            channel.send(test)
        except Exception as e:
            checks.append(Check(FAIL, f"send {name}", f"{type(e).__name__}: {e}"))
        else:
            checks.append(Check(OK, f"send {name}", "delivered"))
    return checks


def run(config: Config, send_test: bool = False, out: Callable[[str], None] = print) -> bool:
    """Run every check, print each result, and return True when nothing failed."""
    checks = [_timezone(config), _database(config), _api_token(config), *_telegram_bot(config),
              *_inbound_sms(config)]
    channel_checks, built = _channels(config)
    checks += channel_checks
    if send_test:
        checks += _send_tests(built)

    for check in checks:
        out(str(check))
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    hint = "" if send_test else " Run with --send-test to deliver a test message on each channel."
    out("")
    out(f"{failed} failed, {warned} warning{'' if warned == 1 else 's'}.{hint}")
    return failed == 0
