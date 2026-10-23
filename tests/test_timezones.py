import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import TZ, dt

from maestro.models import Item, Kind
from maestro.notifier import MultiNotifier
from maestro.parser import parse
from maestro.scheduler import fire_due
from maestro.store import MIGRATIONS, Store
from maestro.tz import get_timezone

KOLKATA = ZoneInfo("Asia/Kolkata")


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item.id)


def test_stored_as_utc_and_read_back_in_user_timezone():
    store = Store()
    item = store.add(parse("call mom tomorrow at 5pm", now=dt(2026, 10, 23, 18, 0)))
    (raw,) = store.conn.execute("SELECT due_at FROM items WHERE id = ?", (item.id,)).fetchone()
    assert raw == "2026-10-24T21:00:00+00:00"  # 17:00 EDT

    loaded = store.get(item.id)
    assert loaded.due_at.tzinfo == TZ
    assert (loaded.due_at.hour, loaded.due_at.minute) == (17, 0)


def test_server_in_utc_fires_at_users_local_time():
    # e.g. Docker: the process clock is UTC but the user lives in India.
    store = Store(tz=KOLKATA)
    item = store.add(parse("call mom at 5pm", now=datetime(2026, 10, 24, 9, 0, tzinfo=KOLKATA), tz=KOLKATA))
    notifier = MultiNotifier({"push": Recorder()})

    assert fire_due(store, notifier, now=datetime(2026, 10, 24, 11, 29, tzinfo=UTC)) == []
    fired = fire_due(store, notifier, now=datetime(2026, 10, 24, 11, 30, tzinfo=UTC))
    assert [i.id for i in fired] == [item.id]  # 17:00 IST == 11:30 UTC


def test_daily_reminder_keeps_wall_clock_time_across_fall_back():
    store = Store()
    item = store.add(parse("take vitamins every day at 8am", now=dt(2026, 10, 31, 7, 0)))
    fire_due(store, MultiNotifier({"push": Recorder()}), now=dt(2026, 10, 31, 8, 0))

    next_due = store.get(item.id).due_at
    assert (next_due.date().isoformat(), next_due.hour) == ("2026-11-01", 8)
    assert next_due.astimezone(UTC).hour == 13  # EST is UTC-5; the day before it was 12:00 UTC (EDT)


def test_daily_reminder_keeps_wall_clock_time_across_spring_forward():
    store = Store()
    item = store.add(parse("take vitamins every day at 8am", now=dt(2027, 3, 13, 7, 0)))
    fire_due(store, MultiNotifier({"push": Recorder()}), now=dt(2027, 3, 13, 8, 0))
    next_due = store.get(item.id).due_at
    assert (next_due.date().isoformat(), next_due.hour) == ("2027-03-14", 8)


def test_relative_and_interval_times_use_real_elapsed_time_across_dst():
    before_fall_back = dt(2026, 11, 1, 0, 30)  # EDT; clocks go 2:00 -> 1:00 at 2am

    in_two_hours = parse("check the oven in 2 hours", now=before_fall_back)
    assert in_two_hours.due_at.astimezone(UTC) - before_fall_back.astimezone(UTC) == timedelta(hours=2)
    assert in_two_hours.due_at.hour == 1  # 01:30 EST, the second 1:30 that night

    store = Store()
    item = store.add(parse("drink water every 2 hours", now=before_fall_back))
    first = item.due_at
    fire_due(store, MultiNotifier({"push": Recorder()}), now=first)
    second = store.get(item.id).due_at
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=2)


def test_same_instant_shown_in_another_timezone(tmp_path):
    path = tmp_path / "m.db"
    Store(path, tz=TZ).add(Item(text="Standup", kind=Kind.REMINDER, created_at=dt(2026, 10, 23, 8, 0),
                                due_at=dt(2026, 10, 26, 9, 30)))
    shown = Store(path, tz=KOLKATA).list()[0].due_at
    assert shown == dt(2026, 10, 26, 9, 30)  # same instant
    assert (shown.hour, shown.minute) == (19, 0)  # 09:30 EDT == 19:00 IST


def test_migration_4_converts_naive_local_rows(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    for statements in MIGRATIONS[:3]:
        for sql in statements:
            conn.execute(sql)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO items (text, kind, status, due_at, created_at, recurrence)"
        " VALUES ('Pay rent', 'reminder', 'pending', '2026-11-01T09:00:00',"
        " '2026-10-23T18:00:00', 'monthly:1')"
    )
    conn.execute(
        "INSERT INTO deliveries (item_id, occurrence, channel, attempts, next_attempt_at)"
        " VALUES (1, '2026-11-01T09:00:00', 'sms', 1, '2026-11-01T09:01:00')"
    )
    conn.commit()
    conn.close()

    store = Store(path)  # MAESTRO_TIMEZONE is America/New_York in tests
    assert store.schema_version == len(MIGRATIONS)
    item = store.get(1)
    assert item.due_at == dt(2026, 11, 1, 9, 0)  # Nov 1 is after fall-back: EST
    (raw,) = store.conn.execute("SELECT due_at FROM items").fetchone()
    assert raw == "2026-11-01T14:00:00+00:00"
    assert store.deliveries(1, item.due_at)["sms"].next_attempt_at == dt(2026, 11, 1, 9, 1)


def test_timezone_resolution(monkeypatch):
    assert get_timezone("Europe/London").key == "Europe/London"
    assert get_timezone().key == TZ.key  # from MAESTRO_TIMEZONE
    monkeypatch.delenv("MAESTRO_TIMEZONE")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert get_timezone().key == "Asia/Tokyo"
    with pytest.raises(ValueError, match="Unknown timezone"):
        get_timezone("Mars/Olympus_Mons")
