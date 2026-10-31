"""Chat-style commands shared by the Telegram bot and SMS: list, done, snooze, stop, rm, help.

Telegram uses slash commands ("/done 3"); SMS accepts the same words without the slash
("done 3"). Plain text that isn't exactly a command is captured as a new item.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from maestro import recurrence
from maestro.models import Item, Kind, Status
from maestro.parser import DURATION_UNITS, parse_duration
from maestro.storage import Store
from maestro.tz import add_elapsed

LIST_LIMIT = 20

DEFAULT_SNOOZE = "10m"

# Only exact command shapes count, so "done with taxes by friday" is still captured as a reminder.
# The item number is optional: without it, a reply acts on the reminder last sent on that channel.
_COMMAND_RE = re.compile(
    r"^/?(?:(?P<bare>list|help|start)"
    r"|(?P<one>done|stop|rm|delete)(?:\s+#?(?P<id1>\d+))?"
    rf"|(?P<snooze>snooze)(?:\s+#?(?P<id2>\d+))?(?:\s+(?P<dur>\d+\s*{DURATION_UNITS}))?)(?:@\w+)?$",
    re.I,
)


@dataclass
class Command:
    action: str  # list, help, done, stop, rm or snooze
    item_id: int | None = None
    duration: str | None = None


def when(item: Item) -> str:
    if not item.due_at:
        return "note"
    label = item.due_at.strftime("%a %d %b %H:%M")
    if item.snoozed_until:
        label = f"{item.snoozed_until:%a %d %b %H:%M} (snoozed), then {label}"
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
        f"{prefix}done <id>: mark done (for a repeating reminder: this time)\n"
        f"{prefix}snooze <id> <10m|2h|1d>: push back\n"
        f"{prefix}stop <id>: end a repeating reminder\n"
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


def act(store: Store, action: str, args: list[str], prefix: str = "", via: str = "chat",
        occurrence: datetime | None = None) -> str:
    """Run done / stop / rm / snooze on an item and describe the outcome. `occurrence` is the delivery a
    reply is about, when known; done and snooze on a recurring reminder apply to it (ADR 0009)."""
    expected = 2 if action == "snooze" else 1
    if len(args) != expected or not args[0].lstrip("#").isdigit():
        usage = "snooze <id> <10m|2h|1d>" if action == "snooze" else f"{action} <id>"
        return f"Usage: {prefix}{usage}"
    item_id = int(args[0].lstrip("#"))
    if (item := store.get(item_id)) is None:
        return f"No item #{item_id}"
    if action == "done":
        store.complete(item_id, via=via, occurrence=occurrence)
        after = store.get(item_id)
        if item.recurrence and after is not None and after.status != Status.DONE:
            return f"✅ Done for now: {item.text}. Next: {when(after)}"
        return f"✅ Done: {item.text}"
    if action == "stop":
        store.stop(item_id, via=via)
        return f"⏹ Stopped: {item.text}"
    if action == "rm":
        store.delete(item_id)
        return f"🗑 Deleted: {item.text}"
    try:
        delta = parse_duration(args[1])
    except ValueError as e:
        return str(e)
    store.snooze(item_id, add_elapsed(datetime.now(store.tz).replace(microsecond=0), delta), via=via,
                 occurrence=occurrence)
    if (snoozed := store.get(item_id)) is None:  # deleted in the meantime, e.g. from another device
        return f"No item #{item_id}"
    return f"😴 Snoozed: {line(snoozed)}"


def run(store: Store, command: Command, channel: str | None = None, prefix: str = "") -> str:
    """Run a parsed command. Without an item number, done/snooze/rm apply to the reminder most
    recently delivered on `channel`, so replying "done" to a reminder just works."""
    if command.action == "help":
        return help_text(prefix)
    if command.action == "list":
        return list_items(store)
    item_id = command.item_id
    occurrence = None
    # Deleting or ending a series is never inferred: "rm" and "stop" always need an explicit number.
    if item_id is None and channel and command.action not in ("rm", "stop"):
        item_id = store.last_delivered(channel)
        if item_id is not None:  # a reply to that message: it's about the occurrence it delivered
            occurrence = store.latest_delivered_occurrence(item_id)
    if item_id is None:
        return f"Which one? Add its number, e.g. {prefix}{command.action} 3 (see {prefix}list)."
    args = [str(item_id)]
    if command.action == "snooze":
        args.append(command.duration or DEFAULT_SNOOZE)
    return act(store, command.action, args, prefix, via=channel or "chat", occurrence=occurrence)


class ReplyOutcome(StrEnum):
    ACTED = "acted"
    ALREADY_HANDLED = "already handled"  # the same inbound message was seen before (provider retry)
    STALE = "stale"  # about an earlier delivery of the item, or an item that no longer exists


def act_on_delivery(store: Store, source: str, key: str, item_id: int, occurrence: datetime | int,
                    action: str, args: list[str], via: str) -> tuple[ReplyOutcome, str]:
    """Act on a reply to one specific delivery (an email reply, a key press on a reminder call).

    The protocol every such channel follows: an inbound message is handled at most once (`key`, e.g. the
    Message-ID or CallSid, under `source`); it acts only if it is about the item's latest delivered
    occurrence (replying 'done' on Wednesday to Monday's message of a daily reminder must not count);
    and it is recorded as handled only after the action succeeded, so a failure is retried, not lost.
    """
    if store.is_claimed(source, key):
        return ReplyOutcome.ALREADY_HANDLED, ""
    stamp = occurrence if isinstance(occurrence, int) else int(occurrence.timestamp())
    latest = store.latest_delivered_occurrence(item_id)
    if latest is None or int(latest.timestamp()) != stamp:
        return ReplyOutcome.STALE, ""
    result = act(store, action, [str(item_id), *args], via=via, occurrence=latest)
    store.claim_inbound(source, key, item_id)
    return ReplyOutcome.ACTED, result
