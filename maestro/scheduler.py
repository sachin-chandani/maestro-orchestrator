"""Polls the store and fires reminders that have come due."""

import logging
import time
from datetime import datetime

from maestro import recurrence
from maestro.models import Item, Status
from maestro.notifier import Notifier
from maestro.store import Store

log = logging.getLogger(__name__)


def fire_due(store: Store, notifier: Notifier, now: datetime | None = None) -> list[Item]:
    now = now or datetime.now()
    fired = []
    for item in store.due(now):
        try:
            notifier.send(item)
        except Exception:
            # Leave it pending so the next tick retries; one broken channel shouldn't stop the loop.
            log.exception("Failed to deliver reminder #%s", item.id)
            continue
        if item.recurrence:
            # Schedule from now, not the old due time, so a long outage fires once rather than catching up.
            next_due = recurrence.next_occurrence(item.recurrence, max(now, item.due_at), item.due_at.time())
            store.reschedule(item.id, next_due)
        else:
            store.set_status(item.id, Status.FIRED)
        fired.append(item)
    return fired


def run_forever(store: Store, notifier: Notifier, interval: float = 30.0) -> None:
    while True:
        fire_due(store, notifier)
        time.sleep(interval)
