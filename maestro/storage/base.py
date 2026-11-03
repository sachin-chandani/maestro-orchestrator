"""The storage interface every backend implements.

`Store` is what the CLI, the API, the scheduler and the channels are written against: they never
touch SQL or a connection. A backend subclasses it, implements every abstract method with the
semantics documented here, and is held to them by `tests/storage/test_conformance.py`.

Times crossing this interface are aware datetimes in the store's timezone (`self.tz`); naive ones
are taken to be wall-clock time in that zone. Storage is UTC (ADR 0001).
"""

# Store.list shadows the builtin inside the class body, so annotations there say builtins.list.
from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Self
from zoneinfo import ZoneInfo

from maestro.models import Acknowledgement, Delivery, Item, Status
from maestro.storage.logic import ACK_WINDOW

__all__ = ["ACK_WINDOW", "BACKUP_TABLES", "COLUMN_DEFAULTS", "RestoreBlockedError", "Row",
           "SchemaTooNewError", "Store", "TableSpec"]

#: One exported or restored row: portable values only — aware datetimes for time columns, bool for
#: flag columns, otherwise int, str or None. Never a backend's own storage type.
Row = dict[str, object]


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer Maestro than this one."""


class RestoreBlockedError(RuntimeError):
    """A restore was refused because a scheduler holds an unexpired lease (a server is running)."""

    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(
            f"a scheduler is running ({holder}); stop it first, or wait for its lease to expire"
        )


@dataclass(frozen=True)
class TableSpec:
    """One table as it appears in a backup (#59): its name and columns, and which columns hold
    timestamps or booleans, so every backend exports and restores the same portable values.

    A migration that adds a column must extend the spec, or backups would silently omit it;
    `tests/test_backup.py` compares the spec against each backend's real schema.
    """

    name: str
    columns: tuple[str, ...]
    times: frozenset[str] = frozenset()
    flags: frozenset[str] = frozenset()


#: Everything a backup carries. The scheduler lease is deliberately absent: it names a process on a
#: machine, not data, and restoring it could stop the next scheduler from starting.
BACKUP_TABLES: tuple[TableSpec, ...] = (
    TableSpec("items",
              ("id", "text", "kind", "status", "due_at", "created_at", "source_text", "recurrence",
               "snoozed_until", "channels", "urgent"),
              times=frozenset({"due_at", "created_at", "snoozed_until"}),
              flags=frozenset({"urgent"})),
    TableSpec("deliveries",
              ("item_id", "occurrence", "channel", "attempts", "delivered_at", "next_attempt_at",
               "last_error"),
              times=frozenset({"occurrence", "delivered_at", "next_attempt_at"})),
    TableSpec("acknowledgements",
              ("item_id", "occurrence", "acknowledged_at", "via"),
              times=frozenset({"occurrence", "acknowledged_at"})),
    TableSpec("inbound_messages", ("source", "external_id", "item_id")),
)

#: What restore fills in when a backup from an older Maestro lacks a column: exactly the value the
#: schema migrations would give an existing row. Columns absent here are required, and a backup
#: without them is refused. `tests/test_backup.py` checks these against the real schema defaults.
COLUMN_DEFAULTS: dict[str, Row] = {
    "items": {"source_text": "", "recurrence": None, "snoozed_until": None, "channels": None,
              "urgent": None},
    "deliveries": {"attempts": 0, "delivered_at": None, "next_attempt_at": None, "last_error": None},
}


class Store(ABC):
    """Persistence for captured items, their deliveries and the scheduler's lease."""

    #: Which backend this is, as it appears in `maestro doctor`, `/health` and metrics labels.
    #: Matches the URL scheme that opens it (see `maestro.storage.factory`).
    backend: str

    tz: ZoneInfo
    closed: bool

    # --- lifecycle -------------------------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Release the backend's resources. Safe to call more than once."""

    @abstractmethod
    def describe(self) -> str:
        """Where the data is, as a person would say it: a file path, or a server and database.

        Printed by `maestro doctor`, so it must never contain a password.
        """

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    @property
    @abstractmethod
    def schema_version(self) -> int:
        """Number of migrations applied to this database."""

    # --- capture ---------------------------------------------------------------------------

    @abstractmethod
    def add(self, item: Item) -> Item:
        """Save a new item and return it with its `id` set."""

    @abstractmethod
    def add_once(self, source: str, external_id: str, item: Item) -> tuple[Item, bool]:
        """Add an item for an inbound message unless that message was already captured.

        Returns (item, created). A retried webhook gets back the item from the first delivery. The
        check, the insert and the record happen atomically.
        """

    @abstractmethod
    def is_claimed(self, source: str, external_id: str) -> bool:
        """Whether this inbound message was already captured or acted on."""

    @abstractmethod
    def claim_inbound(self, source: str, external_id: str, item_id: int) -> bool:
        """Record an inbound message that acted on an existing item (e.g. an email reply 'done'),
        once the action succeeded. False if it was already recorded."""

    # --- reading ---------------------------------------------------------------------------

    @abstractmethod
    def get(self, item_id: int) -> Item | None:
        """The item with this id, or None."""

    @abstractmethod
    def list(self, include_done: bool = False) -> builtins.list[Item]:
        """Every item: reminders by when they next go out first, then notes by creation time."""

    @abstractmethod
    def counts(self) -> dict[tuple[str, str], int]:
        """Number of items per (kind, status)."""

    @abstractmethod
    def search(self, query: str) -> builtins.list[Item]:
        """Items whose text contains `query`, newest first. Case-insensitive for ASCII."""

    @abstractmethod
    def due(self, now: datetime) -> builtins.list[Item]:
        """Pending items whose due time or snoozed re-send has arrived, soonest first."""

    # --- actions ---------------------------------------------------------------------------

    @abstractmethod
    def set_status(self, item_id: int, status: Status) -> bool:
        """Set an item's status, whatever it was. False if there is no such item."""

    @abstractmethod
    def reschedule(self, item_id: int, due_at: datetime) -> bool:
        """Move an item to a new due time and make it pending again."""

    @abstractmethod
    def complete(self, item_id: int, via: str, now: datetime | None = None,
                 occurrence: datetime | None = None) -> bool:
        """Mark an item done because the person said so, acknowledging what was delivered
        (`occurrence` when the reply names it).

        For a recurring reminder, done is about one occurrence, not the series (ADR 0009): the
        delivered one awaiting acknowledgement, or else the upcoming one, which is skipped. `stop`
        ends a series.
        """

    @abstractmethod
    def stop(self, item_id: int, via: str, now: datetime | None = None) -> bool:
        """End an item for good, a recurring series included, acknowledging what was delivered."""

    @abstractmethod
    def snooze(self, item_id: int, until: datetime, via: str, now: datetime | None = None,
               occurrence: datetime | None = None) -> bool:
        """Push an item to `until` because the person asked, acknowledging what was delivered
        (`occurrence` when the reply names it).

        A recurring reminder keeps its series' time (ADR 0009): the occurrence is sent again at
        `until`, and series occurrences up to `until` are skipped.
        """

    @abstractmethod
    def delete(self, item_id: int) -> bool:
        """Delete an item and everything recorded about it. False if there was no such item."""

    # --- the scheduler's bookkeeping -------------------------------------------------------

    @abstractmethod
    def finish_occurrence(self, item_id: int, occurrence: datetime, next_due: datetime | None) -> bool:
        """The scheduler's update after delivering `occurrence`: mark it fired, or move a recurring
        item on to `next_due`. Only if the item is still pending at that occurrence, so a done or
        snooze that landed in the meantime (e.g. from another process) is never overwritten."""

    @abstractmethod
    def finish_snooze(self, item_id: int, occurrence: datetime) -> bool:
        """The scheduler's update after re-sending a snoozed occurrence of a recurring reminder;
        like finish_occurrence, only if nothing changed it in the meantime."""

    # --- deliveries and acknowledgements ---------------------------------------------------

    @abstractmethod
    def deliveries(self, item_id: int, occurrence: datetime) -> dict[str, Delivery]:
        """Delivery state of one occurrence, by channel."""

    @abstractmethod
    def record_delivery(self, item_id: int, occurrence: datetime, delivery: Delivery) -> None:
        """Save delivery state for one occurrence on one channel, replacing what was there."""

    @abstractmethod
    def latest_delivered_occurrence(self, item_id: int) -> datetime | None:
        """The most recent occurrence of this item that reached any channel."""

    @abstractmethod
    def last_delivered(self, channel: str) -> int | None:
        """The item most recently delivered on `channel` that still exists (for "reply done").
        Escalation stages on the same channel ("escalation2:<channel>") count too."""

    @abstractmethod
    def acknowledgement(self, item_id: int, occurrence: datetime) -> Acknowledgement | None:
        """How one occurrence was acknowledged, or None if nobody has yet. At most one per
        occurrence: the first done, snooze or reply wins."""

    @abstractmethod
    def unacknowledged(self, since: datetime | None = None) -> builtins.list[tuple[Item, datetime]]:
        """Items whose latest delivered occurrence nobody has acknowledged yet, with that
        occurrence, oldest delivery first; optionally only occurrences from `since` on."""

    @abstractmethod
    def unacknowledged_ids(self) -> frozenset[int]:
        """Cheap form of unacknowledged() for flags and counts."""

    # --- the scheduler lease (ADR 0007) ----------------------------------------------------

    @abstractmethod
    def lease_holder(self, now: datetime) -> str | None:
        """Who holds an unexpired scheduler lease, without taking a write lock."""

    @abstractmethod
    def acquire_lease(self, owner: str, now: datetime, expires_at: datetime) -> tuple[bool, str]:
        """Take or renew the scheduler lease if it is free, expired or already ours.

        Returns (acquired, current holder). The check-and-set is atomic across processes, so two
        of them can't both see the lease as free.
        """

    @abstractmethod
    def release_lease(self, owner: str) -> None:
        """Give up the lease (on shutdown) so a standby scheduler can take over at once."""

    # --- backup and restore (#59) ------------------------------------------------------------

    @abstractmethod
    def export_rows(self, table: TableSpec) -> builtins.list[Row]:
        """Every row of one backup table, as portable values (see `Row`). The tables of one backup
        are read in a single snapshot: items never reference deliveries the backup doesn't hold."""

    @abstractmethod
    def replace_all(self, rows: Mapping[str, builtins.list[Row]], now: datetime) -> None:
        """Replace the contents of every backup table with `rows`, atomically: after a crash
        mid-restore the old data is still there.

        Ids are kept exactly as given (they live on in chat buttons and reply flows), and new items
        after the restore never reuse an id the database has already handed out, even one the backup
        no longer contains. The stale scheduler lease is cleared. Raises RestoreBlockedError without
        touching anything if a scheduler holds an unexpired lease at `now` — the check happens inside
        the same transaction, so a scheduler can't start unnoticed between check and load.
        """
