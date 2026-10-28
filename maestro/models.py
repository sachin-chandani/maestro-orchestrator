from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Kind(StrEnum):
    REMINDER = "reminder"  # has a due time and fires a notification
    NOTE = "note"  # a fact to remember, no due time


class Status(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    DONE = "done"


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
    id: int | None = None


@dataclass
class Delivery:
    """Delivery state of one occurrence of an item on one channel."""

    channel: str
    attempts: int = 0
    delivered_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error: str | None = None
