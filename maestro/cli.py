"""Command-line interface: `maestro add "call mom tomorrow at 5pm"`, `maestro list`, `maestro run`."""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from maestro import recurrence
from maestro.config import Config, load_dotenv
from maestro.models import Item, Kind, Status
from maestro.notifier import ConfigError, Notifier, build_notifier, default_channels
from maestro.observability import configure_logging
from maestro.parser import parse, parse_duration
from maestro.scheduler import Lease, fire_due, run_forever
from maestro.store import Store
from maestro.tz import add_elapsed, get_timezone

if TYPE_CHECKING:
    from maestro.telegram import TelegramBot


def _format(item: Item) -> str:
    when = item.due_at.strftime("%a %d %b %H:%M") if item.due_at else "—"
    tag = "⏰" if item.kind == Kind.REMINDER else "📝"
    repeat = f" ({recurrence.describe(item.recurrence)})" if item.recurrence else ""
    status = "" if item.status == Status.PENDING else f" [{item.status}]"
    return f"{item.id:>4}  {tag} {when:<16}  {item.text}{repeat}{status}"


def _build_parser(config: Config) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maestro", description="Your second brain for things to remember.")
    p.add_argument("--db", default=config.db_path, help="SQLite database path (env: MAESTRO_DB)")
    sub = p.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help='capture something, e.g. "pay rent on the 1st of every month"')
    add.add_argument("text", nargs="+")

    ls = sub.add_parser("list", help="show reminders and notes")
    ls.add_argument("--all", action="store_true", help="include completed items")

    search = sub.add_parser("search", help="find items by text")
    search.add_argument("query")

    for name, help_text in [("done", "mark an item as done"), ("rm", "delete an item")]:
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

    doctor = sub.add_parser("doctor", help="check configuration and channels before deploying")
    doctor.add_argument("--send-test", action="store_true", help="deliver a test message on every channel")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = Config.from_env()
    if "MAESTRO_CHANNELS" not in os.environ:
        config.channels = default_channels()
    args = _build_parser(config).parse_args(argv)
    if args.command == "doctor":
        # Runs before the normal setup below, because checking that setup is its job.
        from maestro import doctor

        config.db_path = args.db
        return 0 if doctor.run(config, send_test=args.send_test) else 1
    try:
        tz = get_timezone(config.timezone)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    with Store(args.db, tz=tz) as store:
        return _dispatch(args, config, store, tz)


def _dispatch(args: argparse.Namespace, config: Config, store: Store, tz: ZoneInfo) -> int:
    if args.command == "add":
        item = store.add(parse(" ".join(args.text), tz=tz))
        print(f"Saved: {_format(item).strip()}")
    elif args.command == "list":
        items = store.list(include_done=args.all)
        print("\n".join(map(_format, items)) if items else "Nothing to remember yet.")
    elif args.command == "search":
        items = store.search(args.query)
        print("\n".join(map(_format, items)) if items else "No matches.")
    elif args.command in ("done", "rm", "snooze"):
        if store.get(args.id) is None:
            print(f"No item #{args.id}", file=sys.stderr)
            return 1
        if args.command == "done":
            store.set_status(args.id, Status.DONE)
            print(f"Marked #{args.id} as done.")
        elif args.command == "rm":
            store.delete(args.id)
            print(f"Deleted #{args.id}.")
        else:
            try:
                delta = parse_duration(args.duration)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 2
            store.reschedule(args.id, add_elapsed(datetime.now(tz).replace(microsecond=0), delta))
            if (snoozed := store.get(args.id)) is None:
                print(f"No item #{args.id}", file=sys.stderr)
                return 1
            print(f"Snoozed: {_format(snoozed).strip()}")
    elif args.command in ("run", "serve"):
        try:
            configure_logging(config.log_format, config.log_level)
            notifier = build_notifier(config)
        except (ConfigError, ValueError) as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 2
        bot = _build_bot(config, store)
        if args.command == "serve":
            return _serve(store, config, notifier, args, bot)
        if args.once:
            # Respect a running server's lease, and hand it straight back afterwards.
            lease = Lease()
            try:
                fire_due(store, notifier, lease=lease)
            finally:
                lease.release(store)
        else:
            extra = " and listening on Telegram" if bot else ""
            print(f"Watching for due reminders every {args.interval:g}s via {', '.join(config.channels)}"
                  f"{extra}. Ctrl+C to stop.")
            try:
                run_forever(store, notifier, args.interval, wait=_bot_wait(bot) if bot else time.sleep)
            except KeyboardInterrupt:
                pass
    return 0


def _build_bot(config: Config, store: Store) -> "TelegramBot | None":
    if not config.telegram_bot_token:
        return None
    from maestro.telegram import TelegramBot, TelegramClient

    if not config.telegram_chat_id:
        logging.warning("TELEGRAM_CHAT_ID is not set: send /start to your bot to get it")
    return TelegramBot(TelegramClient.for_token(config.telegram_bot_token), store, config.telegram_chat_id)


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
           bot: "TelegramBot | None" = None) -> int:
    try:
        import uvicorn

        from maestro.api import create_app
    except ImportError:
        print("The server needs extra packages: pip install 'maestro-orchestrator[server]'", file=sys.stderr)
        return 2
    if config.api_token is None and args.host not in ("127.0.0.1", "localhost"):
        print("Refusing to listen on a public interface without MAESTRO_API_TOKEN set.", file=sys.stderr)
        return 2
    sms = None
    if config.twilio_auth_token and config.sms_to:
        from maestro.sms import SmsHandler

        sms = SmsHandler(store, config.twilio_auth_token, config.sms_to)
    app = create_app(store, api_token=config.api_token, notifier=notifier, interval=args.interval, bot=bot,
                     sms=sms, public_url=config.public_url)
    # log_config=None keeps uvicorn on our root handler, so MAESTRO_LOG_FORMAT=json covers access logs too.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
