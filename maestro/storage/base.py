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
from datetime import date, datetime
from types import TracebackType
from typing import Self
from zoneinfo import ZoneInfo

from maestro.models import Acknowledgement, Capture, Delivery, Device, Item, Kind, Status
from maestro.storage.logic import ACK_WINDOW

__all__ = ["ACK_WINDOW", "SchemaTooNewError", "Store"]


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer Maestro than this one."""


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

    # --- captures awaiting confirmation (#117) ----------------------------------------------

    @abstractmethod
    def add_capture(self, capture: Capture, dedupe_key: str | None = None) -> tuple[Capture, bool]:
        """Save a capture awaiting confirmation and return it with its `id` set.

        Returns (capture, created). With a `dedupe_key` (the provider's message id), a retried
        webhook gets back the capture from the first delivery instead of being asked twice.

        Whatever the capture's `status`, `undo_until`, `decided_at` and `decided_via` say is stored
        as given: a split capture (#126) is recorded as already applied, so its undo window starts
        the moment it is saved rather than when someone answers a question.
        """

    @abstractmethod
    def get_capture(self, capture_id: int) -> Capture | None:
        """The capture with this id, or None."""

    @abstractmethod
    def confirm_capture(self, capture_id: int, via: str, now: datetime,
                        undo_until: datetime) -> bool:
        """Claim a pending, unexpired capture as applied: the one atomic check-and-set that
        decides which of two simultaneous confirmations acts (#117). False when it was already
        decided, expired, or never existed; the caller applies the actions only on True."""

    @abstractmethod
    def cancel_capture(self, capture_id: int, via: str, now: datetime) -> bool:
        """Mark a pending capture cancelled. False when it wasn't pending."""

    @abstractmethod
    def undo_capture(self, capture_id: int, via: str, now: datetime) -> bool:
        """Claim an applied capture as undone, inside its undo window; same check-and-set rules
        as confirm_capture. The caller deletes the items only on True."""

    @abstractmethod
    def expire_captures(self, now: datetime) -> int:
        """Mark pending captures whose expires_at has passed as expired; returns how many.
        They stay listed (visible, never applied); confirm_capture checks the time itself, so a
        late sweep can't let a stale confirmation through."""

    @abstractmethod
    def captures(self, status: str | None = None, source: str | None = None,
                 address: str | None = None) -> builtins.list[Capture]:
        """Captures, newest first, optionally filtered by status and by owner (source, address)."""

    @abstractmethod
    def link_capture(self, capture_id: int, item_ids: builtins.list[int]) -> None:
        """Record which items a confirmed capture created, so undo knows what to remove."""

    @abstractmethod
    def items_for_capture(self, capture_id: int) -> builtins.list[Item]:
        """The items a confirmed capture created (empty once undone)."""

    # --- once-a-day marks (#128) -------------------------------------------------------------

    @abstractmethod
    def claim_daily(self, name: str, day: date) -> bool:
        """Claim `name` for `day`, exactly once across every process: True for the claimer,
        False ever after. How the evening wrap-up sends one notice however many schedulers run."""

    # --- devices signed in with their own token (#108, ADR 0018) ----------------------------

    @abstractmethod
    def add_device(self, device: Device) -> Device:
        """Save a device and return it with its `id` set. `token_hash` must be unique."""

    @abstractmethod
    def find_device(self, token_hash: str) -> Device | None:
        """The device whose token hashes to this, unless it was revoked. How a request is
        authenticated, so it must be one indexed lookup."""

    @abstractmethod
    def get_device(self, device_id: int) -> Device | None:
        """The device with this id, revoked or not, or None."""

    @abstractmethod
    def devices(self) -> builtins.list[Device]:
        """Devices that can still sign in, oldest first."""

    @abstractmethod
    def revoke_device(self, device_id: int, now: datetime) -> bool:
        """Stop a device's token working, keeping its row. False if it was unknown or already revoked."""

    @abstractmethod
    def touch_device(self, device_id: int, now: datetime) -> None:
        """Record that the device was just seen. Called at most once an hour per device, so
        authenticating a request is usually a read alone."""

    # --- readings that need a look (#126, ADR 0017) -----------------------------------------

    @abstractmethod
    def needs_look(self, source: str | None = None, address: str | None = None,
                   since: datetime | None = None) -> builtins.list[Item]:
        """Items saved with a gap, newest first: visible, never scheduled, never expiring.

        `source` and `address` restrict them to the owner of the capture they came from, so one
        sender's reply can only ever reach their own; `since` restricts them to recent ones, for
        the bare-reply shortcut. Items that are done are left out.
        """

    @abstractmethod
    def fill_gap(self, item_id: int, due_at: datetime | None = None, recurrence: str | None = None,
                 channels: builtins.list[str] | None = None, text: str | None = None,
                 kind: Kind | None = None) -> bool:
        """Fill an item's gap and clear it, so it can be acted on at last.

        A `due_at` makes it a pending reminder (with `recurrence`, which is set whenever `due_at`
        is, so filling a one-off time over a repeat really does clear the repeat); `channels` sets
        where it goes; `text` replaces words the capture never had, and `kind` the kind those words
        turn out to mean, overriding what `due_at` implies. False when the item is gone or had no
        gap, which is the check-and-set that makes two simultaneous answers safe: exactly one of
        them fills it.
        """

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
        """Pending items whose due time or snoozed re-send has arrived, soonest first.

        Items still carrying a gap (#126) are never due, however their times read: nobody has
        confirmed what they mean, so the scheduler must not deliver them.
        """

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
