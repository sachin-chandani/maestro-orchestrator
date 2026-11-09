"""What a capture asks Maestro to do, as typed, validated actions (ADR 0015).

An interpreter (the rules today, optionally a model later, see `maestro.interpret`) turns text into a
list of these; `apply` is the only code that turns them into store calls. Every action has the same
JSON form in both directions, so a model can later fill in exactly what the rules fill in today, and
anything it sends that isn't valid is rejected before it reaches the store.

Room is left for `AddToList` and `Ask`: add the dataclass, register its field types and handle it
in `apply`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar

from maestro import commands
from maestro.models import Item, Kind
from maestro.notifier import apply_channel_choice
from maestro.parser import parse_duration
from maestro.storage import Store
from maestro.tz import get_timezone

# --- JSON field types ----------------------------------------------------------------------
# One validator per field name. Every action field must have one, so a new action can only add a
# field that has a declared JSON type. Validators raise ValueError on anything they can't accept.


def _as_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _as_optional_text(name: str, value: Any) -> str | None:
    return None if value is None else _as_text(name, value)


def _as_raw_text(name: str, value: Any) -> str:
    """A string that may be empty (the capture as it arrived)."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _as_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _as_item_id(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive item number")
    return value


def _as_moment(name: str, value: Any) -> datetime | None:
    """An ISO 8601 timestamp that carries an offset, so it means one moment anywhere (ADR 0001)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be an ISO 8601 timestamp")
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be an ISO 8601 timestamp, not {value!r}") from None
    if moment.tzinfo is None:
        raise ValueError(f"{name} must say which timezone it is in")
    return moment


def _as_channels(name: str, value: Any) -> list[str] | None:
    # Copied, so a caller's list can't change an action after the fact; an empty list is "no choice
    # made", the same as None, so there is only one way to say it.
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(c, str) and c for c in value):
        raise ValueError(f"{name} must be a list of channel names")
    return list(value) or None


FIELD_TYPES: dict[str, Callable[[str, Any], Any]] = {
    "text": _as_text,
    "source_text": _as_raw_text,
    "due_at": _as_moment,
    "created_at": _as_moment,
    "recurrence": _as_optional_text,
    "channels": _as_channels,
    "urgent": _as_bool,
    "item_id": _as_item_id,
    "duration": _as_optional_text,
}


# --- actions -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One thing to do. Subclasses declare their fields; `TYPE` is the tag in the JSON form."""

    TYPE: ClassVar[str] = ""

    def __post_init__(self) -> None:
        # Validated the same way whether the action came from JSON or from Python, and read in the
        # same way: an ISO timestamp becomes a datetime here, so no field ever holds its JSON shape.
        for f in fields(self):
            object.__setattr__(self, f.name, FIELD_TYPES[f.name](f.name, getattr(self, f.name)))

    def to_json(self) -> dict[str, Any]:
        """The action as JSON-ready data. Fields left at their default are omitted, so the form
        stays small and a reader only sees what was actually decided."""
        data: dict[str, Any] = {"type": self.TYPE}
        for f in fields(self):
            value = getattr(self, f.name)
            if value == f.default:
                continue
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, list):
                value = list(value)  # a copy: changing the JSON must not change the action
            data[f.name] = value
        return data


@dataclass(frozen=True)
class _Create(Action):
    """Shared by the actions that save something new."""

    text: str
    source_text: str = ""  # the capture as it arrived, kept for search and for showing what was said
    channels: list[str] | None = None  # None means the configured default channels
    urgent: bool = False
    created_at: datetime | None = None  # when the capture happened; apply fills it in when absent

    def _item(self, kind: Kind, now: datetime | None, **extra: Any) -> Item:
        return Item(
            text=self.text,
            kind=kind,
            created_at=self.created_at or now or datetime.now(get_timezone()).replace(microsecond=0),
            source_text=self.source_text,
            channels=list(self.channels) if self.channels else None,
            urgent=self.urgent,
            **extra,
        )


@dataclass(frozen=True)
class CreateReminder(_Create):
    """Save something to be delivered at a time, and escalated if it isn't acknowledged."""

    TYPE: ClassVar[str] = "create_reminder"

    due_at: datetime | None = None  # required; optional only so the inherited fields keep defaults
    recurrence: str | None = None  # see maestro.recurrence for the rule format

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.due_at is None:
            raise ValueError("create_reminder needs a due_at; use create_note for something with no time")

    def to_item(self, now: datetime | None = None) -> Item:
        return self._item(Kind.REMINDER, now, due_at=self.due_at, recurrence=self.recurrence)


@dataclass(frozen=True)
class CreateTask(_Create):
    """Save something to do that has no time (#114). A task is never delivered and never escalated;
    done works on it, and giving it a time later makes it a reminder."""

    TYPE: ClassVar[str] = "create_task"

    def to_item(self, now: datetime | None = None) -> Item:
        return self._item(Kind.TASK, now)


@dataclass(frozen=True)
class CreateNote(_Create):
    """Save something to remember. A note has no time and is never delivered."""

    TYPE: ClassVar[str] = "create_note"

    def to_item(self, now: datetime | None = None) -> Item:
        return self._item(Kind.NOTE, now)


@dataclass(frozen=True)
class Done(Action):
    """Mark an item done. For a recurring reminder that is this occurrence only (ADR 0009)."""

    TYPE: ClassVar[str] = "done"

    item_id: int | None = None  # None means the reminder last delivered on the channel being used


@dataclass(frozen=True)
class Snooze(Action):
    """Push one occurrence of a reminder back by a duration such as "10m", "2h" or "1d"."""

    TYPE: ClassVar[str] = "snooze"

    item_id: int | None = None
    duration: str | None = None  # None means the default snooze

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.duration is not None:
            parse_duration(self.duration)  # raises ValueError with the wording the user would see


@dataclass(frozen=True)
class Stop(Action):
    """End a repeating reminder, keeping what it already did."""

    TYPE: ClassVar[str] = "stop"

    item_id: int | None = None


ACTIONS: tuple[type[Action], ...] = (CreateReminder, CreateTask, CreateNote, Done, Snooze, Stop)
BY_TYPE: dict[str, type[Action]] = {a.TYPE: a for a in ACTIONS}


def from_json(data: Any) -> Action:
    """Build an action from its JSON form, rejecting anything that isn't one.

    Untrusted input: a wrong type, an unknown field or a time with no offset is a ValueError, never
    a half-built action.
    """
    if not isinstance(data, dict):
        raise ValueError("An action must be a JSON object")
    tag = data.get("type")
    if tag not in BY_TYPE:
        known = ", ".join(sorted(BY_TYPE))
        raise ValueError(f"Unknown action type {tag!r}; expected one of {known}")
    cls = BY_TYPE[tag]
    known_fields = {f.name for f in fields(cls)}
    if unknown := set(data) - known_fields - {"type"}:
        raise ValueError(f"{tag} has no field {', '.join(sorted(unknown))}")
    # The values are checked and read by __post_init__, so JSON and Python go through one gate.
    return cls(**{name: value for name, value in data.items() if name != "type"})


def actions_from_json(data: Any) -> list[Action]:
    """Build a list of actions from its JSON form (what an interpreter returns)."""
    if not isinstance(data, list):
        raise ValueError("Actions must be a JSON list")
    return [from_json(entry) for entry in data]


def from_item(item: Item) -> CreateReminder | CreateTask | CreateNote:
    """The action that would save this item: how the rule-based parser's result becomes an action."""
    common: dict[str, Any] = {
        "text": item.text,
        "source_text": item.source_text,
        "channels": list(item.channels) if item.channels else None,
        "urgent": bool(item.urgent),
        "created_at": item.created_at,
    }
    if item.kind == Kind.REMINDER:
        return CreateReminder(due_at=item.due_at, recurrence=item.recurrence, **common)
    if item.kind == Kind.TASK:
        return CreateTask(**common)
    return CreateNote(**common)


# --- applying ------------------------------------------------------------------------------


@dataclass
class Applied:
    """What `apply` did for one action, so the channel can say it in its own words."""

    action: Action
    item: Item | None = None  # the item saved, or the one already saved for a repeated message
    created: bool = True  # False when this inbound message had been captured before
    dropped: list[str] = field(default_factory=list)  # channels asked for that aren't configured
    message: str = ""  # for done / snooze / stop: the outcome, already worded

    @property
    def saved(self) -> Item:
        """The item, for a capture. A create action always has one; done / snooze / stop don't."""
        if self.item is None:
            raise ValueError(f"{self.action.TYPE} saved no item")
        return self.item


def apply(store: Store, actions: list[Action], source: str, *, dedupe_key: str | None = None,
          requestable: frozenset[str] | None = None, channel: str | None = None,
          prefix: str = "") -> list[Applied]:
    """Carry out actions against the store: the one place actions become store calls (ADR 0015).

    `source` names where the capture came from, and is the namespace a `dedupe_key` is unique in
    ("twilio_sms", "email", …), so it has to stay the same between releases for a retry that
    straddles an upgrade to still be recognised.

    With `dedupe_key` (a provider's message or call id) the capture happens at most once, however
    often the provider retries. A caller with no id must pass None, not "": an empty key would make
    every keyless capture from that source look like the same message. Today the inbound paths
    normalise it (SMS and voice send `external_id or None`, an email always has a Message-ID or a
    hash of itself), so a message with no id is simply captured each time it arrives, as it was
    before ADR 0015.

    `requestable` is the set of channels this deployment can honour; None skips that check.
    `channel` and `prefix` are the channel a done / snooze / stop came from and the prefix its help
    text uses.
    """
    return [_apply_one(store, action, source, dedupe_key, requestable, channel, prefix)
            for action in actions]


def _apply_one(store: Store, action: Action, source: str, dedupe_key: str | None,
               requestable: frozenset[str] | None, channel: str | None, prefix: str) -> Applied:
    if isinstance(action, CreateReminder | CreateTask | CreateNote):
        item = action.to_item(datetime.now(store.tz).replace(microsecond=0))
        dropped = apply_channel_choice(item, requestable) if requestable is not None else []
        if dedupe_key is None:
            return Applied(action, store.add(item), created=True, dropped=dropped)
        saved, created = store.add_once(source, dedupe_key, item)
        return Applied(action, saved, created=created, dropped=dropped)
    if isinstance(action, Done | Snooze | Stop):
        command = commands.Command(
            action=action.TYPE,
            item_id=action.item_id,
            duration=action.duration if isinstance(action, Snooze) else None,
        )
        return Applied(action, message=commands.run(store, command, channel=channel, prefix=prefix))
    raise ValueError(f"No way to apply {type(action).__name__}")  # pragma: no cover - guarded by typing
