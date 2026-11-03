"""#60: SIGTERM finishes the work in flight, releases the lease and exits; nothing is cut in half."""

import os
import signal
import threading
import time
from datetime import UTC, datetime

import pytest
from conftest import dt

from maestro import cli
from maestro.models import Item, Kind, Status
from maestro.notifier import MultiNotifier
from maestro.scheduler import Lease, fire_due, run_forever
from maestro.shutdown import Shutdown
from maestro.store import Store

NOW = dt(2026, 10, 23, 9, 0)


@pytest.fixture(autouse=True)
def _restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


class Recorder:
    """A channel that can ask for a stop in the middle of a send, like SIGTERM would."""

    def __init__(self, shutdown=None, stop_on=None):
        self.sent = []
        self.shutdown = shutdown
        self.stop_on = stop_on

    def send(self, item):
        if self.shutdown and item.id == self.stop_on:
            assert not self.shutdown.request()  # mid-send counts as busy: no interruption
        self.sent.append(item.id)


def add_reminders(store, count):
    for n in range(1, count + 1):
        store.add(Item(text=f"Reminder {n}", kind=Kind.REMINDER, created_at=NOW, due_at=NOW))


def test_request_interrupts_only_when_nothing_is_in_flight():
    shutdown = Shutdown()
    assert not shutdown.requested
    with shutdown.busy():
        assert shutdown.request() is False
        assert shutdown.requested
    assert shutdown.request() is True  # idle now: the wait may be cut short


def test_a_stop_mid_tick_finishes_the_reminder_in_flight_and_leaves_the_rest_due():
    shutdown = Shutdown()
    with Store() as store:
        add_reminders(store, 3)
        channel = Recorder(shutdown, stop_on=1)
        with shutdown.busy():
            finished = fire_due(store, MultiNotifier({"c": channel}), now=NOW, shutdown=shutdown)
        assert channel.sent == [1]
        assert [i.id for i in finished] == [1]
        assert store.get(1).status == Status.FIRED  # delivered *and* recorded
        assert [i.id for i in store.due(NOW)] == [2, 3]  # untouched, for the next scheduler


def test_run_forever_stops_after_the_tick_without_waiting_and_releases_the_lease():
    shutdown = Shutdown()
    with Store() as store:
        add_reminders(store, 2)
        channel = Recorder(shutdown, stop_on=1)
        lease = Lease(owner="a")

        def never(seconds):
            pytest.fail("the loop should exit instead of waiting")

        run_forever(store, MultiNotifier({"c": channel}), interval=30, wait=never, lease=lease,
                    shutdown=shutdown)
        assert channel.sent == [1]
        assert store.lease_holder(datetime.now(UTC)) is None


def test_sigterm_during_a_send_lets_it_finish(tmp_path, monkeypatch):
    """The whole path `docker stop` takes through `maestro run`, in-process so it is deterministic."""
    db = str(tmp_path / "m.db")
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)
    with Store(db) as store:
        add_reminders(store, 2)

    class Slow:
        sent = []

        def send(self, item):
            os.kill(os.getpid(), signal.SIGTERM)  # arrives while this send is in flight
            threading.Event().wait(0.05)  # the handler has run by now; we are still here
            self.sent.append(item.id)

    slow = Slow()
    monkeypatch.setattr(cli, "build_notifier", lambda config: MultiNotifier({"slow": slow}))
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("stopped before the wait: no sleep"))
    assert cli.main(["--db", db, "run", "--interval", "30"]) == 0
    assert slow.sent == [1]
    with Store(db) as store:
        assert store.get(1).status == Status.FIRED
        assert store.get(2).status == Status.PENDING
        assert store.lease_holder(datetime.now(UTC)) is None


def test_sigterm_while_idle_exits_at_once(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)

    def asleep(seconds):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        pytest.fail("an idle wait should have been interrupted")

    monkeypatch.setattr(cli.time, "sleep", asleep)
    with pytest.raises(SystemExit) as exit:
        cli.main(["--db", db, "run", "--interval", "30"])
    assert exit.value.code == 0
    with Store(db) as store:
        assert store.lease_holder(datetime.now(UTC)) is None


def test_the_wait_skips_sleeping_once_a_stop_was_asked_for(monkeypatch):
    shutdown = Shutdown()

    class Email:
        def process(self):
            assert shutdown.request() is False  # capturing fetched mail is finished first
            return []

    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("should not sleep while stopping"))
    cli._wait_with(None, Email(), shutdown)(30)
    assert shutdown.requested


def test_the_bot_finishes_the_batch_it_fetched():
    shutdown = Shutdown()
    handled = []

    class Bot:
        def fetch(self, timeout):
            return ["first", "second"]

        def handle(self, update):
            if update == "first":
                shutdown.request()
            handled.append(update)

    cli._bot_wait(Bot(), shutdown)(30)
    assert handled == ["first", "second"]  # Telegram considers both confirmed; neither is lost


def test_sigterm_handler_chains_to_the_one_uvicorn_installed():
    received = []
    signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
    shutdown = Shutdown()
    assert callable(shutdown.install(chain=True))
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.05)
    assert shutdown.requested and received == [signal.SIGTERM]


def test_installing_off_the_main_thread_is_a_no_op():
    results = []
    thread = threading.Thread(target=lambda: results.append(Shutdown().install()))
    thread.start()
    thread.join()
    assert results == [None]


def test_serve_shutdown_releases_the_lease_without_waiting_for_telegram():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    release = threading.Event()

    class Bot:
        def fetch(self):
            release.wait(30)  # a long poll with nothing to say
            return []

        def handle(self, update):
            pass

    with Store() as store:
        started = time.monotonic()
        with TestClient(create_app(store, notifier=MultiNotifier({}), interval=30, bot=Bot())) as client:
            assert client.get("/health").status_code == 200
            assert store.lease_holder(datetime.now(UTC)) is not None
        assert time.monotonic() - started < 10
        assert store.lease_holder(datetime.now(UTC)) is None  # released before the loop closed
    release.set()
