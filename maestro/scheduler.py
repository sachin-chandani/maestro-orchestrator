"""Polls the store and fires reminders that have come due.

Delivery is tracked per channel for each occurrence. A channel that fails is retried with
exponential backoff on later ticks without re-sending on channels that already succeeded.
After MAX_ATTEMPTS a channel is given up on so one broken integration can't block a reminder forever.

When several schedulers share a database (serve + run, or two replicas), a lease in the database lets
only one of them fire; the others stand by and take over when the lease expires (see Lease).
"""

import dataclasses
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from maestro import recurrence
from maestro.models import Delivery, Item
from maestro.notifier import MultiNotifier, Notifier
from maestro.observability import METRICS
from maestro.quiet import is_urgent
from maestro.storage import Store
from maestro.tz import UTC, localize

if TYPE_CHECKING:
    from maestro.escalation import Stage
    from maestro.quiet import QuietHours

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_BACKOFF = timedelta(minutes=30)


def _default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Lease:
    """Leadership among schedulers sharing one database (ADR 0007).

    The holder renews the lease as it works; if it stops (crash, kill, hang), another scheduler takes
    over once `ttl` has passed. Renewals always use the real current time, so a long tick (slow
    channels) keeps the lease alive. A leader frozen for longer than `ttl` mid-reminder could still
    overlap with its successor for that reminder, so `ttl` defaults to three ticks.
    """

    ttl: timedelta = timedelta(seconds=90)
    owner: str = field(default_factory=_default_owner)
    clock: Callable[[], datetime] = _utc_now
    held: bool | None = None  # last known state, to log only on changes
    renewed_at: datetime | None = None

    @classmethod
    def for_interval(cls, interval: float) -> "Lease":
        return cls(ttl=timedelta(seconds=max(3 * interval, 30)))

    def acquire(self, store: Store, now: datetime) -> bool:
        now = max(now, self.clock())  # never renew against a stale tick-start time
        if self.held and self.renewed_at and now - self.renewed_at < self.ttl / 3:
            return True  # renewed recently: nobody can have taken it, skip the write
        if not self.held and (holder := store.lease_holder(now)) not in (None, self.owner):
            return self._settle(False, holder)  # cheap read: standbys don't take the write lock
        acquired, holder = store.acquire_lease(self.owner, now, now + self.ttl)
        if acquired:
            self.renewed_at = now
        return self._settle(acquired, holder)

    def _settle(self, acquired: bool, holder: str) -> bool:
        if acquired != self.held:
            if acquired:
                log.info("Scheduler %s is now firing reminders", self.owner)
            else:
                log.info("Scheduler %s is standing by: %s holds the lease", self.owner, holder)
        self.held = acquired
        if not acquired:
            self.renewed_at = None
        METRICS.set("maestro_scheduler_leader", 1 if acquired else 0)
        return acquired

    def release(self, store: Store) -> None:
        """Hand the lease over (on shutdown) if we hold it. Never raises: it expires on its own anyway."""
        if not self.held:
            return
        try:
            store.release_lease(self.owner)
        except Exception:
            log.exception("Could not release the scheduler lease; it will expire on its own")
        self.held, self.renewed_at = False, None
        METRICS.set("maestro_scheduler_leader", 0)


def _backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** (attempts - 1)), MAX_BACKOFF)


def _channels(notifier: Notifier) -> dict[str, Notifier]:
    return notifier.channels if isinstance(notifier, MultiNotifier) else {"default": notifier}


def _occurrence(item: Item, now: datetime) -> tuple[int, datetime, bool] | None:
    """(id, occurrence, whether it's a snoozed one) for a stored, scheduled item that's due, else None.
    A snoozed occurrence of a recurring reminder goes first (it's always before the series' next one),
    unless the next one is due too (after downtime): then that one is sent and the snooze is dropped."""
    if item.id is None:
        return None
    series_due = item.due_at is not None and item.due_at <= now
    if item.snoozed_until is not None and item.snoozed_until <= now and not series_due:
        return item.id, item.snoozed_until, True
    if item.due_at is None:
        return None
    return item.id, item.due_at, False


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


def fire_due(store: Store, notifier: Notifier, now: datetime | None = None, lease: Lease | None = None,
             escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None) -> list[Item]:
    """Deliver due reminders, then any escalation stages that are due. Returns the items whose
    occurrence is finished this tick.

    With a lease, nothing is sent unless this scheduler holds it, and it is renewed before each item.
    During quiet hours, non-urgent reminders and their escalations are held (left due) until the window ends.
    """
    now = localize(now, store.tz) if now else datetime.now(UTC)
    if lease and not lease.acquire(store, now):
        if quiet is not None:
            METRICS.set("maestro_reminders_held", 0)  # the leader reports what it holds
        return []
    channels = _channels(notifier)
    local = now.astimezone(store.tz)

    def held(item: Item) -> bool:
        return quiet is not None and quiet.holds(item, local)

    finished = []
    due: list[tuple[Item, int, datetime, bool]] = []
    holding = 0
    for item in store.due(now):
        if (occurrence := _occurrence(item, now)) is None:
            METRICS.inc("maestro_scheduler_errors_total")
            log.error("Skipping malformed due item %r", item)
            continue
        if held(item):
            holding += 1
            continue
        due.append((item, *occurrence))
    if quiet is not None:
        METRICS.set("maestro_reminders_held", holding)
        if holding:
            log.debug("Quiet hours: holding %d reminder(s) until %s", holding, quiet.ends_after(local))
    METRICS.inc("maestro_scheduler_ticks_total")
    METRICS.set("maestro_scheduler_last_tick_timestamp_seconds", now.timestamp())
    def late_since(item: Item, due_at: datetime) -> datetime:
        # A reminder held by quiet hours is on time when the window ends, not late by the whole night.
        if quiet is not None and not is_urgent(item):
            return max(due_at, quiet.last_end(local))
        return due_at

    lag = max(((now - late_since(item, due_at)).total_seconds() for item, _, due_at, _ in due), default=0)
    METRICS.set("maestro_scheduler_lag_seconds", lag)
    for item, item_id, due_at, snoozed in due:
        if lease and not lease.acquire(store, now):
            break  # lost the lease mid-tick: the new holder takes it from here
        try:
            # Channels see the occurrence being sent as the due time (it's in message ids and call URLs).
            notice = dataclasses.replace(item, due_at=due_at, snoozed_until=None) if snoozed else item
            if not _deliver(store, notice, item_id, due_at, channels, now):
                continue
            METRICS.inc("maestro_reminders_fired_total")
            if snoozed:
                store.finish_snooze(item_id, due_at)  # the series itself stays where it is
                finished.append(item)
                continue
            if item.snoozed_until is not None:  # overtaken by this occurrence: one catch-up notice, not two
                store.finish_snooze(item_id, item.snoozed_until)
            if item.recurrence:
                # Schedule from now, not the old due time, so a long outage fires once rather than catching
                # up. Work in the item's timezone so the next occurrence keeps its wall-clock time.
                after = max(now, due_at).astimezone(due_at.tzinfo)
                next_due = recurrence.next_occurrence(item.recurrence, after, due_at.time(), anchor=due_at)
                store.finish_occurrence(item_id, due_at, next_due)
            else:
                store.finish_occurrence(item_id, due_at, None)
            finished.append(item)
        except Exception:
            # One broken item (e.g. a corrupt recurrence rule) must not stop the others.
            METRICS.inc("maestro_scheduler_errors_total")
            log.exception("Failed to process reminder #%s", item_id)
    if escalation and (not lease or lease.acquire(store, now)):
        from maestro.escalation import escalate

        escalate(store, escalation, now, _deliver, MAX_ATTEMPTS,
                 renew=(lambda: lease.acquire(store, now)) if lease else (lambda: True), quiet=quiet)
    return finished


def tick(store: Store, notifier: Notifier, lease: Lease | None = None,
         escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None) -> None:
    """One scheduler pass for a long-running loop: errors are logged and counted, never raised."""
    try:
        fire_due(store, notifier, lease=lease, escalation=escalation, quiet=quiet)
    except Exception:
        METRICS.inc("maestro_scheduler_errors_total")
        log.exception("Scheduler tick failed; retrying next tick")


def run_forever(store: Store, notifier: Notifier, interval: float = 30.0,
                wait: Callable[[float], None] = time.sleep, lease: Lease | None = None,
                escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None) -> None:
    """Fire due reminders every `interval` seconds. `wait` can do useful work while it waits,
    e.g. long-poll a chat bot for new messages. The lease is released on the way out (Ctrl+C,
    SIGTERM handled as an exception) so a standby scheduler can take over at once."""
    lease = lease or Lease.for_interval(interval)
    try:
        while True:
            tick(store, notifier, lease, escalation, quiet)
            wait(interval)
    finally:
        lease.release(store)
