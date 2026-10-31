import base64
import sqlite3
import urllib.parse

import pytest
from conftest import dt

from maestro.config import Config
from maestro.models import Item, Kind, Status
from maestro.notifier import ConfigError, NtfyNotifier, TwilioSMSNotifier, build_notifier
from maestro.scheduler import fire_due
from maestro.store import MIGRATIONS, Store

ITEM = Item(id=7, text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 23))


class FakeOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return 200


def test_ntfy_posts_text_to_topic():
    opener = FakeOpener()
    NtfyNotifier("https://ntfy.example.com", "my reminders", opener=opener).send(ITEM)
    (req,) = opener.requests
    assert req.full_url == "https://ntfy.example.com/my%20reminders"
    assert req.data == b"Call mom"
    assert req.get_method() == "POST"


def test_twilio_sends_sms_with_basic_auth():
    opener = FakeOpener()
    TwilioSMSNotifier("AC123", "secret", "+15550001111", "+15552223333", opener=opener).send(ITEM)
    (req,) = opener.requests
    assert req.full_url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"AC123:secret").decode()
    form = urllib.parse.parse_qs(req.data.decode())
    assert form == {"From": ["+15550001111"], "To": ["+15552223333"],
                    "Body": ["Reminder: Call mom (reply done or snooze 1h)"]}


def test_config_from_env():
    config = Config.from_env({
        "MAESTRO_CHANNELS": "console, ntfy",
        "NTFY_TOPIC": "abc",
        "NTFY_URL": "https://push.example.com/",
    })
    assert config.channels == ["console", "ntfy"]
    assert config.ntfy_url == "https://push.example.com"
    assert list(build_notifier(config).channels) == ["console", "ntfy"]


def test_missing_twilio_keys_are_named():
    config = Config.from_env({"MAESTRO_CHANNELS": "twilio_sms", "TWILIO_ACCOUNT_SID": "AC1"})
    with pytest.raises(ConfigError, match="TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, MAESTRO_SMS_TO"):
        build_notifier(config)


def test_unknown_channel():
    with pytest.raises(ConfigError, match="Unknown channel"):
        build_notifier(Config(channels=["pager"]))


def test_failed_delivery_stays_pending_for_retry():
    store = Store()
    item = store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 23),
                          due_at=dt(2026, 10, 23, 9, 0)))

    class Broken:
        def send(self, _):
            raise ConnectionError("network down")

    assert fire_due(store, Broken(), now=dt(2026, 10, 23, 10, 0)) == []
    assert store.get(item.id).status == Status.PENDING


def test_migrates_v1_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, kind TEXT NOT NULL,"
        " status TEXT NOT NULL, due_at TEXT, created_at TEXT NOT NULL, source_text TEXT NOT NULL DEFAULT '');"
        "INSERT INTO items (text, kind, status, created_at)"
        " VALUES ('old note', 'note', 'pending', '2026-01-01T00:00:00');"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.schema_version == len(MIGRATIONS)
    assert store.get(1).text == "old note"
    assert store.get(1).recurrence is None


def test_failed_migration_closes_the_connection(tmp_path, monkeypatch):
    import maestro.storage.sqlite as store_module

    closed = []
    real_close = store_module.SQLiteStore.close

    def recording_close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(store_module.SQLiteStore, "close", recording_close)

    def broken(conn, tz):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store_module, "MIGRATIONS", [*MIGRATIONS, broken])
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "m.db")
    assert closed == [True]


def test_concurrent_opens_of_fresh_database_migrate_once(tmp_path):
    # Regression: `maestro serve` and `maestro add` racing on a new DB both ran migration 2.
    from concurrent.futures import ThreadPoolExecutor

    for attempt in range(10):
        path = tmp_path / f"race{attempt}.db"
        with ThreadPoolExecutor(8) as pool:
            versions = list(pool.map(lambda p: Store(p).schema_version, [path] * 8))
        assert versions == [len(MIGRATIONS)] * 8


def test_file_databases_use_wal_and_readers_are_not_blocked_by_a_writer(tmp_path):
    path = tmp_path / "m.db"
    with Store(path) as store:
        assert store.journal_mode == "wal"
        store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 23)))

        # Another process holds the write lock in the middle of a transaction...
        writer = sqlite3.connect(path, timeout=0)
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO items (text, kind, status, created_at)"
                       " VALUES ('x', 'note', 'pending', '2026-10-23T00:00:00+00:00')")
        try:
            # ...and reads still answer immediately from the last committed snapshot.
            assert [i.text for i in store.list()] == ["Call mom"]
        finally:
            writer.rollback()
            writer.close()


def test_in_memory_databases_are_left_alone():
    with Store() as store:
        assert store.journal_mode == "memory"


def test_without_wal_synchronous_stays_full_and_a_warning_is_logged(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(Store, "_enable_wal", lambda self: "delete")  # e.g. a network filesystem
    with caplog.at_level(logging.WARNING), Store(tmp_path / "m.db") as store:
        assert store.conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL, not NORMAL (1)
    assert "Could not enable WAL" in caplog.text


def test_failed_wal_switch_closes_the_connection(tmp_path, monkeypatch):
    def readonly(self):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    closed = []
    real_close = Store.close
    monkeypatch.setattr(Store, "_enable_wal", readonly)
    monkeypatch.setattr(Store, "close", lambda self: (closed.append(True), real_close(self)))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        Store(tmp_path / "m.db")
    assert closed == [True]


def test_wal_switch_gives_up_within_the_busy_timeout(tmp_path, monkeypatch):
    import time

    import maestro.storage.sqlite as store_module

    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_SECONDS", 0.3)
    path = tmp_path / "m.db"
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")  # a fresh, non-WAL file locked by someone else
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Store(path)
    finally:
        holder.rollback()
        holder.close()
    assert time.monotonic() - started < 1.0


def test_empty_path_is_a_temporary_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with Store("") as store:
        store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=dt(2026, 10, 23)))
        assert store.journal_mode != "wal"
    assert list(tmp_path.iterdir()) == []
