from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Kind(StrEnum):
    REMINDER = "reminder"  # has a due time and fires a notification
    TASK = "task"  # something to do, no due time, never notified (#114)
    NOTE = "note"  # a fact to remember, no due time


class Status(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    DONE = "done"


class CaptureStatus(StrEnum):
    """The life of a captured message that needed confirmation (#117)."""

    PENDING = "pending"  # asked, not yet answered
    APPLIED = "applied"  # confirmed; its items exist (unless undone)
    CANCELLED = "cancelled"  # the person said no
    EXPIRED = "expired"  # nobody answered in time; still listed, never applied
    UNDONE = "undone"  # confirmed, then taken back inside the undo window


@dataclass
class Item:
    text: str
    kind: Kind
    created_at: datetime
    due_at: datetime | None = None
    status: Status = Status.PENDING
    source_text: str = ""
    recurrence: str | None = None  # see maestro.recurrence for the rule format
    # A recurring reminder snoozed by the person: this occurrence is sent again then, while due_at keeps
    # the series' own time (ADR 0009). Always before due_at; None for one-off reminders.
    snoozed_until: datetime | None = None
    # Channels this item is delivered on ("text me" -> ["twilio_sms"]); None means MAESTRO_CHANNELS.
    channels: list[str] | None = None
    # Urgent reminders ignore quiet hours. None means the item predates the flag (schema 9 / ADR 0011):
    # urgency is then read from the text, as it was before. New items always carry an explicit value.
    urgent: bool | None = None
    # Which interpreter read this from the capture: "rules" or "model" (#116). None means the item
    # predates the column, or was saved directly rather than from a capture.
    interpreter: str | None = None
    # The confirmed capture this item came from (#117), so it can be undone. None for items saved
    # directly, or from before confirmations existed; those can't be undone.
    capture_id: int | None = None
    id: int | None = None


@dataclass
class Acknowledgement:
    """That someone dealt with one occurrence of an item, and how they said so."""

    acknowledged_at: datetime
    via: str  # the interface it came from: cli, web, api, telegram, email, twilio_sms, twilio_call…


@dataclass
class Delivery:
    """Delivery state of one occurrence of an item on one channel."""

    channel: str
    attempts: int = 0
    delivered_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error: str | None = None


@dataclass
class Capture:
    """A captured message that needs (or needed) confirmation before acting (#117).

    It holds the interpretation as validated actions in their JSON form, not as items: nothing
    about it exists in `items` until it is confirmed, so the scheduler, lists and counts cannot
    act on something nobody agreed to.
    """

    status: CaptureStatus
    actions: str  # the proposed actions as a JSON list (maestro.actions.to_json of each)
    source: str  # the channel it arrived on ("telegram", "twilio_sms", "api", "cli")
    source_text: str  # the message as it arrived
    created_at: datetime
    expires_at: datetime  # unanswered past this moment means expired, never applied
    address: str = ""  # who sent it on that channel (chat id, phone number); "" for cli/api
    interpreter: str | None = None  # who read it ("rules" or "model", #116)
    doubts: str = ""  # why the reading was unsure, comma-joined (maestro.confidence)
    undo_until: datetime | None = None  # set when applied: take-backs are allowed until then
    decided_at: datetime | None = None  # when it was applied, cancelled or undone
    decided_via: str | None = None  # the interface that decided it
    id: int | None = None
