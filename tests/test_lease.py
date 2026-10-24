"""#52: several schedulers on one database deliver each reminder exactly once."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from conftest import dt

from maestro.models import Item, Kind
from maestro.notifier import MultiNotifier
from maestro.observability import METRICS
from maestro.scheduler import Lease, fire_due, run_forever
from maestro.store import Store

NOW = dt(2026, 10, 23, 9, 0)
TTL = timedelta(seconds=90)


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item.id)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shared.db"


def add_reminders(path, count=3):
    with Store(path) as store:
        for n in range(count):
            store.add(Item(text=f"Reminder {n}", kind=Kind.REMINDER, created_at=NOW, due_at=NOW))


def test_two_schedulers_deliver_each_reminder_once(path):
    add_reminders(path, 5)
    a_store, b_store = Store(path), Store(path)  # e.g. `maestro serve` and `maestro run`
    a, b = Lease(ttl=TTL, owner="a"), Lease(ttl=TTL, owner="b")
    a_sent, b_sent = Recorder(), Recorder()

    for minute in range(3):
        now = NOW + timedelta(minutes=minute)
        fire_due(a_store, MultiNotifier({"push": a_sent}), now=now, lease=a)
        fire_due(b_store, MultiNotifier({"push": b_sent}), now=now, lease=b)

    assert sorted(a_sent.sent + b_sent.sent) == [1, 2, 3, 4, 5]  # exactly once each
    assert b_sent.sent == []  # b only stood by
    assert (a.held, b.held) == (True, False)


def test_standby_takes_over_when_the_leader_stops_renewing(path):
    add_reminders(path, 1)
    store = Store(path)
    a, b = Lease(ttl=TTL, owner="a"), Lease(ttl=TTL, owner="b")
    assert a.acquire(store, NOW)  # a takes the lease, then dies before firing

    b_sent = Recorder()
    assert fire_due(store, MultiNotifier({"push": b_sent}), now=NOW + timedelta(seconds=60), lease=b) == []
    finished = fire_due(store, MultiNotifier({"push": b_sent}), now=NOW + timedelta(seconds=91), lease=b)
    assert [i.id for i in finished] == [1] and b_sent.sent == [1]


def test_release_hands_over_immediately(path):
    store = Store(path)
    a, b = Lease(ttl=TTL, owner="a"), Lease(ttl=TTL, owner="b")
    assert a.acquire(store, NOW)
    assert not b.acquire(store, NOW + timedelta(seconds=1))
    a.release(store)
    assert b.acquire(store, NOW + timedelta(seconds=2))


def test_leader_that_lost_the_lease_mid_tick_stops(path, monkeypatch):
    add_reminders(path, 3)
    store = Store(path)
    a, b = Lease(ttl=TTL, owner="a"), Lease(ttl=TTL, owner="b")
    sent = Recorder()

    calls = {"n": 0}
    real_acquire = Store.acquire_lease

    def steal_after_first_item(self, owner, now, expires_at):
        calls["n"] += 1
        if calls["n"] == 3:  # tick start, item 1, then b takes over before item 2
            self.release_lease("a")
            real_acquire(self, "b", now, expires_at)
        return real_acquire(self, owner, now, expires_at)

    monkeypatch.setattr(Store, "acquire_lease", steal_after_first_item)
    finished = fire_due(store, MultiNotifier({"push": sent}), now=NOW, lease=a)
    assert [i.id for i in finished] == [1]
    assert sent.sent == [1]
    assert a.held is False and b.held is None


def test_transitions_are_logged_and_exported(path, caplog):
    store = Store(path)
    a, b = Lease(ttl=TTL, owner="a"), Lease(ttl=TTL, owner="b")
    with caplog.at_level(logging.INFO):
        a.acquire(store, NOW)
        a.acquire(store, NOW + timedelta(seconds=30))  # renewal: no new log line
        b.acquire(store, NOW + timedelta(seconds=31))
    assert caplog.text.count("a is now firing reminders") == 1
    assert "b is standing by: a holds the lease" in caplog.text
    assert METRICS.get("maestro_scheduler_leader") == 0  # b acted last


def test_lease_ttl_follows_the_interval():
    assert Lease.for_interval(30).ttl == timedelta(seconds=90)
    assert Lease.for_interval(1).ttl == timedelta(seconds=30)  # never shorter than 30 s


def test_run_forever_releases_the_lease_on_exit(path):
    store = Store(path)
    lease = Lease(ttl=TTL, owner="a")

    def stop(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_forever(store, MultiNotifier({}), interval=30, wait=stop, lease=lease)
    assert Lease(ttl=TTL, owner="b").acquire(store, dt(2000, 1, 1))  # free straight away


def test_cron_style_run_once_respects_a_running_server(path, monkeypatch, capsys):
    from maestro.cli import main

    add_reminders(path, 1)
    with Store(path) as store:
        server = Lease(ttl=timedelta(hours=1), owner="serve")
        assert server.acquire(store, datetime.now(UTC))  # the CLI runs on the real clock
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    assert main(["--db", str(path), "run", "--once"]) == 0
    assert "Reminder #1" not in capsys.readouterr().out  # the server delivers it, not cron
