"""Command-line interface: `maestro add "call mom tomorrow at 5pm"`, `maestro list`, `maestro run`."""

import argparse
import logging
import os
import sys
from datetime import datetime

from maestro import recurrence
from maestro.config import Config, load_dotenv
from maestro.models import Item, Kind, Status
from maestro.notifier import ConfigError, build_notifier, default_channels
from maestro.parser import parse, parse_duration
from maestro.scheduler import fire_due, run_forever
from maestro.store import Store


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
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = Config.from_env()
    if "MAESTRO_CHANNELS" not in os.environ:
        config.channels = default_channels()
    args = _build_parser(config).parse_args(argv)
    store = Store(args.db)

    if args.command == "add":
        item = store.add(parse(" ".join(args.text)))
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
            store.reschedule(args.id, datetime.now().replace(microsecond=0) + delta)
            print(f"Snoozed: {_format(store.get(args.id)).strip()}")
    elif args.command in ("run", "serve"):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            notifier = build_notifier(config)
        except ConfigError as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 2
        if args.command == "serve":
            return _serve(store, config, notifier, args)
        if args.once:
            fire_due(store, notifier)
        else:
            print(f"Watching for due reminders every {args.interval:g}s via {', '.join(config.channels)}. "
                  "Ctrl+C to stop.")
            try:
                run_forever(store, notifier, args.interval)
            except KeyboardInterrupt:
                pass
    return 0


def _serve(store: Store, config: Config, notifier, args) -> int:
    try:
        import uvicorn

        from maestro.api import create_app
    except ImportError:
        print("The server needs extra packages: pip install 'maestro-orchestrator[server]'", file=sys.stderr)
        return 2
    if config.api_token is None and args.host not in ("127.0.0.1", "localhost"):
        print("Refusing to listen on a public interface without MAESTRO_API_TOKEN set.", file=sys.stderr)
        return 2
    app = create_app(store, api_token=config.api_token, notifier=notifier, interval=args.interval)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
