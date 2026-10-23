from datetime import timedelta

from conftest import dt

from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.parser import parse
from maestro.scheduler import MAX_ATTEMPTS, fire_due
from maestro.store import Store

DUE = dt(2026, 10, 23, 9, 0)


class Recorder:
    def __init__(self, failures: int = 0):
        self.sent = []
        self.failures = failures  # fail this many times, then succeed

    def send(self, item):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("network down")
        self.sent.append(item.id)


def _store_with_reminder() -> tuple[Store, Item]:
    store = Store()
    item = store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=DUE, due_at=DUE))
    return store, item


def test_failing_channel_does_not_resend_on_healthy_one():
    # Regression for #2: a failure on one channel used to re-send on every channel next tick.
    store, item = _store_with_reminder()
    push, sms = Recorder(), Recorder(failures=1)
    notifier = MultiNotifier({"push": push, "sms": sms})

    assert fire_due(store, notifier, now=DUE) == []
    assert store.get(item.id).status == Status.PENDING

    # Still inside the backoff window: nothing is retried.
    fire_due(store, notifier, now=DUE + timedelta(seconds=30))
    assert sms.sent == []

    finished = fire_due(store, notifier, now=DUE + timedelta(minutes=2))
    assert [i.id for i in finished] == [item.id]
    assert push.sent == [item.id]  # exactly once
    assert sms.sent == [item.id]
    assert store.get(item.id).status == Status.FIRED


def test_gives_up_after_max_attempts():
    store, item = _store_with_reminder()
    broken, ok = Recorder(failures=999), Recorder()
    notifier = MultiNotifier({"broken": broken, "ok": ok})

    now = DUE
    for _ in range(MAX_ATTEMPTS):
        fire_due(store, notifier, now=now)
        now += timedelta(hours=1)  # always past the backoff

    assert store.get(item.id).status == Status.FIRED
    assert ok.sent == [item.id]
    state = store.deliveries(item.id, DUE)
    assert state["broken"].attempts == MAX_ATTEMPTS
    assert state["broken"].delivered_at is None
    assert state["broken"].last_error == "ConnectionError: network down"


def test_backoff_grows_between_attempts():
    store, item = _store_with_reminder()
    notifier = MultiNotifier({"sms": Recorder(failures=999)})

    fire_due(store, notifier, now=DUE)
    first = store.deliveries(item.id, DUE)["sms"].next_attempt_at
    fire_due(store, notifier, now=first)
    second = store.deliveries(item.id, DUE)["sms"].next_attempt_at
    assert first - DUE == timedelta(minutes=1)
    assert second - first == timedelta(minutes=2)


def test_each_recurrence_is_delivered_separately():
    store = Store()
    item = store.add(parse("take vitamins every day at 8am", now=dt(2026, 10, 23, 7, 0)))
    push = Recorder()
    notifier = MultiNotifier({"push": push})

    fire_due(store, notifier, now=dt(2026, 10, 23, 8, 0))
    fire_due(store, notifier, now=dt(2026, 10, 24, 8, 0))
    assert push.sent == [item.id, item.id]


def test_delete_removes_delivery_rows():
    store, item = _store_with_reminder()
    fire_due(store, MultiNotifier({"sms": Recorder(failures=1)}), now=DUE)
    assert store.deliveries(item.id, DUE)
    store.delete(item.id)
    assert store.deliveries(item.id, DUE) == {}
