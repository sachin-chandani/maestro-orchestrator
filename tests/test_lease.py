"""#52: several schedulers on one database deliver each reminder exactly once."""

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import dt

from maestro.models import Item, Kind
from maestro.notifier import MultiNotifier
from maestro.observability import METRICS
from maestro.scheduler import Lease, fire_due, run_forever
from maestro.store import SchemaTooNewError, Store

NOW = dt(2026, 10, 23, 9, 0)
TTL = timedelta(seconds=90)


class Clock:
    """A controllable clock; the scheduler never sees the real time in these tests."""

    def __init__(self, at=NOW):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, **delta):
        self.at += timedelta(**delta)


def lease(owner, clock):
    return Lease(ttl=TTL, owner=owner, clock=clock)


class Recorder:
    def __init__(self, on_send=None):
        self.sent = []
        self.on_send = on_send

    def send(self, item):
        self.sent.append(item.id)
        if self.on_send:
            self.on_send()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shared.db"


def add_reminders(path, count=3):
    with Store(path) as store:
        for n in range(count):
            store.add(Item(text=f"Reminder {n}", kind=Kind.REMINDER, created_at=NOW, due_at=NOW))


def test_two_schedulers_deliver_each_reminder_once(path):
    add_reminders(path, 5)
    clock = Clock()
    a_store, b_store = Store(path), Store(path)  # e.g. `maestro serve` and `maestro run`
    a, b = lease("a", clock), lease("b", clock)
    a_sent, b_sent = Recorder(), Recorder()

    for _ in range(3):
        fire_due(a_store, MultiNotifier({"push": a_sent}), now=clock(), lease=a)
        fire_due(b_store, MultiNotifier({"push": b_sent}), now=clock(), lease=b)
        clock.advance(seconds=30)

    assert sorted(a_sent.sent + b_sent.sent) == [1, 2, 3, 4, 5]  # exactly once each
    assert b_sent.sent == []
    assert (a.held, b.held) == (True, False)


def test_standby_takes_over_when_the_leader_stops_renewing(path):
    add_reminders(path, 1)
    clock = Clock()
    store = Store(path)
    a, b = lease("a", clock), lease("b", clock)
    assert a.acquire(store, clock())  # a takes the lease, then dies before firing

    b_sent = Recorder()
    clock.advance(seconds=60)
    assert fire_due(store, MultiNotifier({"push": b_sent}), now=clock(), lease=b) == []
    clock.advance(seconds=31)
    finished = fire_due(store, MultiNotifier({"push": b_sent}), now=clock(), lease=b)
    assert [i.id for i in finished] == [1] and b_sent.sent == [1]


def test_a_long_tick_keeps_its_lease(path):
    # Regression: renewals used the tick's start time, so a slow tick let a standby take over and
    # send the same reminders again.
    add_reminders(path, 4)
    clock = Clock()
    a_store, b_store = Store(path), Store(path)
    a, b = lease("a", clock), lease("b", clock)
    b_sent = Recorder()

    def slow_channel_while_b_ticks():
        clock.advance(seconds=40)  # each send takes 40 s: the tick lasts well past the 90 s TTL
        fire_due(b_store, MultiNotifier({"push": b_sent}), now=clock(), lease=b)

    a_sent = Recorder(on_send=slow_channel_while_b_ticks)
    fire_due(a_store, MultiNotifier({"push": a_sent}), now=clock(), lease=a)

    assert a_sent.sent == [1, 2, 3, 4]
    assert b_sent.sent == []
    assert b.held is False


def test_leader_stalled_past_the_ttl_stops_mid_tick(path):
    add_reminders(path, 3)
    clock = Clock()
    a_store, b_store = Store(path), Store(path)
    a, b = lease("a", clock), lease("b", clock)

    def stall_then_b_takes_over():
        clock.advance(seconds=200)  # e.g. a suspended VM: a's lease expires
        b.acquire(b_store, clock())

    a_sent = Recorder(on_send=stall_then_b_takes_over)
    finished = fire_due(a_store, MultiNotifier({"push": a_sent}), now=clock(), lease=a)
    assert [i.id for i in finished] == [1]  # stops instead of racing b for items 2 and 3
    assert (a.held, b.held) == (False, True)


def test_standbys_do_not_take_the_write_lock(path, monkeypatch):
    clock = Clock()
    store = Store(path)
    assert lease("a", clock).acquire(store, clock())
    writes = []
    real = Store.acquire_lease
    monkeypatch.setattr(Store, "acquire_lease", lambda self, *a: writes.append(a) or real(self, *a))
    b = lease("b", clock)
    for _ in range(5):
        assert not b.acquire(store, clock())
    assert writes == []


def test_leader_renews_at_most_every_third_of_the_ttl(path, monkeypatch):
    clock = Clock()
    store = Store(path)
    a = lease("a", clock)
    writes = []
    real = Store.acquire_lease
    monkeypatch.setattr(Store, "acquire_lease", lambda self, *a: writes.append(a) or real(self, *a))
    for _ in range(10):  # 10 checks within 20 s
        assert a.acquire(store, clock())
        clock.advance(seconds=2)
    assert len(writes) == 1
    clock.advance(seconds=30)
    assert a.acquire(store, clock())
    assert len(writes) == 2


def test_release_hands_over_immediately_and_only_when_held(path, monkeypatch):
    clock = Clock()
    store = Store(path)
    a, b = lease("a", clock), lease("b", clock)
    assert a.acquire(store, clock())
    assert not b.acquire(store, clock())
    released = []
    real = Store.release_lease
    monkeypatch.setattr(Store, "release_lease",
                        lambda self, owner: released.append(owner) or real(self, owner))
    b.release(store)  # not held: no write
    a.release(store)
    assert released == ["a"]
    assert b.acquire(store, clock())


def test_transitions_are_logged_and_exported(path, caplog):
    clock = Clock()
    store = Store(path)
    a, b = lease("a", clock), lease("b", clock)
    with caplog.at_level(logging.INFO):
        a.acquire(store, clock())
        clock.advance(seconds=30)
        a.acquire(store, clock())  # renewal: no new log line
        b.acquire(store, clock())
    assert caplog.text.count("a is now firing reminders") == 1
    assert "b is standing by: a holds the lease" in caplog.text
    assert METRICS.get("maestro_scheduler_leader") == 0  # b acted last


def test_lease_ttl_follows_the_interval():
    assert Lease.for_interval(30).ttl == timedelta(seconds=90)
    assert Lease.for_interval(1).ttl == timedelta(seconds=30)  # never shorter than 30 s


def test_run_forever_releases_the_lease_on_exit(path):
    clock = Clock()
    store = Store(path)

    def stop(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_forever(store, MultiNotifier({}), interval=30, wait=stop, lease=lease("a", clock))
    assert lease("b", clock).acquire(store, clock())  # free straight away


def test_cron_style_run_once_respects_a_running_server(path, monkeypatch, capsys):
    from maestro.cli import main

    add_reminders(path, 1)
    with Store(path) as store:
        assert Lease(ttl=timedelta(hours=1), owner="serve").acquire(store, datetime.now(UTC))
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    assert main(["--db", str(path), "run", "--once"]) == 0
    assert "Reminder #1" not in capsys.readouterr().out  # the server delivers it, not cron


def test_sigterm_releases_the_lease(path, tmp_path):
    """`docker stop` / `systemctl stop` must hand over at once, not after the TTL."""
    env = {**os.environ, "MAESTRO_DB": str(path), "MAESTRO_CHANNELS": "console"}
    proc = subprocess.Popen([sys.executable, "-m", "maestro.cli", "run", "--interval", "30"],
                            env=env, cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with Store(path) as store:
                if store.lease_holder(datetime.now(UTC)):
                    break
            time.sleep(0.05)
        else:
            pytest.fail("maestro run never took the lease")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.wait()
    with Store(path) as store:
        assert store.lease_holder(datetime.now(UTC)) is None


def test_older_maestro_refuses_a_newer_database(path):
    with Store(path) as store:
        store.conn.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaTooNewError, match="schema 99 is newer"):
        Store(path)


def test_a_failed_write_does_not_wedge_the_shared_connection(path, caplog):
    import sqlite3

    clock = Clock()
    store = Store(path)
    store.conn.execute("INSERT INTO items (text, kind, status, created_at)"
                       " VALUES ('x', 'note', 'pending', '2026-10-23T00:00:00+00:00')")
    assert store.conn.in_transaction  # as if a multi-statement write failed half way
    with pytest.raises(sqlite3.OperationalError):
        store.conn.execute("BEGIN IMMEDIATE")  # what used to break every later lease attempt
    with caplog.at_level(logging.WARNING):
        assert lease("a", clock).acquire(store, clock())
    assert "left open by an earlier failure" in caplog.text
