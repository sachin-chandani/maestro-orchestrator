"""Chat-style commands shared by the Telegram bot and SMS: list, done, snooze, rm, help.

Telegram uses slash commands ("/done 3"); SMS accepts the same words without the slash
("done 3"). Plain text that isn't exactly a command is captured as a new item.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from maestro import recurrence
from maestro.models import Item, Kind, Status
from maestro.parser import DURATION_UNITS, parse_duration
from maestro.store import Store
from maestro.tz import add_elapsed

LIST_LIMIT = 20

DEFAULT_SNOOZE = "10m"

# Only exact command shapes count, so "done with taxes by friday" is still captured as a reminder.
# The item number is optional: without it, a reply acts on the reminder last sent on that channel.
_COMMAND_RE = re.compile(
    r"^/?(?:(?P<bare>list|help|start)"
    r"|(?P<one>done|rm|delete)(?:\s+#?(?P<id1>\d+))?"
    rf"|(?P<snooze>snooze)(?:\s+#?(?P<id2>\d+))?(?:\s+(?P<dur>\d+\s*{DURATION_UNITS}))?)(?:@\w+)?$",
    re.I,
)


@dataclass
class Command:
    action: str  # list, help, done, rm or snooze
    item_id: int | None = None
    duration: str | None = None


def when(item: Item) -> str:
    if not item.due_at:
        return "note"
    label = item.due_at.strftime("%a %d %b %H:%M")
    return f"{label}, {recurrence.describe(item.recurrence)}" if item.recurrence else label


def line(item: Item) -> str:
    icon = "⏰" if item.kind == Kind.REMINDER else "📝"
    return f"{icon} #{item.id} {item.text} ({when(item)})"


def help_text(prefix: str = "") -> str:
    return (
        "Send anything to remember, e.g.\n"
        "• call mom tomorrow at 5pm\n"
        "• pay rent on the 1st of every month\n"
        "• remember the wifi password is on the router\n\n"
        f"{prefix}list: what's coming up\n"
        f"{prefix}done <id>: mark done\n"
        f"{prefix}snooze <id> <10m|2h|1d>: push back\n"
        f"{prefix}rm <id>: delete\n\n"
        "Reply \"done\" or \"snooze 1h\" to a reminder to act on it without the number."
    )


def parse_command(text: str) -> Command | None:
    """Command("done", 3) for "done 3" or "/done 3"; None when the text isn't a command."""
    if not (m := _COMMAND_RE.match(text.strip())):
        return None
    if m["bare"]:
        return Command("help" if m["bare"].lower() == "start" else m["bare"].lower())
    if m["one"]:
        action = "rm" if m["one"].lower() == "delete" else m["one"].lower()
        return Command(action, int(m["id1"]) if m["id1"] else None)
    return Command("snooze", int(m["id2"]) if m["id2"] else None, m["dur"])


def list_items(store: Store) -> str:
    return "\n".join(line(i) for i in store.list()[:LIST_LIMIT]) or "Nothing to remember yet."


def act(store: Store, action: str, args: list[str], prefix: str = "") -> str:
    """Run done / rm / snooze on an item and describe the outcome."""
    expected = 2 if action == "snooze" else 1
    if len(args) != expected or not args[0].lstrip("#").isdigit():
        usage = "snooze <id> <10m|2h|1d>" if action == "snooze" else f"{action} <id>"
        return f"Usage: {prefix}{usage}"
    item_id = int(args[0].lstrip("#"))
    if (item := store.get(item_id)) is None:
        return f"No item #{item_id}"
    if action == "done":
        store.set_status(item_id, Status.DONE)
        return f"✅ Done: {item.text}"
    if action == "rm":
        store.delete(item_id)
        return f"🗑 Deleted: {item.text}"
    try:
        delta = parse_duration(args[1])
    except ValueError as e:
        return str(e)
    store.reschedule(item_id, add_elapsed(datetime.now(store.tz).replace(microsecond=0), delta))
    return f"😴 Snoozed: {line(store.get(item_id))}"


def run(store: Store, command: Command, channel: str | None = None, prefix: str = "") -> str:
    """Run a parsed command. Without an item number, done/snooze/rm apply to the reminder most
    recently delivered on `channel`, so replying "done" to a reminder just works."""
    if command.action == "help":
        return help_text(prefix)
    if command.action == "list":
        return list_items(store)
    item_id = command.item_id
    # Deleting is never inferred: "rm" always needs an explicit number.
    if item_id is None and channel and command.action != "rm":
        item_id = store.last_delivered(channel)
    if item_id is None:
        return f"Which one? Add its number, e.g. {prefix}{command.action} 3 (see {prefix}list)."
    args = [str(item_id)]
    if command.action == "snooze":
        args.append(command.duration or DEFAULT_SNOOZE)
    return act(store, command.action, args, prefix)
