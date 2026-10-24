"""Polls the store and fires reminders that have come due.

Delivery is tracked per channel for each occurrence. A channel that fails is retried with
exponential backoff on later ticks without re-sending on channels that already succeeded.
After MAX_ATTEMPTS a channel is given up on so one broken integration can't block a reminder forever.

When several schedulers share a database (serve + run, or two replicas), a lease in the database lets
only one of them fire; the others stand by and take over when the lease expires (see Lease).
"""

import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from maestro import recurrence
from maestro.models import Delivery, Item, Status
from maestro.notifier import MultiNotifier, Notifier
from maestro.observability import METRICS
from maestro.store import Store
from maestro.tz import UTC, localize

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_BACKOFF = timedelta(minutes=30)


def _default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class Lease:
    """Leadership among schedulers sharing one database.

    The holder renews the lease every tick and before each reminder; if it stops (crash, kill, hang),
    another scheduler takes over once `ttl` has passed. Exactly one scheduler fires in normal
    operation. A leader frozen for longer than `ttl` mid-tick could still overlap with its successor
    for one reminder, so keep `ttl` well above the tick interval (the default is three ticks).
    """

    ttl: timedelta = timedelta(seconds=90)
    owner: str = field(default_factory=_default_owner)
    held: bool | None = None  # last known state, to log only on changes

    @classmethod
    def for_interval(cls, interval: float) -> "Lease":
        return cls(ttl=timedelta(seconds=max(3 * interval, 30)))

    def acquire(self, store: Store, now: datetime) -> bool:
        acquired, holder = store.acquire_lease(self.owner, now, now + self.ttl)
        if acquired != self.held:
            if acquired:
                log.info("Scheduler %s is now firing reminders", self.owner)
            else:
                log.info("Scheduler %s is standing by: %s holds the lease", self.owner, holder)
        self.held = acquired
        METRICS.set("maestro_scheduler_leader", 1 if acquired else 0)
        return acquired

    def release(self, store: Store) -> None:
        store.release_lease(self.owner)
        self.held = False
        METRICS.set("maestro_scheduler_leader", 0)


def _backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** (attempts - 1)), MAX_BACKOFF)


def _channels(notifier: Notifier) -> dict[str, Notifier]:
    return notifier.channels if isinstance(notifier, MultiNotifier) else {"default": notifier}


def _occurrence(item: Item) -> tuple[int, datetime] | None:
    """The (id, due time) of a stored, scheduled item, or None for anything else."""
    if item.id is None or item.due_at is None:
        return None
    return item.id, item.due_at


def _deliver(store: Store, item: Item, item_id: int, due_at: datetime, channels: dict[str, Notifier],
             now: datetime) -> bool:
    """Try every channel that still needs this occurrence. True once none are left to retry."""
    state = store.deliveries(item_id, due_at)
    waiting = False
    for name, channel in channels.items():
        delivery = state.get(name) or Delivery(channel=name)
        if delivery.delivered_at or delivery.attempts >= MAX_ATTEMPTS:
            continue
        if delivery.next_attempt_at and delivery.next_attempt_at > now:
            waiting = True
            continue
        delivery.attempts += 1
        try:
            channel.send(item)
        except Exception as e:
            delivery.last_error = f"{type(e).__name__}: {e}"[:500]
            if delivery.attempts >= MAX_ATTEMPTS:
                METRICS.inc("maestro_deliveries_total", channel=name, result="gave_up")
                log.error("Giving up on reminder #%s via %s after %d attempts: %s",
                          item.id, name, delivery.attempts, delivery.last_error)
            else:
                METRICS.inc("maestro_deliveries_total", channel=name, result="error")
                delivery.next_attempt_at = now + _backoff(delivery.attempts)
                waiting = True
                log.warning("Reminder #%s via %s failed (attempt %d), retrying at %s: %s", item.id, name,
                            delivery.attempts, delivery.next_attempt_at, delivery.last_error)
        else:
            METRICS.inc("maestro_deliveries_total", channel=name, result="ok")
            delivery.delivered_at = now
            delivery.next_attempt_at = None
        store.record_delivery(item_id, due_at, delivery)
    return not waiting


def fire_due(store: Store, notifier: Notifier, now: datetime | None = None, lease: Lease | None = None
             ) -> list[Item]:
    """Deliver due reminders. Returns the items whose occurrence is finished this tick.

    With a lease, nothing is sent unless this scheduler holds it, and it is renewed before each item.
    """
    now = localize(now, store.tz) if now else datetime.now(UTC)
    if lease and not lease.acquire(store, now):
        return []
    channels = _channels(notifier)
    finished = []
    due: list[tuple[Item, int, datetime]] = []
    for item in store.due(now):
        if (occurrence := _occurrence(item)) is None:
            METRICS.inc("maestro_scheduler_errors_total")
            log.error("Skipping malformed due item %r", item)
            continue
        due.append((item, *occurrence))
    METRICS.inc("maestro_scheduler_ticks_total")
    METRICS.set("maestro_scheduler_last_tick_timestamp_seconds", now.timestamp())
    lag = max(((now - due_at).total_seconds() for _, _, due_at in due), default=0)
    METRICS.set("maestro_scheduler_lag_seconds", lag)
    for item, item_id, due_at in due:
        if lease and not lease.acquire(store, now):
            break  # lost the lease mid-tick: the new holder takes it from here
        try:
            if not _deliver(store, item, item_id, due_at, channels, now):
                continue
            METRICS.inc("maestro_reminders_fired_total")
            if item.recurrence:
                # Schedule from now, not the old due time, so a long outage fires once rather than catching
                # up. Work in the item's timezone so the next occurrence keeps its wall-clock time.
                after = max(now, due_at).astimezone(due_at.tzinfo)
                next_due = recurrence.next_occurrence(item.recurrence, after, due_at.time(), anchor=due_at)
                store.reschedule(item_id, next_due)
            else:
                store.set_status(item_id, Status.FIRED)
            finished.append(item)
        except Exception:
            # One broken item (e.g. a corrupt recurrence rule) must not stop the others.
            METRICS.inc("maestro_scheduler_errors_total")
            log.exception("Failed to process reminder #%s", item_id)
    return finished


def tick(store: Store, notifier: Notifier, lease: Lease | None = None) -> None:
    """One scheduler pass for a long-running loop: errors are logged and counted, never raised."""
    try:
        fire_due(store, notifier, lease=lease)
    except Exception:
        METRICS.inc("maestro_scheduler_errors_total")
        log.exception("Scheduler tick failed; retrying next tick")


def run_forever(store: Store, notifier: Notifier, interval: float = 30.0,
                wait: Callable[[float], None] = time.sleep, lease: Lease | None = None) -> None:
    """Fire due reminders every `interval` seconds. `wait` can do useful work while it waits,
    e.g. long-poll a chat bot for new messages. The lease is released on the way out (Ctrl+C,
    SIGTERM handled as an exception) so a standby scheduler can take over at once."""
    lease = lease or Lease.for_interval(interval)
    try:
        while True:
            tick(store, notifier, lease)
            wait(interval)
    finally:
        try:
            lease.release(store)
        except Exception:
            log.exception("Could not release the scheduler lease; it will expire on its own")
