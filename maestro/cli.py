"""Command-line interface: `maestro add "call mom tomorrow at 5pm"`, `maestro list`, `maestro run`."""

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from maestro import captures as confirmations
from maestro import commands, gaps, recurrence
from maestro.actions import apply
from maestro.config import Config, load_dotenv
from maestro.interpret import Context, Interpreter, build_interpreter, interpret_assessed
from maestro.models import Item, Kind, Status
from maestro.notifier import (
    ConfigError,
    Notifier,
    build_notifier,
    default_channels,
    requestable_channels,
)
from maestro.observability import configure_logging
from maestro.parser import parse_duration
from maestro.scheduler import Lease, fire_due, run_forever
from maestro.storage import (
    SchemaTooNewError,
    Store,
    StoreConfigError,
    driver_errors,
    open_store,
    parse_url,
)
from maestro.tz import add_elapsed, get_timezone

if TYPE_CHECKING:
    from maestro.actions import Action
    from maestro.confidence import Confidence
    from maestro.email_in import EmailWorker
    from maestro.escalation import Stage
    from maestro.quiet import QuietHours
    from maestro.telegram import TelegramBot


def _format(item: Item, verbose: bool = False) -> str:
    next_time = item.snoozed_until or item.due_at  # a snoozed occurrence goes out before the series' next
    when = next_time.strftime("%a %d %b %H:%M") if next_time else "—"
    tag = commands.KIND_ICONS.get(item.kind, "📝")
    urgent = "❗ " if item.urgent else ""
    repeat = f" ({recurrence.describe(item.recurrence)})" if item.recurrence else ""
    via = f" [via {', '.join(item.channels)}]" if item.channels else ""
    status = "" if item.status == Status.PENDING else f" [{item.status}]"
    line = f"{item.id:>4}  {tag} {when:<16}  {urgent}{item.text}{repeat}{via}{status}"
    if verbose:  # which interpreter read it (#116); items from before it was recorded say so
        line += f"\n{'':>6}read by {item.interpreter or 'unknown (saved before this was recorded)'}"
    return line


# Reminders, then tasks, then notes: `store.list` already returns them in this order (#114).
_GROUPS = ((Kind.REMINDER, "Reminders"), (Kind.TASK, "Tasks"), (Kind.NOTE, "Notes"))


def _grouped(items: list[Item], verbose: bool = False) -> str:
    """The list under one heading per kind, so tasks are their own section and not lost among
    notes. A kind nobody has captured gets no heading.

    Items still needing a look (#126) get a section of their own, last: they are saved and visible,
    but none of them will go out until someone answers, and mixing them in would hide that.
    """
    sections = []
    for kind, title in _GROUPS:
        if lines := [_format(i, verbose) for i in items if i.kind == kind and i.gap is None]:
            sections.append("\n".join([title, *lines]))
    if waiting := [gaps.line(i) for i in items if i.gap is not None]:
        sections.append("\n".join(["Needs a look", *waiting]))
    return "\n\n".join(sections)


def _confirmation(config: Config) -> "confirmations.Confirmation | None":
    from maestro.config import confirmation

    return confirmation(config)


def _propose(store: Store, captured: "list[Action]", confidence: "Confidence", text: str,
             confirm: "confirmations.Confirmation") -> int:
    """Hold an unsure capture for confirmation (#117): ask right here when on a terminal,
    otherwise print how to answer and leave it pending."""
    proposal, _ = confirmations.propose(store, captured, confidence, "cli", "", text,
                                        ttl=confirm.ttl)
    print(confirmations.question(proposal))
    if sys.stdin is not None and sys.stdin.isatty():
        reply = input("Save it? [Y/n] ").strip().lower()
        verb = "no" if reply in ("n", "no") else "yes"
        outcome, items, removed = confirmations.answer(store, proposal, verb, via="cli")
        hint = f" (undo with: maestro undo {proposal.id})" \
            if outcome == confirmations.Outcome.APPLIED else ""
        print(confirmations.worded(outcome, proposal, items, removed, hint))
        return 0
    print(f"Answer with: maestro confirm {proposal.id}  |  maestro cancel {proposal.id}")
    return 0


def _add_split(store: Store, captured: "list[Action]", confidence: "Confidence", text: str,
               requestable: "frozenset[str] | None") -> int:
    """`maestro add` under `MAESTRO_CONFIRM=split`: save what is clear, list what still needs an
    answer, and say how to undo the lot (#126)."""
    split = gaps.split_apply(store, captured, confidence, "cli", "", text, requestable=requestable)
    if split.dropped:
        chosen = {c for i in split.saved for c in (i.channels or [])}
        kept = "the other requested channels will be used" if chosen else "using the default channels"
        print(f"Note: {', '.join(split.dropped)} isn't configured; {kept}.", file=sys.stderr)
    for item in split.added:
        print(f"Saved: {_format(item).strip()}")
    for item in split.needs_look:
        wanted = gaps.WANTED.get(str(item.gap), "an answer")
        print(f"Needs {wanted}: {item.text} (maestro fill {item.id} \"…\")")
    # The undo token exists for every capture, but it is only worth a line when the capture did
    # something the person might not have expected. One plain reminder prints what it always has —
    # and its number, which `maestro rm` takes.
    if split.added and (len(split.saved) > 1 or split.needs_look):
        print(f"Undo all of it within 10 minutes with: maestro undo {split.undo_id}")
    return 0


def _list_needs_look(store: Store) -> int:
    """`maestro list --needs-look`: readings saved but waiting for an answer (#126)."""
    rows = store.needs_look()
    if not rows:
        print("Nothing needs a look.")
        return 0
    for item in rows:
        print(gaps.line(item))
    return 0


def _answer_gap(store: Store, command: str, item_id: int, phrase: str,
                requestable: "frozenset[str] | None" = None) -> int:
    """`maestro fill <id> <phrase>` / `maestro dismiss <id>`: answer one question by naming it."""
    if (item := store.get(item_id)) is None or item.gap is None:
        print(f"No item #{item_id} needing a look", file=sys.stderr)
        return 1
    if command == "dismiss":
        gaps.dismiss(store, item)
        print(f"Dismissed: {item.text}")
        return 0
    if (filled := gaps.fill(store, item, phrase, requestable)) is None:
        wanted = gaps.WANTED.get(item.gap, "an answer")
        print(f"Couldn't read {wanted} in {phrase!r}", file=sys.stderr)
        return 1
    print(f"Scheduled: {_format(filled).strip()}")
    return 0


def _list_proposals(store: Store) -> int:
    confirmations.expire(store)  # so what is listed as pending really still is
    from maestro.models import CaptureStatus

    rows = [*store.captures(status=str(CaptureStatus.PENDING)),
            *store.captures(status=str(CaptureStatus.EXPIRED))]
    if not rows:
        print("No captures awaiting confirmation.")
        return 0
    for capture in rows:
        print(confirmations.line(capture))
    return 0


def _devices(store: Store, args: argparse.Namespace, tz: ZoneInfo) -> int:
    """`maestro devices list|add|revoke` (#108, ADR 0018)."""
    from maestro import devices

    now = datetime.now(tz).replace(microsecond=0)
    if args.devices_command == "add":
        device, token = devices.register(store, " ".join(args.name), now)
        print(f"Added device #{device.id} ({device.name}). Its token, shown only now:\n{token}")
        return 0
    if args.devices_command == "revoke":
        if not store.revoke_device(args.id, now):
            print(f"No device #{args.id}", file=sys.stderr)
            return 1
        print(f"Revoked device #{args.id}; its token no longer works.")
        return 0
    rows = store.devices()
    if not rows:
        print("No devices signed in. Sign in from the web app, or: maestro devices add <name>")
        return 0
    for device in rows:
        seen = device.last_seen_at.strftime("%a %d %b %H:%M") if device.last_seen_at else "never"
        print(f"#{device.id}  {device.name}  (added {device.created_at:%d %b %Y}, last seen {seen})")
    return 0


def _decide(store: Store, command: str, capture_id: int) -> int:
    proposal = store.get_capture(capture_id)
    if proposal is None:
        print(f"No capture #{capture_id}", file=sys.stderr)
        return 1
    verb = {"confirm": "yes", "cancel": "no", "undo": "undo"}[command]
    outcome, items, removed = confirmations.answer(store, proposal, verb, via="cli")
    hint = f" (undo with: maestro undo {capture_id})" \
        if outcome == confirmations.Outcome.APPLIED else ""
    print(confirmations.worded(outcome, proposal, items, removed, hint))
    return 0 if outcome in (confirmations.Outcome.APPLIED, confirmations.Outcome.CANCELLED,
                            confirmations.Outcome.UNDONE, confirmations.Outcome.ALREADY) else 1


def _build_parser(config: Config) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maestro", description="Your second brain for things to remember.")
    p.add_argument("--db", default=config.db_path,
                   help="database path or URL (env: MAESTRO_DB_URL, MAESTRO_DB)")
    sub = p.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help='capture something, e.g. "pay rent on the 1st of every month"')
    add.add_argument("text", nargs="+")

    ls = sub.add_parser("list", help="show reminders and notes")
    ls.add_argument("--all", action="store_true", help="include completed items")
    ls.add_argument("--verbose", "-v", action="store_true",
                    help="also show which interpreter read each item (rules or model)")
    ls.add_argument("--proposals", action="store_true",
                    help="show captures awaiting confirmation (and recently expired ones) instead")
    ls.add_argument("--needs-look", action="store_true",
                    help="show readings that are saved but still need a time, a description or a channel")

    fill = sub.add_parser("fill", help='answer what an item needs, e.g. `fill 3 "tomorrow 9am"`')
    fill.add_argument("id", type=int)
    fill.add_argument("phrase", nargs="+")

    dismissed = sub.add_parser("dismiss", help="drop an item that needs a look instead of answering it")
    dismissed.add_argument("id", type=int)

    for name, help_text in [("confirm", "save a capture that is awaiting confirmation"),
                            ("cancel", "turn down a capture that is awaiting confirmation"),
                            ("undo", "take back a capture confirmed in the last 10 minutes")]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("capture_id", type=int)

    search = sub.add_parser("search", help="find items by text")
    search.add_argument("query")

    for name, help_text in [("done", "mark an item as done (a repeating one: this time only)"),
                            ("stop", "end a repeating reminder"), ("rm", "delete an item")]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("id", type=int)

    snooze = sub.add_parser("snooze", help="push a reminder back, e.g. `snooze 3 10m`")
    snooze.add_argument("id", type=int)
    snooze.add_argument("duration", help="10m, 2h, 1d")

    run = sub.add_parser("run", help="fire reminders as they come due (env: MAESTRO_CHANNELS)")
    run.add_argument("--interval", type=float, default=30.0, help="seconds between checks")
    run.add_argument("--once", action="store_true", help="check once and exit")

    serve = sub.add_parser("serve", help="run the HTTP API and the scheduler (needs the [server] extra)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--interval", type=float, default=30.0, help="seconds between checks")

    dev = sub.add_parser("devices", help="phones and browsers signed in with a token of their own (ADR 0018)")
    dev_sub = dev.add_subparsers(dest="devices_command", required=True)
    dev_sub.add_parser("list", help="show devices that can sign in")
    dev_add = dev_sub.add_parser("add", help="sign a device in and print its token, once")
    dev_add.add_argument("name", nargs="+", help='what to call it, e.g. "kitchen tablet"')
    dev_revoke = dev_sub.add_parser("revoke", help="stop a device's token working, at once")
    dev_revoke.add_argument("id", type=int)

    doctor = sub.add_parser("doctor", help="check configuration and channels before deploying")
    doctor.add_argument("--send-test", action="store_true", help="deliver a test message on every channel")
    doctor.add_argument("--check-model", action="store_true",
                        help="send one test phrase to the configured model provider (#115)")
    return p


def unusable_database(backend: str, opening: bool = False) -> tuple[type[BaseException], ...]:
    """What a database problem looks like, as opposed to a bug worth a traceback.

    Opening one also touches the filesystem — an unwritable directory, a missing mount — which is
    just as much something the operator can fix; once it is open, an OSError is not about the
    database any more.
    """
    errors: tuple[type[BaseException], ...] = (SchemaTooNewError, *driver_errors(backend))
    return (*errors, OSError) if opening else errors


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    try:
        config = Config.from_env()
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if "MAESTRO_CHANNELS" not in os.environ:
        config.channels = default_channels()
    args = _build_parser(config).parse_args(argv)
    if args.command == "doctor":
        # Runs before the normal setup below, because checking that setup is its job.
        from maestro import doctor

        config.db_path = args.db
        return 0 if doctor.run(config, send_test=args.send_test, check_model=args.check_model) else 1
    try:
        tz = get_timezone(config.timezone)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    try:
        # Built once, from the configuration, and handed to whatever captures: the rules unless
        # MAESTRO_INTERPRETER=model (#115). Half-configured is an error, never a silent fallback.
        interpreter = build_interpreter(config)
        _confirmation(config)  # a bad MAESTRO_CONFIRM is a config error, found now (#117)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    try:
        store = open_store(args.db, tz=tz)
    except StoreConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    except unusable_database(parse_url(args.db).backend, opening=True) as e:
        # A database that is locked, missing, unreachable or newer than this Maestro is something
        # the operator can act on, so say it in one line instead of printing a traceback.
        print(f"Database error: {e}", file=sys.stderr)
        return 2
    with store:
        try:
            return _dispatch(args, config, store, tz, interpreter)
        except unusable_database(store.backend) as e:
            print(f"Database error: {e}", file=sys.stderr)
            return 2


def _dispatch(args: argparse.Namespace, config: Config, store: Store, tz: ZoneInfo,
              interpreter: "Interpreter | None" = None) -> int:
    if args.command == "add":
        text = " ".join(args.text)
        captured, confidence = interpret_assessed(text, Context(tz=tz), interpreter)
        confirm = _confirmation(config)
        requestable = requestable_channels(config)
        if confirm is not None and confirm.splits:
            return _add_split(store, captured, confidence, text, requestable)
        if confirm and confirmations.needs_confirmation(captured, confidence):
            return _propose(store, captured, confidence, text, confirm)
        results = apply(store, captured, source="cli", requestable=requestable)
        for result in results:
            if result.dropped:
                kept = "the other requested channels will be used" if result.saved.channels \
                    else "using the default channels"
                print(f"Note: {', '.join(result.dropped)} isn't configured; {kept}.", file=sys.stderr)
            print(f"Saved: {_format(result.saved).strip()}")
    elif args.command in ("fill", "dismiss"):
        return _answer_gap(store, args.command, args.id,
                           " ".join(getattr(args, "phrase", [])), requestable_channels(config))
    elif args.command == "list":
        if args.proposals:
            return _list_proposals(store)
        if args.needs_look:
            return _list_needs_look(store)
        items = store.list(include_done=args.all)
        print(_grouped(items, args.verbose) if items else "Nothing to remember yet.")
    elif args.command == "search":
        items = store.search(args.query)
        print("\n".join(map(_format, items)) if items else "No matches.")
    elif args.command in ("confirm", "cancel", "undo"):
        return _decide(store, args.command, args.capture_id)
    elif args.command == "devices":
        return _devices(store, args, tz)
    elif args.command in ("done", "stop", "rm", "snooze"):
        if (target := store.get(args.id)) is None:
            print(f"No item #{args.id}", file=sys.stderr)
            return 1
        if args.command == "done":
            store.complete(args.id, via="cli")
            after = store.get(args.id)
            if target.recurrence and after and after.status != Status.DONE:
                print(f"Marked #{args.id} as done for now; next: {_format(after).strip()}")
            else:
                print(f"Marked #{args.id} as done.")
        elif args.command == "stop":
            store.stop(args.id, via="cli")
            print(f"Stopped #{args.id}.")
        elif args.command == "rm":
            store.delete(args.id)
            print(f"Deleted #{args.id}.")
        else:
            try:
                delta = parse_duration(args.duration)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 2
            store.snooze(args.id, add_elapsed(datetime.now(tz).replace(microsecond=0), delta), via="cli")
            if (snoozed := store.get(args.id)) is None:
                print(f"No item #{args.id}", file=sys.stderr)
                return 1
            print(f"Snoozed: {_format(snoozed).strip()}")
    elif args.command in ("run", "serve"):
        try:
            configure_logging(config.log_format, config.log_level)
            notifier = build_notifier(config)
            from maestro.escalation import build_stages
            from maestro.quiet import parse_quiet_hours

            escalation = build_stages(config)
            quiet = parse_quiet_hours(config.quiet_hours)
        except (ConfigError, ValueError) as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 2
        bot = _build_bot(config, store, interpreter, _confirmation(config))
        try:
            email = _build_email(config, store, interpreter)
        except ValueError as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 2
        if args.command == "serve":
            return _serve(store, config, notifier, args, bot, email, escalation, quiet, interpreter)
        if args.once:
            # Respect a running server's lease, and hand it straight back afterwards.
            lease = Lease()
            try:
                fire_due(store, notifier, lease=lease, escalation=escalation, quiet=quiet)
            finally:
                lease.release(store)
        else:
            extra = " and listening on Telegram" if bot else ""
            print(f"Watching for due reminders every {args.interval:g}s via {', '.join(config.channels)}"
                  f"{extra}. Ctrl+C to stop.")
            # docker stop / systemctl stop send SIGTERM: exit through the normal path so the scheduler
            # lease is released and a standby takes over at once instead of after the TTL.
            signal.signal(signal.SIGTERM, _exit_on_sigterm)
            if email:
                email.start()
            try:
                run_forever(store, notifier, args.interval, wait=_wait_with(bot, email),
                            escalation=escalation, quiet=quiet)
            except KeyboardInterrupt:
                pass
            finally:
                if email:
                    email.stop()
    return 0


def _exit_on_sigterm(signum: int, frame: object) -> None:
    raise SystemExit(0)


def _build_bot(config: Config, store: Store, interpreter: "Interpreter | None" = None,
               confirm: "confirmations.Confirmation | None" = None) -> "TelegramBot | None":
    if not config.telegram_bot_token:
        return None
    from maestro.telegram import TelegramBot, TelegramClient

    if not config.telegram_chat_id:
        logging.warning("TELEGRAM_CHAT_ID is not set: send /start to your bot to get it")
    return TelegramBot(TelegramClient.for_token(config.telegram_bot_token), store, config.telegram_chat_id,
                       requestable=requestable_channels(config), interpreter=interpreter,
                       confirm=confirm)


def _build_email(config: Config, store: Store,
                 interpreter: "Interpreter | None" = None) -> "EmailWorker | None":
    from maestro.config import email_settings
    from maestro.email_in import EmailPoller, EmailWorker

    settings = email_settings(config)
    if not settings:
        return None
    return EmailWorker(EmailPoller(store, settings, requestable=requestable_channels(config),
                                   interpreter=interpreter))


def _wait_with(bot: "TelegramBot | None", email: "EmailWorker | None") -> Callable[[float], None]:
    """Between scheduler ticks: capture mail the background worker fetched (never blocks), then
    long-poll Telegram (or just sleep)."""
    then = _bot_wait(bot) if bot else time.sleep

    def wait(seconds: float) -> None:
        if email:
            email.process()
        then(seconds)

    return wait


def _bot_wait(bot: "TelegramBot") -> Callable[[float], None]:
    """Use the wait between scheduler ticks to long-poll Telegram, so messages are answered right away."""

    def wait(seconds: float) -> None:
        try:
            bot.poll_once(timeout=max(1, int(seconds)))
        except Exception:
            logging.exception("Telegram polling failed; retrying after the next tick")
            time.sleep(seconds)

    return wait


def _serve(store: Store, config: Config, notifier: Notifier, args: argparse.Namespace,
           bot: "TelegramBot | None" = None, email: "EmailWorker | None" = None,
           escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None,
           interpreter: "Interpreter | None" = None) -> int:
    try:
        import uvicorn

        from maestro.api import create_app
        from maestro.ratelimit import RateLimits
    except ImportError:
        print("The server needs extra packages: pip install 'maestro-orchestrator[server]'", file=sys.stderr)
        return 2
    if config.api_token is None and args.host not in ("127.0.0.1", "localhost"):
        print("Refusing to listen on a public interface without MAESTRO_API_TOKEN set.", file=sys.stderr)
        return 2
    sms = voice = None
    if config.twilio_auth_token and config.sms_to:
        from maestro.sms import SmsHandler
        from maestro.voice import VoiceHandler

        requestable = requestable_channels(config)
        sms = SmsHandler(store, config.twilio_auth_token, config.sms_to, requestable=requestable,
                         interpreter=interpreter, confirm=_confirmation(config))
        voice = VoiceHandler(store, config.twilio_auth_token, config.sms_to, requestable=requestable,
                             interpreter=interpreter)
    app = create_app(store, api_token=config.api_token, notifier=notifier, interval=args.interval, bot=bot,
                     sms=sms, voice=voice, email=email, escalation=escalation, quiet=quiet,
                     public_url=config.public_url, requestable=requestable_channels(config),
                     interpreter=interpreter, confirm=_confirmation(config),
                     limits=RateLimits(config.rate_limit_webhook, config.rate_limit_auth_failures,
                                       config.trust_proxy_headers))
    # log_config=None keeps uvicorn on our root handler, so MAESTRO_LOG_FORMAT=json covers access logs too.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
