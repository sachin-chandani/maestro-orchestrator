"""SQLite persistence for captured items.

Timestamps are stored as UTC ISO-8601 strings (`2026-10-24T21:00:00+00:00`), so string order is
time order. They come back out as aware datetimes in the store's timezone. Naive datetimes passed
in are taken to be wall-clock time in that timezone.

Schema changes go through MIGRATIONS; the applied version is tracked in PRAGMA user_version.
A migration is either a list of SQL statements or a function `(conn, tz) -> None` for data changes.
"""

# Store.list shadows the builtin inside the class body, so annotations there say builtins.list.
from __future__ import annotations

import builtins
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.models import Delivery, Item, Kind, Status
from maestro.tz import UTC, get_timezone, localize

# Wait this long for another process's lock instead of failing with "database is locked".
BUSY_TIMEOUT_SECONDS = 5.0


def _utc_iso(dt: datetime | None, tz: ZoneInfo) -> str | None:
    return localize(dt, tz).astimezone(UTC).isoformat(timespec="seconds") if dt else None


def _naive_local_to_utc(conn: sqlite3.Connection, tz: ZoneInfo) -> None:
    """Migration 4: rows written before timezone support hold naive local time; convert them to UTC."""
    columns = {
        "items": ["due_at", "created_at"],
        "deliveries": ["occurrence", "delivered_at", "next_attempt_at"],
    }
    for table, cols in columns.items():
        for col in cols:
            rows = conn.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
            for (value,) in rows:
                converted = _utc_iso(datetime.fromisoformat(value), tz)
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (converted, value))


MIGRATIONS: list[list[str] | Callable[[sqlite3.Connection, ZoneInfo], None]] = [
    # 1: initial schema
    [
        """CREATE TABLE IF NOT EXISTS items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            status      TEXT NOT NULL,
            due_at      TEXT,
            created_at  TEXT NOT NULL,
            source_text TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_items_due ON items (status, due_at)",
    ],
    # 2: recurring reminders
    ["ALTER TABLE items ADD COLUMN recurrence TEXT"],
    # 3: per-channel delivery tracking, keyed by occurrence so each repeat of a recurring item starts fresh
    [
        """CREATE TABLE IF NOT EXISTS deliveries (
            item_id         INTEGER NOT NULL,
            occurrence      TEXT NOT NULL,
            channel         TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            delivered_at    TEXT,
            next_attempt_at TEXT,
            last_error      TEXT,
            PRIMARY KEY (item_id, occurrence, channel)
        )""",
    ],
    # 4: store UTC instead of naive local time
    _naive_local_to_utc,
]


class Store:
    def __init__(self, path: str | Path = ":memory:", tz: ZoneInfo | None = None):
        self.tz = tz or get_timezone()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The API server touches the connection from its event-loop thread, not the creating thread.
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            # WAL: readers (API, web UI) never block the writer (scheduler) and vice versa.
            # synchronous=NORMAL is crash-safe with WAL and much faster than FULL.
            # See docs/adr/0006-sqlite-wal.md for the trade-offs (no network filesystems).
            self._enable_wal()
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.closed = False
        try:
            self._migrate()
        except BaseException:
            self.close()  # the caller never gets the object, so it can't close it
            raise

    def _enable_wal(self) -> None:
        # Switching a fresh file to WAL needs exclusive access, and SQLite fails that pragma at once
        # instead of waiting like it does for normal locks. Retry so processes starting together
        # (serve + add on a new database) don't crash. Once set, WAL persists and this is a no-op.
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while True:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    def close(self) -> None:
        self.conn.close()
        self.closed = True

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        # BEGIN IMMEDIATE takes the write lock before reading the version, so two processes opening
        # a fresh database at once (e.g. `maestro serve` and `maestro add`) can't both migrate.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                if callable(migration):
                    migration(self.conn, self.tz)
                else:
                    for sql in migration:
                        self.conn.execute(sql)
                self.conn.execute(f"PRAGMA user_version = {number}")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _to_db(self, dt: datetime | None) -> str | None:
        return _utc_iso(dt, self.tz)

    def _required_from_db(self, value: str) -> datetime:
        return datetime.fromisoformat(value).astimezone(self.tz)

    def _from_db(self, value: str | None) -> datetime | None:
        return self._required_from_db(value) if value else None

    def _item(self, row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            text=row["text"],
            kind=Kind(row["kind"]),
            status=Status(row["status"]),
            due_at=self._from_db(row["due_at"]),
            created_at=self._required_from_db(row["created_at"]),
            source_text=row["source_text"],
            recurrence=row["recurrence"],
        )

    @property
    def journal_mode(self) -> str:
        return str(self.conn.execute("PRAGMA journal_mode").fetchone()[0])

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def add(self, item: Item) -> Item:
        cur = self.conn.execute(
            "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item.text, item.kind, item.status, self._to_db(item.due_at),
             self._to_db(item.created_at), item.source_text, item.recurrence),
        )
        self.conn.commit()
        item.id = cur.lastrowid
        return item

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def list(self, include_done: bool = False) -> builtins.list[Item]:
        sql = "SELECT * FROM items"
        if not include_done:
            sql += f" WHERE status != '{Status.DONE}'"
        # Reminders by due time first, then notes by creation time.
        sql += " ORDER BY due_at IS NULL, due_at, created_at"
        return [self._item(r) for r in self.conn.execute(sql)]

    def counts(self) -> dict[tuple[str, str], int]:
        """Number of items per (kind, status)."""
        rows = self.conn.execute("SELECT kind, status, COUNT(*) FROM items GROUP BY kind, status")
        return {(kind, status): n for kind, status, n in rows}

    def search(self, query: str) -> builtins.list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE text LIKE ? ORDER BY created_at DESC",
            (f"%{query}%",),
        )
        return [self._item(r) for r in rows]

    def due(self, now: datetime) -> builtins.list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE status = ? AND due_at IS NOT NULL AND due_at <= ?"
            " ORDER BY due_at",
            (Status.PENDING, self._to_db(now)),
        )
        return [self._item(r) for r in rows]

    def set_status(self, item_id: int, status: Status) -> bool:
        cur = self.conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
        self.conn.commit()
        return cur.rowcount > 0

    def reschedule(self, item_id: int, due_at: datetime) -> bool:
        """Move an item to a new due time and make it pending again (snooze, next recurrence)."""
        cur = self.conn.execute(
            "UPDATE items SET due_at = ?, status = ?, kind = ? WHERE id = ?",
            (self._to_db(due_at), Status.PENDING, Kind.REMINDER, item_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, item_id: int) -> bool:
        self.conn.execute("DELETE FROM deliveries WHERE item_id = ?", (item_id,))
        cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def last_delivered(self, channel: str) -> int | None:
        """The item most recently delivered on `channel` that still exists (for "reply done")."""
        row = self.conn.execute(
            "SELECT d.item_id FROM deliveries d JOIN items i ON i.id = d.item_id"
            " WHERE d.channel = ? AND d.delivered_at IS NOT NULL ORDER BY d.delivered_at DESC LIMIT 1",
            (channel,),
        ).fetchone()
        return row[0] if row else None

    def deliveries(self, item_id: int, occurrence: datetime) -> dict[str, Delivery]:
        rows = self.conn.execute(
            "SELECT * FROM deliveries WHERE item_id = ? AND occurrence = ?",
            (item_id, self._to_db(occurrence)),
        )
        return {
            r["channel"]: Delivery(
                channel=r["channel"],
                attempts=r["attempts"],
                delivered_at=self._from_db(r["delivered_at"]),
                next_attempt_at=self._from_db(r["next_attempt_at"]),
                last_error=r["last_error"],
            )
            for r in rows
        }

    def record_delivery(self, item_id: int, occurrence: datetime, delivery: Delivery) -> None:
        self.conn.execute(
            "INSERT INTO deliveries (item_id, occurrence, channel, attempts, delivered_at, next_attempt_at,"
            " last_error) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (item_id, occurrence, channel) DO UPDATE SET attempts = excluded.attempts,"
            " delivered_at = excluded.delivered_at, next_attempt_at = excluded.next_attempt_at,"
            " last_error = excluded.last_error",
            (item_id, self._to_db(occurrence), delivery.channel, delivery.attempts,
             self._to_db(delivery.delivered_at), self._to_db(delivery.next_attempt_at), delivery.last_error),
        )
        self.conn.commit()
