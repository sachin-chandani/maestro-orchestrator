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
    id: int | None = None
