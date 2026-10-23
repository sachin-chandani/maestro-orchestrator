"""SQLite persistence for captured items.

Datetimes are stored as naive local-time ISO strings (timezone support is on the roadmap).
Schema changes go through MIGRATIONS; the applied version is tracked in PRAGMA user_version.
"""

# Store.list shadows the builtin in the class body; deferred annotations keep `list[Item]` valid on <3.14.
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from maestro.models import Delivery, Item, Kind, Status

MIGRATIONS: list[list[str]] = [
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
]


def _to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        text=row["text"],
        kind=Kind(row["kind"]),
        status=Status(row["status"]),
        due_at=_from_iso(row["due_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        source_text=row["source_text"],
        recurrence=row["recurrence"],
    )


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The API server touches the connection from its event-loop thread, not the creating thread.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        # BEGIN IMMEDIATE takes the write lock before reading the version, so two processes opening
        # a fresh database at once (e.g. `maestro serve` and `maestro add`) can't both migrate.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
                for sql in statements:
                    self.conn.execute(sql)
                self.conn.execute(f"PRAGMA user_version = {number}")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def add(self, item: Item) -> Item:
        cur = self.conn.execute(
            "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item.text, item.kind, item.status, _to_iso(item.due_at),
             _to_iso(item.created_at), item.source_text, item.recurrence),
        )
        self.conn.commit()
        item.id = cur.lastrowid
        return item

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _from_row(row) if row else None

    def list(self, include_done: bool = False) -> list[Item]:
        sql = "SELECT * FROM items"
        if not include_done:
            sql += f" WHERE status != '{Status.DONE}'"
        # Reminders by due time first, then notes by creation time.
        sql += " ORDER BY due_at IS NULL, due_at, created_at"
        return [_from_row(r) for r in self.conn.execute(sql)]

    def search(self, query: str) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE text LIKE ? ORDER BY created_at DESC",
            (f"%{query}%",),
        )
        return [_from_row(r) for r in rows]

    def due(self, now: datetime) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE status = ? AND due_at IS NOT NULL AND due_at <= ?"
            " ORDER BY due_at",
            (Status.PENDING, _to_iso(now)),
        )
        return [_from_row(r) for r in rows]

    def set_status(self, item_id: int, status: Status) -> bool:
        cur = self.conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
        self.conn.commit()
        return cur.rowcount > 0

    def reschedule(self, item_id: int, due_at: datetime) -> bool:
        """Move an item to a new due time and make it pending again (snooze, next recurrence)."""
        cur = self.conn.execute(
            "UPDATE items SET due_at = ?, status = ?, kind = ? WHERE id = ?",
            (_to_iso(due_at), Status.PENDING, Kind.REMINDER, item_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, item_id: int) -> bool:
        self.conn.execute("DELETE FROM deliveries WHERE item_id = ?", (item_id,))
        cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def deliveries(self, item_id: int, occurrence: datetime) -> dict[str, Delivery]:
        rows = self.conn.execute(
            "SELECT * FROM deliveries WHERE item_id = ? AND occurrence = ?", (item_id, _to_iso(occurrence))
        )
        return {
            r["channel"]: Delivery(
                channel=r["channel"],
                attempts=r["attempts"],
                delivered_at=_from_iso(r["delivered_at"]),
                next_attempt_at=_from_iso(r["next_attempt_at"]),
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
            (item_id, _to_iso(occurrence), delivery.channel, delivery.attempts,
             _to_iso(delivery.delivered_at), _to_iso(delivery.next_attempt_at), delivery.last_error),
        )
        self.conn.commit()
