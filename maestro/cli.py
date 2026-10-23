"""Command-line interface: `maestro add "call mom tomorrow at 5pm"`, `maestro list`, `maestro run`."""

import argparse
import os
import sys
from pathlib import Path

from maestro.models import Item, Kind, Status
from maestro.notifier import ConsoleNotifier, MultiNotifier, default_notifier
from maestro.parser import parse
from maestro.scheduler import fire_due, run_forever
from maestro.store import Store

DEFAULT_DB = Path.home() / ".maestro" / "maestro.db"


def _format(item: Item) -> str:
    when = item.due_at.strftime("%a %d %b %H:%M") if item.due_at else "—"
    tag = "⏰" if item.kind == Kind.REMINDER else "📝"
    status = "" if item.status == Status.PENDING else f" [{item.status}]"
    return f"{item.id:>4}  {tag} {when:<16}  {item.text}{status}"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maestro", description="Your second brain for things to remember.")
    p.add_argument("--db", default=os.environ.get("MAESTRO_DB", DEFAULT_DB), help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help='capture something, e.g. "pay rent on Nov 1"')
    add.add_argument("text", nargs="+")

    ls = sub.add_parser("list", help="show reminders and notes")
    ls.add_argument("--all", action="store_true", help="include completed items")

    search = sub.add_parser("search", help="find items by text")
    search.add_argument("query")

    for name, help_text in [("done", "mark an item as done"), ("rm", "delete an item")]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("id", type=int)

    run = sub.add_parser("run", help="fire reminders as they come due")
    run.add_argument("--interval", type=float, default=30.0, help="seconds between checks")
    run.add_argument("--once", action="store_true", help="check once and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
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
    elif args.command == "done":
        if not store.set_status(args.id, Status.DONE):
            print(f"No item #{args.id}", file=sys.stderr)
            return 1
        print(f"Marked #{args.id} as done.")
    elif args.command == "rm":
        if not store.delete(args.id):
            print(f"No item #{args.id}", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}.")
    elif args.command == "run":
        notifier = MultiNotifier(ConsoleNotifier(), default_notifier())
        if args.once:
            fire_due(store, notifier)
        else:
            print(f"Watching for due reminders every {args.interval:g}s. Ctrl+C to stop.")
            try:
                run_forever(store, notifier, args.interval)
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
