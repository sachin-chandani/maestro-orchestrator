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
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.models import Delivery, Item, Kind, Status
from maestro.tz import UTC, get_timezone, localize

log = logging.getLogger(__name__)


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer Maestro than this one."""


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
    # 5: one row naming the scheduler allowed to fire reminders, so several processes never double-send
    [
        """CREATE TABLE IF NOT EXISTS scheduler_lease (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            owner      TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )""",
    ],
    # 6: inbound messages already captured (Twilio MessageSid/CallSid...), so provider retries don't
    # create duplicates
    [
        """CREATE TABLE IF NOT EXISTS inbound_messages (
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            item_id     INTEGER NOT NULL,
            PRIMARY KEY (source, external_id)
        )""",
    ],
]


class Store:
    def __init__(self, path: str | Path = ":memory:", tz: ZoneInfo | None = None):
        self.tz = tz or get_timezone()
        self.path = str(path)
        on_disk = str(path) not in ("", ":memory:")  # "" is a private temporary database
        if on_disk:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The API server touches the connection from its event-loop thread, not the creating thread.
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS)
        self.conn.row_factory = sqlite3.Row
        self.closed = False
        try:
            if on_disk:
                self._configure_durability()
            self._migrate()
        except BaseException:
            self.close()  # the caller never gets the object, so it can't close it
            raise

    def _configure_durability(self) -> None:
        """WAL so other processes' reads and writes don't block each other; see ADR 0006."""
        if self._enable_wal() == "wal":
            # Crash-safe with WAL and much faster than FULL. Without WAL it is not, so keep the default.
            self.conn.execute("PRAGMA synchronous=NORMAL")
        else:
            log.warning("Could not enable WAL for %s (journal mode %r); is it on a network filesystem? "
                        "Keeping synchronous=FULL.", self.path, self.journal_mode)

    def _enable_wal(self) -> str:
        """Switch to WAL and return the resulting journal mode. SQLite doesn't raise when it can't
        switch, it reports the mode it is still in, so callers must check the result.

        Switching a fresh file needs exclusive access, and that pragma fails at once instead of
        waiting like normal locks. Retry with short busy waits so processes starting together (serve
        + add on a new database) don't crash, staying within BUSY_TIMEOUT_SECONDS overall.
        """
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        self.conn.execute("PRAGMA busy_timeout = 50")
        try:
            while True:
                try:
                    return str(self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        finally:
            self.conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")

    def close(self) -> None:
        self.conn.close()
        self.closed = True

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[None]:
        """Run writes atomically: commit on success, roll back on error.

        The connection is shared (ADR 0003), so a transaction left open by an earlier failure would
        otherwise break every later write; it is rolled back first. `immediate` takes the write lock
        up front, for read-then-write sequences that must not interleave with other processes.
        """
        if self.conn.in_transaction:
            log.warning("Rolling back a transaction left open by an earlier failure")
            self.conn.rollback()
        if immediate:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _migrate(self) -> None:
        # BEGIN IMMEDIATE takes the write lock before reading the version, so two processes opening
        # a fresh database at once (e.g. `maestro serve` and `maestro add`) can't both migrate.
        with self._transaction(immediate=True):
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                # An older Maestro must not run against a newer schema: it would ignore features such as
                # the scheduler lease and, for example, deliver reminders twice.
                raise SchemaTooNewError(
                    f"Database schema {version} is newer than this Maestro supports ({len(MIGRATIONS)}); "
                    "upgrade Maestro before using this database"
                )
            for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                if callable(migration):
                    migration(self.conn, self.tz)
                else:
                    for sql in migration:
                        self.conn.execute(sql)
                self.conn.execute(f"PRAGMA user_version = {number}")

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
        with self._transaction():
            cur = self.conn.execute(
                "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.text, item.kind, item.status, self._to_db(item.due_at),
                 self._to_db(item.created_at), item.source_text, item.recurrence),
            )
        item.id = cur.lastrowid
        return item

    def add_once(self, source: str, external_id: str, item: Item) -> tuple[Item, bool]:
        """Add an item for an inbound message unless that message was already captured.

        Returns (item, created). A retried webhook gets back the item from the first delivery. The
        check, the insert and the record happen in one transaction.
        """
        with self._transaction(immediate=True):
            row = self.conn.execute(
                "SELECT item_id FROM inbound_messages WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row is not None:
                existing = self.conn.execute("SELECT * FROM items WHERE id = ?", (row["item_id"],)).fetchone()
                if existing is not None:
                    return self._item(existing), False
            cur = self.conn.execute(
                "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.text, item.kind, item.status, self._to_db(item.due_at),
                 self._to_db(item.created_at), item.source_text, item.recurrence),
            )
            item.id = cur.lastrowid
            self.conn.execute(
                "INSERT OR REPLACE INTO inbound_messages (source, external_id, item_id) VALUES (?, ?, ?)",
                (source, external_id, item.id),
            )
        return item, True

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
        with self._transaction():
            cur = self.conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
        return cur.rowcount > 0

    def reschedule(self, item_id: int, due_at: datetime) -> bool:
        """Move an item to a new due time and make it pending again (snooze, next recurrence)."""
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE items SET due_at = ?, status = ?, kind = ? WHERE id = ?",
                (self._to_db(due_at), Status.PENDING, Kind.REMINDER, item_id),
            )
        return cur.rowcount > 0

    def delete(self, item_id: int) -> bool:
        with self._transaction():
            self.conn.execute("DELETE FROM deliveries WHERE item_id = ?", (item_id,))
            self.conn.execute("DELETE FROM inbound_messages WHERE item_id = ?", (item_id,))
            cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return cur.rowcount > 0

    def last_delivered(self, channel: str) -> int | None:
        """The item most recently delivered on `channel` that still exists (for "reply done")."""
        row = self.conn.execute(
            "SELECT d.item_id FROM deliveries d JOIN items i ON i.id = d.item_id"
            " WHERE d.channel = ? AND d.delivered_at IS NOT NULL ORDER BY d.delivered_at DESC LIMIT 1",
            (channel,),
        ).fetchone()
        return row[0] if row else None

    def lease_holder(self, now: datetime) -> str | None:
        """Who holds an unexpired scheduler lease, without taking the write lock."""
        row = self.conn.execute(
            "SELECT owner FROM scheduler_lease WHERE id = 1 AND expires_at > ?", (self._to_db(now),)
        ).fetchone()
        return str(row["owner"]) if row else None

    def acquire_lease(self, owner: str, now: datetime, expires_at: datetime) -> tuple[bool, str]:
        """Take or renew the scheduler lease if it is free, expired or already ours.

        Returns (acquired, current holder). The check-and-set runs under BEGIN IMMEDIATE, so two
        processes can't both see the lease as free. Standbys check `lease_holder` first, cheaply.
        """
        with self._transaction(immediate=True):
            row = self.conn.execute("SELECT owner, expires_at FROM scheduler_lease WHERE id = 1").fetchone()
            if row is not None and row["owner"] != owner and row["expires_at"] > self._to_db(now):
                return False, str(row["owner"])
            self.conn.execute(
                "INSERT INTO scheduler_lease (id, owner, expires_at) VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at",
                (owner, self._to_db(expires_at)),
            )
        return True, owner

    def release_lease(self, owner: str) -> None:
        """Give up the lease (on shutdown) so a standby scheduler can take over at once."""
        with self._transaction():
            self.conn.execute("DELETE FROM scheduler_lease WHERE id = 1 AND owner = ?", (owner,))

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
        with self._transaction():
            self._upsert_delivery(item_id, occurrence, delivery)

    def _upsert_delivery(self, item_id: int, occurrence: datetime, delivery: Delivery) -> None:
        self.conn.execute(
            "INSERT INTO deliveries (item_id, occurrence, channel, attempts, delivered_at, next_attempt_at,"
            " last_error) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (item_id, occurrence, channel) DO UPDATE SET attempts = excluded.attempts,"
            " delivered_at = excluded.delivered_at, next_attempt_at = excluded.next_attempt_at,"
            " last_error = excluded.last_error",
            (item_id, self._to_db(occurrence), delivery.channel, delivery.attempts,
             self._to_db(delivery.delivered_at), self._to_db(delivery.next_attempt_at), delivery.last_error),
        )
