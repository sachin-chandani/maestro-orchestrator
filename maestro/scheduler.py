"""Polls the store and fires reminders that have come due."""

import time
from datetime import datetime

from maestro.models import Item, Status
from maestro.notifier import Notifier
from maestro.store import Store


def fire_due(store: Store, notifier: Notifier, now: datetime | None = None) -> list[Item]:
    fired = []
    for item in store.due(now or datetime.now()):
        notifier.send(item)
        store.set_status(item.id, Status.FIRED)
        fired.append(item)
    return fired


def run_forever(store: Store, notifier: Notifier, interval: float = 30.0) -> None:
    while True:
        fire_due(store, notifier)
        time.sleep(interval)
