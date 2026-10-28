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
from maestro.voice import VOICE_PATH

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
    # Opening the store migrates it and switches it to WAL, exactly as the server would at startup;
    # doctor is a pre-deploy check, so it deliberately validates the database in its final state.
    path = config.db_path
    try:
        store = Store(path, tz=get_timezone(config.timezone))
    except (sqlite3.Error, OSError, ValueError) as e:
        return Check(FAIL, "database", f"{path}: {e}")
    try:
        with store:
            version, total, journal = store.schema_version, sum(store.counts().values()), store.journal_mode
    except sqlite3.Error as e:  # e.g. "database is locked" by another process
        return Check(FAIL, "database", f"{path}: {e}")
    if version != len(MIGRATIONS):
        return Check(FAIL, "database", f"schema {version}, expected {len(MIGRATIONS)}")
    where = "in memory" if path == ":memory:" else str(Path(path).expanduser())
    items = f"{total} item{'' if total == 1 else 's'}"
    detail = f"{where}, schema {version}, {journal} journal, {items}"
    if path != ":memory:" and journal != "wal":
        return Check(WARN, "database", f"{detail}: not in WAL mode; keep MAESTRO_DB on a local disk "
                                       "or Docker volume, not a network share")
    return Check(OK, "database", detail)


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
        return [Check(WARN, "inbound SMS and voice", "set MAESTRO_PUBLIC_URL so Twilio webhook signatures "
                                                     "can be verified behind a proxy")]
    base = config.public_url.rstrip("/")
    return [Check(OK, "inbound SMS", f"set the number's messaging webhook to {base}{WEBHOOK_PATH}"),
            Check(OK, "inbound voice", f"set the number's voice webhook to {base}{VOICE_PATH}")]


def _email_in(config: Config, connect: bool) -> list[Check]:
    from maestro.config import email_settings
    from maestro.email_in import EmailPoller

    try:
        settings = email_settings(config)
    except ValueError as e:
        return [Check(FAIL, "email-in", str(e))]
    if settings is None:
        return []
    checks = []
    if not settings.require_dmarc:
        checks.append(Check(WARN, "email-in", "MAESTRO_EMAIL_REQUIRE_DMARC=false: anyone can forge an "
                                              "allowed sender; only use with a private mail server"))
    if not connect:
        return [*checks, Check(OK, "email-in", f"{settings.user} on {settings.host} ({settings.folder}), "
                                               f"senders: {', '.join(sorted(settings.allowed_senders))}")]
    try:
        with Store() as scratch:  # only counts unread mail (one SEARCH); nothing is fetched or saved
            unread = EmailPoller(scratch, settings).count_unread()
    except Exception as e:
        return [*checks, Check(FAIL, "email-in", f"IMAP login/fetch failed: {type(e).__name__}: {e}")]
    return [*checks, Check(OK, "email-in", f"connected to {settings.host}, {unread} unread")]


def _escalation(config: Config) -> list[Check]:
    if not config.escalation:
        return []
    from maestro.escalation import build_stages

    try:
        stages = build_stages(config)
    except ConfigError as e:
        return [Check(FAIL, "escalation", str(e))]
    plan = "; ".join(f"after {int(s.delay.total_seconds() // 60)}m: {', '.join(s.names)}" for s in stages)
    return [Check(OK, "escalation", plan)]


def _quiet_hours(config: Config) -> list[Check]:
    from maestro.quiet import parse_quiet_hours

    try:
        quiet = parse_quiet_hours(config.quiet_hours)
    except ConfigError as e:
        return [Check(FAIL, "quiet hours", str(e))]
    if quiet is None:
        return []
    return [Check(OK, "quiet hours", f"{quiet}: non-urgent reminders wait; start one with ! for urgent")]


def _channels(config: Config) -> tuple[list[Check], dict[str, Notifier]]:
    checks: list[Check] = []
    built: dict[str, Notifier] = {}
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
            detail = "configured"
            if name == "twilio_call" and not config.public_url:
                detail = "configured; calls can't offer press 1/2 without MAESTRO_PUBLIC_URL"
            checks.append(Check(OK, f"channel {name}", detail))
    return checks, built


def _send_tests(built: dict[str, Notifier]) -> list[Check]:
    now = datetime.now().astimezone()
    test = Item(id=0, text="Maestro test message: if you can read this, the channel works",
                kind=Kind.REMINDER, created_at=now, due_at=now)
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
              *_inbound_sms(config), *_email_in(config, connect=send_test), *_escalation(config),
              *_quiet_hours(config)]
    channel_checks, built = _channels(config)
    checks += channel_checks
    # Channels used only for escalation still need a test message, or a broken one is found mid-escalation.
    if config.escalation:
        from maestro.escalation import build_stages

        try:
            for stage in build_stages(config):
                built.update({name: notifier for name, notifier in zip(stage.names, stage.channels.values(),
                                                                         strict=True) if name not in built})
        except ConfigError:
            pass  # already reported by _escalation
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
