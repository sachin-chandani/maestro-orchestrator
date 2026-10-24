"""Chat-style commands shared by the Telegram bot and SMS: list, done, snooze, rm, help.

Telegram uses slash commands ("/done 3"); SMS accepts the same words without the slash
("done 3"). Plain text that isn't exactly a command is captured as a new item.
"""

import re
from datetime import datetime

from maestro import recurrence
from maestro.models import Item, Kind, Status
from maestro.parser import parse_duration
from maestro.store import Store
from maestro.tz import add_elapsed

LIST_LIMIT = 20

# Only exact command shapes count, so "done with taxes by friday" is still captured as a reminder.
_COMMAND_RE = re.compile(
    r"^/?(?:(?P<bare>list|help|start)|(?P<one>done|rm|delete)\s+#?(?P<id1>\d+)"
    r"|(?P<snooze>snooze)\s+#?(?P<id2>\d+)\s+(?P<dur>\S+))(?:@\w+)?$",
    re.I,
)


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
        f"{prefix}rm <id>: delete"
    )


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """("done", ["3"]) for "done 3" or "/done 3"; None when the text isn't a command."""
    if not (m := _COMMAND_RE.match(text.strip())):
        return None
    if m["bare"]:
        return m["bare"].lower(), []
    if m["one"]:
        action = m["one"].lower()
        return ("rm" if action == "delete" else action), [m["id1"]]
    return "snooze", [m["id2"], m["dur"]]


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
