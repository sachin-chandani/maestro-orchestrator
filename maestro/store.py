"""SQLite persistence for captured items.

Datetimes are stored as naive local-time ISO strings (timezone support is on the roadmap).
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from maestro.models import Item, Kind, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    due_at      TEXT,
    created_at  TEXT NOT NULL,
    source_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_items_due ON items (status, due_at);
"""


def _to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        text=row["text"],
        kind=Kind(row["kind"]),
        status=Status(row["status"]),
        due_at=datetime.fromisoformat(row["due_at"]) if row["due_at"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        source_text=row["source_text"],
    )


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add(self, item: Item) -> Item:
        cur = self.conn.execute(
            "INSERT INTO items (text, kind, status, due_at, created_at, source_text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (item.text, item.kind, item.status, _to_iso(item.due_at),
             _to_iso(item.created_at), item.source_text),
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

    def delete(self, item_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0
