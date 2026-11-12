"""SQLite persistence for captured items: the default backend behind `maestro.storage.Store`.

Timestamps are stored as UTC ISO-8601 strings (`2026-10-24T21:00:00+00:00`), so string order is
time order. They come back out as aware datetimes in the store's timezone. Naive datetimes passed
in are taken to be wall-clock time in that timezone.

Schema changes go through MIGRATIONS; the applied version is tracked in PRAGMA user_version.
A migration is either a list of SQL statements or a function `(conn, tz) -> None` for data changes.

What the methods promise is documented once, on the interface in `maestro.storage.base`; what is
written here is how SQLite keeps those promises.
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

from maestro.models import Acknowledgement, Capture, CaptureStatus, Delivery, Device, Item, Kind, Status
from maestro.observability import METRICS
from maestro.storage import logic, migrations
from maestro.storage.base import Store
from maestro.tz import UTC, get_timezone, localize

log = logging.getLogger(__name__)

__all__ = ["MIGRATIONS", "Migration", "SQLiteStore"]

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


# A migration is either a list of SQL statements or a function for data changes.
Migration = list[str] | Callable[[sqlite3.Connection, ZoneInfo], None]

MIGRATIONS: list[Migration] = [
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
    # 7: acknowledgements of delivered reminders (done, snooze, reply), per occurrence; escalation
    # (#12) acts on deliveries that stay unacknowledged
    [
        """CREATE TABLE IF NOT EXISTS acknowledgements (
            item_id         INTEGER NOT NULL,
            occurrence      TEXT NOT NULL,
            acknowledged_at TEXT NOT NULL,
            via             TEXT NOT NULL,
            PRIMARY KEY (item_id, occurrence)
        )""",
        # Everything delivered before this version counts as handled, so escalation doesn't fire on
        # the whole history at once after an upgrade.
        """INSERT OR IGNORE INTO acknowledgements (item_id, occurrence, acknowledged_at, via)
           SELECT item_id, occurrence, MAX(delivered_at), 'migration' FROM deliveries
           WHERE delivered_at IS NOT NULL GROUP BY item_id, occurrence""",
    ],
    # 8: snoozing one occurrence of a recurring reminder without moving the series (ADR 0009)
    [
        "ALTER TABLE items ADD COLUMN snoozed_until TEXT",
        # due() asks for due_at OR snoozed_until in the past: with both indexed, SQLite searches each
        "CREATE INDEX IF NOT EXISTS idx_items_snoozed ON items (status, snoozed_until)",
    ],
    # 9: per-item channels and the urgent flag (#20, ADR 0011). Both nullable on purpose: NULL
    # channels means MAESTRO_CHANNELS, and NULL urgent means the item predates the flag, so its
    # urgency keeps being read from the text. New rows always carry explicit values.
    [
        "ALTER TABLE items ADD COLUMN channels TEXT",
        "ALTER TABLE items ADD COLUMN urgent INTEGER",
    ],
    # 10: tasks, a third kind between reminders and notes (#114, ADR 0015). `kind` is free text, so
    # nothing about the column changes; what is new is a third value and an index for listing one
    # kind at a time. Existing rows are left exactly as they are: a note written before tasks
    # existed stays a note, however much it reads like something to do.
    [
        "CREATE INDEX IF NOT EXISTS idx_items_kind ON items (kind, status)",
    ],
    # 11: which interpreter read each capture, "rules" or "model" (#116). Nullable on purpose:
    # existing rows weren't recorded, and NULL says "not known" rather than guessing "rules".
    [
        "ALTER TABLE items ADD COLUMN interpreter TEXT",
    ],
    # 12: captures awaiting confirmation, and undo (#117). A capture holds its proposed actions as
    # JSON; nothing reaches `items` until it is confirmed, and `items.capture_id` records where a
    # confirmed item came from so it can be undone. Old items keep NULL: they can't be undone.
    [
        """CREATE TABLE IF NOT EXISTS captures (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            status      TEXT NOT NULL,
            actions     TEXT NOT NULL,
            source      TEXT NOT NULL,
            address     TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL,
            interpreter TEXT,
            doubts      TEXT NOT NULL DEFAULT '',
            dedupe_key  TEXT,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            undo_until  TEXT,
            decided_at  TEXT,
            decided_via TEXT
        )""",
        # The expiry sweep reads (status, expires_at); reply matching reads an owner's latest.
        "CREATE INDEX IF NOT EXISTS idx_captures_status ON captures (status, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_captures_owner ON captures (source, address, status)",
        # One proposal per provider message, however often the webhook retries.
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_dedupe ON captures (source, dedupe_key)
           WHERE dedupe_key IS NOT NULL""",
        "ALTER TABLE items ADD COLUMN capture_id INTEGER",
    ],
    # 13: readings that need a look (#126, ADR 0017). `gap` names what an item is still missing —
    # "when", "what" or "channel" — and NULL, which every existing row keeps, means nothing is: a
    # database upgraded to here has no items needing a look, exactly as before.
    # No index: `due` already has idx_items_due, where the added `gap IS NULL` matches almost every
    # row, and the list of things awaiting an answer is only ever as long as a person's patience.
    [
        "ALTER TABLE items ADD COLUMN gap TEXT",
    ],
    # 14: devices signed in with a token of their own (#108, ADR 0018). Only the token's hash is
    # kept; a revoked device keeps its row so the name and dates stay on record. The hash is the
    # lookup every authenticated request makes, so it is unique and indexed.
    [
        """CREATE TABLE IF NOT EXISTS devices (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            token_hash   TEXT NOT NULL UNIQUE,
            created_at   TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at   TEXT
        )""",
    ],
]


class SQLiteStore(Store):
    backend = "sqlite"

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

    def describe(self) -> str:
        return "in memory" if self.path == ":memory:" else str(Path(self.path).expanduser())

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
            migrations.apply_pending(
                MIGRATIONS,
                read_version=self._read_version,
                write_version=self._write_version,
                apply_one=self._apply_migration,
            )

    def _read_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _write_version(self, number: int) -> None:
        # PRAGMA takes no parameters; `number` is the migration's index, never user input.
        self.conn.execute(f"PRAGMA user_version = {number}")

    def _apply_migration(self, migration: Migration) -> None:
        if callable(migration):
            migration(self.conn, self.tz)
        else:
            for sql in migration:
                self.conn.execute(sql)

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
            snoozed_until=self._from_db(row["snoozed_until"]),
            channels=row["channels"].split(",") if row["channels"] else None,
            urgent=None if row["urgent"] is None else bool(row["urgent"]),
            interpreter=row["interpreter"],
            capture_id=row["capture_id"],
            gap=row["gap"],
        )

    @property
    def journal_mode(self) -> str:
        return str(self.conn.execute("PRAGMA journal_mode").fetchone()[0])

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    _INSERT_ITEM = (
        "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence,"
        " channels, urgent, interpreter, gap) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def _item_values(self, item: Item) -> tuple[object, ...]:
        return (item.text, item.kind, item.status, self._to_db(item.due_at),
                self._to_db(item.created_at), item.source_text, item.recurrence,
                ",".join(item.channels) if item.channels else None,
                None if item.urgent is None else int(item.urgent), item.interpreter, item.gap)

    def add(self, item: Item) -> Item:
        with self._transaction():
            cur = self.conn.execute(self._INSERT_ITEM, self._item_values(item))
        item.id = cur.lastrowid
        return item

    def add_once(self, source: str, external_id: str, item: Item) -> tuple[Item, bool]:
        with self._transaction(immediate=True):
            row = self.conn.execute(
                "SELECT item_id FROM inbound_messages WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row is not None:
                existing = self.conn.execute("SELECT * FROM items WHERE id = ?", (row["item_id"],)).fetchone()
                if existing is not None:
                    return self._item(existing), False
            cur = self.conn.execute(self._INSERT_ITEM, self._item_values(item))
            item.id = cur.lastrowid
            self.conn.execute(
                "INSERT OR REPLACE INTO inbound_messages (source, external_id, item_id) VALUES (?, ?, ?)",
                (source, external_id, item.id),
            )
        return item, True

    def is_claimed(self, source: str, external_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM inbound_messages WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone() is not None

    def latest_delivered_occurrence(self, item_id: int) -> datetime | None:
        row = self.conn.execute(
            "SELECT MAX(occurrence) FROM deliveries WHERE item_id = ? AND delivered_at IS NOT NULL",
            (item_id,),
        ).fetchone()
        return self._from_db(row[0]) if row else None

    def claim_inbound(self, source: str, external_id: str, item_id: int) -> bool:
        with self._transaction():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO inbound_messages (source, external_id, item_id) VALUES (?, ?, ?)",
                (source, external_id, item_id),
            )
        return cur.rowcount > 0

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def list(self, include_done: bool = False) -> builtins.list[Item]:
        sql = "SELECT * FROM items"
        if not include_done:
            sql += f" WHERE status != '{Status.DONE}'"
        # Reminders by when they next go out first, then tasks, then notes, each by creation
        # time (#114). The id breaks ties (times are stored to the second), so the order is the
        # same on every backend and a list never reshuffles after an unrelated update.
        # The kind term only separates the untimed items: everything with a due time is a reminder.
        sql += (" ORDER BY due_at IS NULL, COALESCE(snoozed_until, due_at),"
                f" CASE WHEN kind = '{Kind.TASK}' THEN 0 ELSE 1 END, created_at, id")
        return [self._item(r) for r in self.conn.execute(sql)]

    def counts(self) -> dict[tuple[str, str], int]:
        rows = self.conn.execute("SELECT kind, status, COUNT(*) FROM items GROUP BY kind, status")
        return {(kind, status): n for kind, status, n in rows}

    def search(self, query: str) -> builtins.list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE text LIKE ? ORDER BY created_at DESC, id DESC",
            (f"%{query}%",),
        )
        return [self._item(r) for r in rows]

    def due(self, now: datetime) -> builtins.list[Item]:
        # `gap IS NULL`: an item nobody has confirmed the meaning of is never delivered (#126).
        rows = self.conn.execute(
            "SELECT * FROM items WHERE status = ? AND gap IS NULL"
            " AND (due_at <= ? OR snoozed_until <= ?)"
            " ORDER BY COALESCE(snoozed_until, due_at), id",
            (Status.PENDING, self._to_db(now), self._to_db(now)),
        )
        return [self._item(r) for r in rows]

    # Row updates shared by the public methods below; callers hold the transaction.
    def _set_status(self, item_id: int, status: Status) -> int:
        return self.conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id)).rowcount

    def _move(self, item_id: int, due_at: datetime, snoozed_until: datetime | None = None) -> int:
        return self.conn.execute(
            "UPDATE items SET due_at = ?, snoozed_until = ?, status = ?, kind = ? WHERE id = ?",
            (self._to_db(due_at), self._to_db(snoozed_until) if snoozed_until else None, Status.PENDING,
             Kind.REMINDER, item_id),
        ).rowcount

    def _series(self, item_id: int) -> logic.Series | None:
        """The recurring item behind this id if it's still running, else None."""
        row = self.conn.execute("SELECT recurrence, due_at, snoozed_until, status FROM items WHERE id = ?",
                                (item_id,)).fetchone()
        if row is None or not row["recurrence"] or not row["due_at"] or row["status"] == Status.DONE:
            return None
        return logic.Series(row["recurrence"], self._required_from_db(row["due_at"]),
                            self._from_db(row["snoozed_until"]))

    def _delivered_target(self, item_id: int, now: datetime | None,
                          occurrence: datetime | None) -> datetime | None:
        """The delivered occurrence a done/snooze is about, or None when it's about the upcoming one.

        A reply to a specific delivery (email, call) names its occurrence. Otherwise it's the latest
        delivery if that was recent and nobody has acknowledged it yet: once it's acknowledged (or if it
        was long ago), "done" means the next one, e.g. "I took them early"."""
        if occurrence is not None:
            return occurrence
        if (latest := self._recent_delivery(item_id, now)) is None:
            return None
        acknowledged = self.conn.execute(
            "SELECT 1 FROM acknowledgements WHERE item_id = ? AND occurrence = ?", (item_id, latest)
        ).fetchone()
        return None if acknowledged else self._required_from_db(latest)

    def set_status(self, item_id: int, status: Status) -> bool:
        with self._transaction():
            changed = self._set_status(item_id, status)
        return changed > 0

    def reschedule(self, item_id: int, due_at: datetime) -> bool:
        with self._transaction():
            changed = self._move(item_id, due_at)
        return changed > 0

    def finish_occurrence(self, item_id: int, occurrence: datetime, next_due: datetime | None) -> bool:
        with self._transaction():
            if next_due is None:
                cur = self.conn.execute(
                    "UPDATE items SET status = ? WHERE id = ? AND status = ? AND due_at = ?",
                    (Status.FIRED, item_id, Status.PENDING, self._to_db(occurrence)),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE items SET due_at = ? WHERE id = ? AND status = ? AND due_at = ?",
                    (self._to_db(next_due), item_id, Status.PENDING, self._to_db(occurrence)),
                )
        return cur.rowcount > 0

    def complete(self, item_id: int, via: str, now: datetime | None = None,
                 occurrence: datetime | None = None) -> bool:
        with self._transaction():
            if series := self._series(item_id):
                move = logic.complete(series, self._delivered_target(item_id, now, occurrence))
                changed = self._move(item_id, move.due_at, snoozed_until=move.snoozed_until)
            else:
                changed = self._set_status(item_id, Status.DONE)
            acknowledged = bool(changed) and self._acknowledge(item_id, via, now, occurrence)
        if acknowledged:
            METRICS.inc("maestro_acknowledgements_total", via=via)  # only once committed
        return changed > 0

    def stop(self, item_id: int, via: str, now: datetime | None = None) -> bool:
        with self._transaction():
            changed = self.conn.execute("UPDATE items SET status = ?, snoozed_until = NULL WHERE id = ?",
                                        (Status.DONE, item_id)).rowcount
            acknowledged = bool(changed) and self._acknowledge(item_id, via, now)
        if acknowledged:
            METRICS.inc("maestro_acknowledgements_total", via=via)
        return changed > 0

    def snooze(self, item_id: int, until: datetime, via: str, now: datetime | None = None,
               occurrence: datetime | None = None) -> bool:
        with self._transaction():
            if series := self._series(item_id):
                move = logic.snooze(series, until, self._delivered_target(item_id, now, occurrence))
                changed = self._move(item_id, move.due_at, snoozed_until=move.snoozed_until)
            else:
                changed = self._move(item_id, until)
            acknowledged = bool(changed) and self._acknowledge(item_id, via, now, occurrence)
        if acknowledged:
            METRICS.inc("maestro_acknowledgements_total", via=via)
        return changed > 0

    def finish_snooze(self, item_id: int, occurrence: datetime) -> bool:
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE items SET snoozed_until = NULL WHERE id = ? AND status = ? AND snoozed_until = ?",
                (item_id, Status.PENDING, self._to_db(occurrence)),
            )
        return cur.rowcount > 0

    def _recent_delivery(self, item_id: int, now: datetime | None) -> str | None:
        """The occurrence (as stored) of the item's latest delivery recent enough to react to."""
        now = now or datetime.now(self.tz)
        row = self.conn.execute(
            "SELECT occurrence FROM deliveries WHERE item_id = ? AND delivered_at IS NOT NULL"
            " AND delivered_at >= ? ORDER BY occurrence DESC LIMIT 1",
            (item_id, self._to_db(logic.ack_cutoff(now))),
        ).fetchone()
        return str(row["occurrence"]) if row else None

    def _acknowledge(self, item_id: int, via: str, now: datetime | None,
                     occurrence: datetime | None = None) -> bool:
        """Acknowledge the item's latest delivery if it was recent (inside a transaction). For a
        recurring item the due time has already moved on, so the occurrence comes from the delivery
        records; how recent counts is `logic.ack_cutoff`."""
        # A reply that names its delivery acknowledges exactly that one, however late it comes.
        stored = self._to_db(occurrence) if occurrence else self._recent_delivery(item_id, now)
        if stored is None:
            return False  # nothing sent recently: nothing to acknowledge
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO acknowledgements (item_id, occurrence, acknowledged_at, via)"
            " VALUES (?, ?, ?, ?)",
            (item_id, stored, self._to_db(now or datetime.now(self.tz)), via),
        )
        return cur.rowcount > 0

    def acknowledgement(self, item_id: int, occurrence: datetime) -> Acknowledgement | None:
        row = self.conn.execute(
            "SELECT acknowledged_at, via FROM acknowledgements WHERE item_id = ? AND occurrence = ?",
            (item_id, self._to_db(occurrence)),
        ).fetchone()
        return Acknowledgement(self._required_from_db(row["acknowledged_at"]), row["via"]) if row else None

    _UNACKNOWLEDGED = """
        FROM items i
        JOIN (SELECT item_id, MAX(occurrence) AS occurrence FROM deliveries
              WHERE delivered_at IS NOT NULL GROUP BY item_id) d ON d.item_id = i.id
        LEFT JOIN acknowledgements a ON a.item_id = i.id AND a.occurrence = d.occurrence
        WHERE a.item_id IS NULL AND i.status != ?"""

    def unacknowledged(self, since: datetime | None = None) -> builtins.list[tuple[Item, datetime]]:
        recent = " AND d.occurrence >= ?" if since else ""
        params: tuple[object, ...] = (Status.DONE, self._to_db(since)) if since else (Status.DONE,)
        rows = self.conn.execute(
            f"SELECT i.*, d.occurrence AS delivered_occurrence {self._UNACKNOWLEDGED}{recent}"
            " ORDER BY d.occurrence, i.id",
            params,
        )
        return [(self._item(r), self._required_from_db(r["delivered_occurrence"])) for r in rows]

    def unacknowledged_ids(self) -> frozenset[int]:
        rows = self.conn.execute(f"SELECT i.id {self._UNACKNOWLEDGED}", (Status.DONE,))
        return frozenset(r[0] for r in rows)

    def delete(self, item_id: int) -> bool:
        with self._transaction():
            self.conn.execute("DELETE FROM acknowledgements WHERE item_id = ?", (item_id,))
            self.conn.execute("DELETE FROM deliveries WHERE item_id = ?", (item_id,))
            self.conn.execute("DELETE FROM inbound_messages WHERE item_id = ?", (item_id,))
            cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return cur.rowcount > 0

    # --- captures awaiting confirmation (#117) ----------------------------------------------

    def _capture(self, row: sqlite3.Row) -> Capture:
        return Capture(
            id=row["id"],
            status=CaptureStatus(row["status"]),
            actions=row["actions"],
            source=row["source"],
            address=row["address"],
            source_text=row["source_text"],
            interpreter=row["interpreter"],
            doubts=row["doubts"],
            created_at=self._required_from_db(row["created_at"]),
            expires_at=self._required_from_db(row["expires_at"]),
            undo_until=self._from_db(row["undo_until"]),
            decided_at=self._from_db(row["decided_at"]),
            decided_via=row["decided_via"],
        )

    def add_capture(self, capture: Capture, dedupe_key: str | None = None) -> tuple[Capture, bool]:
        with self._transaction(immediate=True):
            if dedupe_key is not None:
                row = self.conn.execute(
                    "SELECT * FROM captures WHERE source = ? AND dedupe_key = ?",
                    (capture.source, dedupe_key),
                ).fetchone()
                if row is not None:
                    return self._capture(row), False
            cur = self.conn.execute(
                "INSERT INTO captures (status, actions, source, address, source_text, interpreter,"
                " doubts, dedupe_key, created_at, expires_at, undo_until, decided_at, decided_via)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (capture.status, capture.actions, capture.source, capture.address,
                 capture.source_text, capture.interpreter, capture.doubts, dedupe_key,
                 self._to_db(capture.created_at), self._to_db(capture.expires_at),
                 # A split capture arrives already applied, with its undo window running (#126);
                 # a capture awaiting confirmation leaves all three NULL, as it always has.
                 self._to_db(capture.undo_until), self._to_db(capture.decided_at),
                 capture.decided_via),
            )
        capture.id = cur.lastrowid
        return capture, True

    def get_capture(self, capture_id: int) -> Capture | None:
        row = self.conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        return self._capture(row) if row else None

    def confirm_capture(self, capture_id: int, via: str, now: datetime, undo_until: datetime) -> bool:
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE captures SET status = ?, decided_at = ?, decided_via = ?, undo_until = ?"
                " WHERE id = ? AND status = ? AND expires_at > ?",
                (CaptureStatus.APPLIED, self._to_db(now), via, self._to_db(undo_until),
                 capture_id, CaptureStatus.PENDING, self._to_db(now)),
            )
        return cur.rowcount > 0

    def cancel_capture(self, capture_id: int, via: str, now: datetime) -> bool:
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE captures SET status = ?, decided_at = ?, decided_via = ?"
                " WHERE id = ? AND status = ?",
                (CaptureStatus.CANCELLED, self._to_db(now), via, capture_id, CaptureStatus.PENDING),
            )
        return cur.rowcount > 0

    def undo_capture(self, capture_id: int, via: str, now: datetime) -> bool:
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE captures SET status = ?, decided_at = ?, decided_via = ?"
                " WHERE id = ? AND status = ? AND undo_until > ?",
                (CaptureStatus.UNDONE, self._to_db(now), via, capture_id, CaptureStatus.APPLIED,
                 self._to_db(now)),
            )
        return cur.rowcount > 0

    def expire_captures(self, now: datetime) -> int:
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE captures SET status = ?, decided_at = ? WHERE status = ? AND expires_at <= ?",
                (CaptureStatus.EXPIRED, self._to_db(now), CaptureStatus.PENDING, self._to_db(now)),
            )
        return cur.rowcount

    def captures(self, status: str | None = None, source: str | None = None,
                 address: str | None = None) -> builtins.list[Capture]:
        sql, params = "SELECT * FROM captures", []
        clauses = [("status = ?", status), ("source = ?", source), ("address = ?", address)]
        where = [(c, v) for c, v in clauses if v is not None]
        if where:
            sql += " WHERE " + " AND ".join(c for c, _ in where)
            params = [v for _, v in where]
        sql += " ORDER BY created_at DESC, id DESC"
        return [self._capture(r) for r in self.conn.execute(sql, params)]

    def link_capture(self, capture_id: int, item_ids: builtins.list[int]) -> None:
        with self._transaction():
            self.conn.executemany("UPDATE items SET capture_id = ? WHERE id = ?",
                                  [(capture_id, item_id) for item_id in item_ids])

    def items_for_capture(self, capture_id: int) -> builtins.list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE capture_id = ? ORDER BY id", (capture_id,))
        return [self._item(r) for r in rows]

    # --- devices (#108, ADR 0018) -----------------------------------------------------------

    def _device(self, row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"],
            name=row["name"],
            token_hash=row["token_hash"],
            created_at=self._required_from_db(row["created_at"]),
            last_seen_at=self._from_db(row["last_seen_at"]),
            revoked_at=self._from_db(row["revoked_at"]),
        )

    def add_device(self, device: Device) -> Device:
        with self._transaction():
            cur = self.conn.execute(
                "INSERT INTO devices (name, token_hash, created_at, last_seen_at, revoked_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (device.name, device.token_hash, self._to_db(device.created_at),
                 self._to_db(device.last_seen_at), self._to_db(device.revoked_at)),
            )
        device.id = cur.lastrowid
        return device

    def find_device(self, token_hash: str) -> Device | None:
        row = self.conn.execute(
            "SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)
        ).fetchone()
        return self._device(row) if row else None

    def get_device(self, device_id: int) -> Device | None:
        row = self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return self._device(row) if row else None

    def devices(self) -> builtins.list[Device]:
        rows = self.conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY id")
        return [self._device(r) for r in rows]

    def revoke_device(self, device_id: int, now: datetime) -> bool:
        with self._transaction():
            cur = self.conn.execute(
                "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (self._to_db(now), device_id),
            )
        return cur.rowcount > 0

    def touch_device(self, device_id: int, now: datetime) -> None:
        with self._transaction():
            self.conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?",
                              (self._to_db(now), device_id))

    # --- readings that need a look (#126) ---------------------------------------------------

    def needs_look(self, source: str | None = None, address: str | None = None,
                   since: datetime | None = None) -> builtins.list[Item]:
        # LEFT JOIN, so a gapped item whose capture row is gone is still listed rather than hidden;
        # the owner clauses only come into play when a caller asks about one sender.
        sql = ("SELECT i.* FROM items i LEFT JOIN captures c ON c.id = i.capture_id"
               " WHERE i.gap IS NOT NULL AND i.status != ?")
        params: builtins.list[object] = [Status.DONE]
        for clause, value in (("c.source = ?", source), ("c.address = ?", address),
                              ("i.created_at >= ?", self._to_db(since) if since else None)):
            if value is not None:
                sql += f" AND {clause}"
                params.append(value)
        sql += " ORDER BY i.created_at DESC, i.id DESC"
        return [self._item(r) for r in self.conn.execute(sql, params)]

    def fill_gap(self, item_id: int, due_at: datetime | None = None, recurrence: str | None = None,
                 channels: builtins.list[str] | None = None, text: str | None = None,
                 kind: Kind | None = None) -> bool:
        # A dict, so `kind` given alongside a `due_at` replaces what the time implied instead of
        # assigning the same column twice, which PostgreSQL refuses outright.
        sets: dict[str, object] = {"gap": None}
        if due_at is not None:
            # A filled time makes it a reminder again, pending and ready to fire, exactly as
            # `reschedule` does for an item that already had one.
            sets |= {"due_at": self._to_db(due_at), "status": Status.PENDING,
                     "kind": Kind.REMINDER, "recurrence": recurrence}
        if kind is not None:
            sets["kind"] = kind
        if channels is not None:
            sets["channels"] = ",".join(channels) or None
        if text is not None:
            sets["text"] = text
        # The column names are this method's own literals; every value is bound.
        assignments = ", ".join(f"{name} = ?" for name in sets)
        with self._transaction():
            # `gap IS NOT NULL` is the check-and-set: two answers racing, one fills it.
            cur = self.conn.execute(
                f"UPDATE items SET {assignments} WHERE id = ? AND gap IS NOT NULL",
                (*sets.values(), item_id),
            )
        return cur.rowcount > 0

    def last_delivered(self, channel: str) -> int | None:
        row = self.conn.execute(
            "SELECT d.item_id FROM deliveries d JOIN items i ON i.id = d.item_id"
            " WHERE (d.channel = ? OR (d.channel LIKE 'escalation%' AND"
            "        substr(d.channel, instr(d.channel, ':') + 1) = ?))"
            " AND d.delivered_at IS NOT NULL ORDER BY d.delivered_at DESC LIMIT 1",
            (channel, channel),  # escalation stages on the same channel ("escalation2:<channel>") count too
        ).fetchone()
        return row[0] if row else None

    def lease_holder(self, now: datetime) -> str | None:
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
