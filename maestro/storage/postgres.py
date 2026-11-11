"""PostgreSQL persistence, for installs that already run a database server.

Opened by a `postgresql://` URL (see `maestro.storage.factory`); SQLite stays the default. What the
methods promise is documented once, on the interface in `maestro.storage.base`, and both backends
are held to it by `tests/test_storage_conformance.py`. What is written here is how PostgreSQL keeps
those promises, where that differs from SQLite:

- **Times** are `timestamptz`, not ISO strings. The session runs in UTC and values are truncated to
  whole seconds on the way in, because an occurrence is part of a primary key: a delivery recorded
  with microseconds would not match the same occurrence read back from an item's due time.
- **Concurrency** can't lean on one database-wide write lock. Read-then-write sequences that must
  not interleave (migrating, taking the lease, claiming an inbound message) take a transaction-scoped
  advisory lock first, which other processes respect and PostgreSQL releases at commit or rollback.
- **Connections** follow ADR 0003 as SQLite does: one connection, used from one thread.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from maestro.models import Acknowledgement, Capture, CaptureStatus, Delivery, Item, Kind, Status
from maestro.observability import METRICS
from maestro.storage import logic, migrations
from maestro.storage.base import Store
from maestro.tz import UTC, get_timezone, localize

log = logging.getLogger(__name__)

__all__ = ["MIGRATIONS", "DriverError", "PostgresStore"]

# What psycopg raises when the server is unreachable or refuses a statement; `doctor`
# reports it instead of showing a traceback (see factory.driver_errors).
DriverError = psycopg.Error

# Advisory lock keys: (class, object). The class is "ma" in ASCII, so these can't collide with
# another application's locks by accident.
_LOCK_CLASS = 0x6D61
_MIGRATE_LOCK = 1
_LEASE_LOCK = 2
# Claims are per inbound message, so the object id is a hash of its identity instead of a constant.
_CLAIM_CLASS = 0x6D62

# PostgreSQL support starts here, so migration 1 is the schema as it stands rather than a replay of
# SQLite's eight steps: there is no older PostgreSQL database in the world to upgrade. Later schema
# changes append a migration here and to the SQLite list, and
# tests/test_storage_postgres.py::test_both_backends_describe_the_same_schema keeps the two in step.
MIGRATIONS: list[list[str]] = [
    [
        """CREATE TABLE IF NOT EXISTS items (
            id            BIGSERIAL PRIMARY KEY,
            text          TEXT NOT NULL,
            kind          TEXT NOT NULL,
            status        TEXT NOT NULL,
            due_at        TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL,
            source_text   TEXT NOT NULL DEFAULT '',
            recurrence    TEXT,
            snoozed_until TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_items_due ON items (status, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_items_snoozed ON items (status, snoozed_until)",
        """CREATE TABLE IF NOT EXISTS deliveries (
            item_id         BIGINT NOT NULL,
            occurrence      TIMESTAMPTZ NOT NULL,
            channel         TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            delivered_at    TIMESTAMPTZ,
            next_attempt_at TIMESTAMPTZ,
            last_error      TEXT,
            PRIMARY KEY (item_id, occurrence, channel)
        )""",
        """CREATE TABLE IF NOT EXISTS acknowledgements (
            item_id         BIGINT NOT NULL,
            occurrence      TIMESTAMPTZ NOT NULL,
            acknowledged_at TIMESTAMPTZ NOT NULL,
            via             TEXT NOT NULL,
            PRIMARY KEY (item_id, occurrence)
        )""",
        """CREATE TABLE IF NOT EXISTS inbound_messages (
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            item_id     BIGINT NOT NULL,
            PRIMARY KEY (source, external_id)
        )""",
        """CREATE TABLE IF NOT EXISTS scheduler_lease (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            owner      TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        )""",
    ],
    # 2: per-item channels and the urgent flag (#20, ADR 0011), matching SQLite's migration 9. Both
    # nullable on purpose: NULL channels means MAESTRO_CHANNELS, and NULL urgent means the item
    # predates the flag, so its urgency keeps being read from the text.
    [
        "ALTER TABLE items ADD COLUMN channels TEXT",
        "ALTER TABLE items ADD COLUMN urgent BOOLEAN",
    ],
    # 3: tasks (#114), matching SQLite's migration 10. `kind` is free text on both backends, so the
    # only change is the index for listing one kind at a time; existing rows keep the kind they have.
    [
        "CREATE INDEX IF NOT EXISTS idx_items_kind ON items (kind, status)",
    ],
    # 4: which interpreter read each capture (#116), matching SQLite's migration 11. Nullable:
    # existing rows weren't recorded, and NULL says "not known" rather than guessing "rules".
    [
        "ALTER TABLE items ADD COLUMN interpreter TEXT",
    ],
    # 5: captures awaiting confirmation, and undo (#117), matching SQLite's migration 12. Nothing
    # reaches `items` until a capture is confirmed; `items.capture_id` says where a confirmed item
    # came from so it can be undone. Old items keep NULL: they can't be undone.
    [
        """CREATE TABLE IF NOT EXISTS captures (
            id          BIGSERIAL PRIMARY KEY,
            status      TEXT NOT NULL,
            actions     TEXT NOT NULL,
            source      TEXT NOT NULL,
            address     TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL,
            interpreter TEXT,
            doubts      TEXT NOT NULL DEFAULT '',
            dedupe_key  TEXT,
            created_at  TIMESTAMPTZ NOT NULL,
            expires_at  TIMESTAMPTZ NOT NULL,
            undo_until  TIMESTAMPTZ,
            decided_at  TIMESTAMPTZ,
            decided_via TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_captures_status ON captures (status, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_captures_owner ON captures (source, address, status)",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_dedupe ON captures (source, dedupe_key)
           WHERE dedupe_key IS NOT NULL""",
        "ALTER TABLE items ADD COLUMN capture_id BIGINT",
    ],
    # 6: readings that need a look (#126, ADR 0017), matching SQLite's migration 13. `gap` names
    # what an item is still missing; NULL, which every existing row keeps, means nothing is. No
    # index, for the reasons given there.
    [
        "ALTER TABLE items ADD COLUMN gap TEXT",
    ],
]


def _version(number: int) -> str:
    """PostgreSQL reports its version as one integer: 170011 is 17.11."""
    return f"{number // 10000}.{number % 10000}"


class PostgresStore(Store):
    backend = "postgresql"

    def __init__(self, url: str, tz: ZoneInfo | None = None):
        self.tz = tz or get_timezone()
        self.url = url
        self.closed = False
        self._in_transaction = False
        self._connect()
        try:
            self._migrate()
        except BaseException:
            self.close()  # the caller never gets the object, so it can't close it
            raise

    def _connect(self) -> None:
        self.conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        # Every timestamptz comes back in the session's zone; pinning it to UTC keeps results
        # independent of the server's timezone setting, as ADR 0001 requires.
        self.conn.execute("SET TIME ZONE 'UTC'")

    def _execute(self, sql: str, params: tuple[object, ...] | None = None) -> psycopg.Cursor[dict]:
        """Run one statement, reconnecting first if the server went away.

        Nothing is retried: a statement that failed may or may not have been applied, and a
        reminder must not go out twice. The caller tries again — the scheduler on its next tick,
        the API on the next request — and finds a working connection.
        """
        self._reconnect_if_lost()
        return self.conn.execute(sql, params)

    def _reconnect_if_lost(self) -> None:
        """A restarted or failed-over server leaves a dead connection behind; without this, every
        later call would fail and the scheduler would stop delivering until someone restarted it.

        Never inside a transaction (the rest of it would land on another connection) and never
        after the caller closed the store.
        """
        if self.closed or self._in_transaction or not (self.conn.closed or self.conn.broken):
            return
        log.warning("The PostgreSQL connection was lost; reconnecting")
        self._connect()
        METRICS.inc("maestro_store_reconnects_total")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run writes atomically: commit on success, roll back on error."""
        self._reconnect_if_lost()  # before the transaction opens, never during it
        with self.conn.transaction():
            self._in_transaction = True
            try:
                yield
            finally:
                self._in_transaction = False

    def close(self) -> None:
        self.conn.close()
        self.closed = True

    def describe(self) -> str:
        """The server and database, never the URL: that would carry the password."""
        info = self.conn.info
        where = f"{info.host}:{info.port}" if not info.host.startswith("/") else info.host
        return f"postgresql {_version(info.server_version)} at {where}/{info.dbname}"

    def _lock(self, object_id: int, lock_class: int = _LOCK_CLASS) -> None:
        """Hold a lock for the rest of the transaction, so a read-then-write can't interleave with
        the same sequence in another process. Waits rather than failing, like SQLite's busy timeout."""
        self._execute("SELECT pg_advisory_xact_lock(%s, %s)", (lock_class, object_id))

    def _claim_lock(self, source: str, external_id: str) -> None:
        # Length-prefixed, so ("sms", "1:2") and ("sms:1", "2") are different locks. A separator
        # byte would do, except PostgreSQL text can't hold NUL and anything else can occur in an id.
        self._execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                          (_CLAIM_CLASS, f"{len(source)}:{source}{external_id}"))

    def _migrate(self) -> None:
        with self._transaction():
            # Taken before the version is read, so two processes starting together (`maestro serve`
            # and `maestro add`) can't both decide the same migration is missing.
            self._lock(_MIGRATE_LOCK)
            self._execute("""CREATE TABLE IF NOT EXISTS schema_version (
                id      SMALLINT PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )""")
            migrations.apply_pending(
                MIGRATIONS,
                read_version=self._read_version,
                write_version=self._write_version,
                apply_one=self._apply_migration,
            )

    def _read_version(self) -> int:
        row = self._execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return int(row["version"]) if row else 0

    def _write_version(self, number: int) -> None:
        self._execute(
            "INSERT INTO schema_version (id, version) VALUES (1, %s)"
            " ON CONFLICT (id) DO UPDATE SET version = excluded.version",
            (number,),
        )

    def _apply_migration(self, migration: builtins.list[str]) -> None:
        for sql in migration:
            self._execute(sql)

    @property
    def schema_version(self) -> int:
        return self._read_version()

    # --- values -------------------------------------------------------------------------------

    def _to_db(self, dt: datetime | None) -> datetime | None:
        """UTC, to the second: occurrences are keys, so they must compare exactly (see the module
        docstring)."""
        if dt is None:
            return None
        return localize(dt, self.tz).astimezone(UTC).replace(microsecond=0)

    def _required_from_db(self, value: datetime) -> datetime:
        return value.astimezone(self.tz)

    def _from_db(self, value: datetime | None) -> datetime | None:
        return self._required_from_db(value) if value else None

    def _item(self, row: dict) -> Item:
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
            urgent=row["urgent"],
            interpreter=row["interpreter"],
            capture_id=row["capture_id"],
            gap=row["gap"],
        )

    _INSERT_ITEM = (
        "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence,"
        " channels, urgent, interpreter, gap)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"
    )

    def _insert(self, item: Item) -> int:
        row = self._execute(
            self._INSERT_ITEM,
            (item.text, str(item.kind), str(item.status), self._to_db(item.due_at),
             self._to_db(item.created_at), item.source_text, item.recurrence,
             ",".join(item.channels) if item.channels else None, item.urgent, item.interpreter,
             item.gap),
        ).fetchone()
        assert row is not None  # RETURNING always yields a row
        return int(row["id"])

    # --- capture ------------------------------------------------------------------------------

    def add(self, item: Item) -> Item:
        with self._transaction():
            item.id = self._insert(item)
        return item

    def add_once(self, source: str, external_id: str, item: Item) -> tuple[Item, bool]:
        with self._transaction():
            self._claim_lock(source, external_id)
            row = self._execute(
                "SELECT item_id FROM inbound_messages WHERE source = %s AND external_id = %s",
                (source, external_id),
            ).fetchone()
            if row is not None:
                existing = self._execute("SELECT * FROM items WHERE id = %s",
                                             (row["item_id"],)).fetchone()
                if existing is not None:
                    return self._item(existing), False
            item.id = self._insert(item)
            self._execute(
                "INSERT INTO inbound_messages (source, external_id, item_id) VALUES (%s, %s, %s)"
                " ON CONFLICT (source, external_id) DO UPDATE SET item_id = excluded.item_id",
                (source, external_id, item.id),
            )
        return item, True

    def is_claimed(self, source: str, external_id: str) -> bool:
        return self._execute(
            "SELECT 1 FROM inbound_messages WHERE source = %s AND external_id = %s", (source, external_id)
        ).fetchone() is not None

    def claim_inbound(self, source: str, external_id: str, item_id: int) -> bool:
        with self._transaction():
            cur = self._execute(
                "INSERT INTO inbound_messages (source, external_id, item_id) VALUES (%s, %s, %s)"
                " ON CONFLICT (source, external_id) DO NOTHING",
                (source, external_id, item_id),
            )
        return cur.rowcount > 0

    # --- reading ------------------------------------------------------------------------------

    def get(self, item_id: int) -> Item | None:
        row = self._execute("SELECT * FROM items WHERE id = %s", (item_id,)).fetchone()
        return self._item(row) if row else None

    def list(self, include_done: bool = False) -> builtins.list[Item]:
        sql = "SELECT * FROM items"
        params: tuple[object, ...] = ()
        if not include_done:
            sql += " WHERE status != %s"
            params = (str(Status.DONE),)
        # Reminders by when they next go out first, then tasks, then notes, each by creation
        # time (#114). The id breaks ties (times are stored to the second), so the order is the
        # same on every backend and a list never reshuffles after an unrelated update.
        # The kind term only separates the untimed items: everything with a due time is a reminder.
        sql += (" ORDER BY due_at IS NULL, COALESCE(snoozed_until, due_at),"
                f" CASE WHEN kind = '{Kind.TASK}' THEN 0 ELSE 1 END, created_at, id")
        return [self._item(r) for r in self._execute(sql, params)]

    def counts(self) -> dict[tuple[str, str], int]:
        rows = self._execute("SELECT kind, status, COUNT(*) AS n FROM items GROUP BY kind, status")
        return {(r["kind"], r["status"]): r["n"] for r in rows}

    def search(self, query: str) -> builtins.list[Item]:
        # ILIKE, because SQLite's LIKE ignores case for ASCII and the two must agree.
        rows = self._execute(
            "SELECT * FROM items WHERE text ILIKE %s ORDER BY created_at DESC, id DESC", (f"%{query}%",)
        )
        return [self._item(r) for r in rows]

    def due(self, now: datetime) -> builtins.list[Item]:
        # `gap IS NULL`: an item nobody has confirmed the meaning of is never delivered (#126).
        rows = self._execute(
            "SELECT * FROM items WHERE status = %s AND gap IS NULL"
            " AND (due_at <= %s OR snoozed_until <= %s)"
            " ORDER BY COALESCE(snoozed_until, due_at), id",
            (str(Status.PENDING), self._to_db(now), self._to_db(now)),
        )
        return [self._item(r) for r in rows]

    # Row updates shared by the public methods below; callers hold the transaction.
    def _set_status(self, item_id: int, status: Status) -> int:
        return self._execute("UPDATE items SET status = %s WHERE id = %s",
                                 (str(status), item_id)).rowcount

    def _move(self, item_id: int, due_at: datetime, snoozed_until: datetime | None = None) -> int:
        return self._execute(
            "UPDATE items SET due_at = %s, snoozed_until = %s, status = %s, kind = %s WHERE id = %s",
            (self._to_db(due_at), self._to_db(snoozed_until) if snoozed_until else None,
             str(Status.PENDING), str(Kind.REMINDER), item_id),
        ).rowcount

    def _series(self, item_id: int) -> logic.Series | None:
        row = self._execute(
            "SELECT recurrence, due_at, snoozed_until, status FROM items WHERE id = %s", (item_id,)
        ).fetchone()
        if row is None or not row["recurrence"] or not row["due_at"] or row["status"] == Status.DONE:
            return None
        return logic.Series(row["recurrence"], self._required_from_db(row["due_at"]),
                            self._from_db(row["snoozed_until"]))

    def _delivered_target(self, item_id: int, now: datetime | None,
                          occurrence: datetime | None) -> datetime | None:
        """The delivered occurrence a done/snooze is about, or None when it's about the upcoming one."""
        if occurrence is not None:
            return occurrence
        if (latest := self._recent_delivery(item_id, now)) is None:
            return None
        acknowledged = self._execute(
            "SELECT 1 FROM acknowledgements WHERE item_id = %s AND occurrence = %s", (item_id, latest)
        ).fetchone()
        return None if acknowledged else self._required_from_db(latest)

    # --- actions ------------------------------------------------------------------------------

    def set_status(self, item_id: int, status: Status) -> bool:
        with self._transaction():
            changed = self._set_status(item_id, status)
        return changed > 0

    def reschedule(self, item_id: int, due_at: datetime) -> bool:
        with self._transaction():
            changed = self._move(item_id, due_at)
        return changed > 0

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
            changed = self._execute(
                "UPDATE items SET status = %s, snoozed_until = NULL WHERE id = %s",
                (str(Status.DONE), item_id),
            ).rowcount
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

    def delete(self, item_id: int) -> bool:
        with self._transaction():
            self._execute("DELETE FROM acknowledgements WHERE item_id = %s", (item_id,))
            self._execute("DELETE FROM deliveries WHERE item_id = %s", (item_id,))
            self._execute("DELETE FROM inbound_messages WHERE item_id = %s", (item_id,))
            cur = self._execute("DELETE FROM items WHERE id = %s", (item_id,))
        return cur.rowcount > 0

    # --- the scheduler's bookkeeping ------------------------------------------------------------

    def finish_occurrence(self, item_id: int, occurrence: datetime, next_due: datetime | None) -> bool:
        with self._transaction():
            if next_due is None:
                cur = self._execute(
                    "UPDATE items SET status = %s WHERE id = %s AND status = %s AND due_at = %s",
                    (str(Status.FIRED), item_id, str(Status.PENDING), self._to_db(occurrence)),
                )
            else:
                cur = self._execute(
                    "UPDATE items SET due_at = %s WHERE id = %s AND status = %s AND due_at = %s",
                    (self._to_db(next_due), item_id, str(Status.PENDING), self._to_db(occurrence)),
                )
        return cur.rowcount > 0

    def finish_snooze(self, item_id: int, occurrence: datetime) -> bool:
        with self._transaction():
            cur = self._execute(
                "UPDATE items SET snoozed_until = NULL"
                " WHERE id = %s AND status = %s AND snoozed_until = %s",
                (item_id, str(Status.PENDING), self._to_db(occurrence)),
            )
        return cur.rowcount > 0

    # --- deliveries and acknowledgements ---------------------------------------------------------

    def deliveries(self, item_id: int, occurrence: datetime) -> dict[str, Delivery]:
        rows = self._execute(
            "SELECT * FROM deliveries WHERE item_id = %s AND occurrence = %s",
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
            self._execute(
                "INSERT INTO deliveries (item_id, occurrence, channel, attempts, delivered_at,"
                " next_attempt_at, last_error) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (item_id, occurrence, channel) DO UPDATE SET attempts = excluded.attempts,"
                " delivered_at = excluded.delivered_at, next_attempt_at = excluded.next_attempt_at,"
                " last_error = excluded.last_error",
                (item_id, self._to_db(occurrence), delivery.channel, delivery.attempts,
                 self._to_db(delivery.delivered_at), self._to_db(delivery.next_attempt_at),
                 delivery.last_error),
            )

    def latest_delivered_occurrence(self, item_id: int) -> datetime | None:
        row = self._execute(
            "SELECT MAX(occurrence) AS occurrence FROM deliveries"
            " WHERE item_id = %s AND delivered_at IS NOT NULL",
            (item_id,),
        ).fetchone()
        return self._from_db(row["occurrence"]) if row else None

    # --- captures awaiting confirmation (#117) ----------------------------------------------

    def _capture(self, row: dict) -> Capture:
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
        with self._transaction():
            row = self._execute(
                "INSERT INTO captures (status, actions, source, address, source_text, interpreter,"
                " doubts, dedupe_key, created_at, expires_at, undo_until, decided_at, decided_via)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (source, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"
                " RETURNING id",
                (str(capture.status), capture.actions, capture.source, capture.address,
                 capture.source_text, capture.interpreter, capture.doubts, dedupe_key,
                 self._to_db(capture.created_at), self._to_db(capture.expires_at),
                 # A split capture arrives already applied, with its undo window running (#126);
                 # a capture awaiting confirmation leaves all three NULL, as it always has.
                 self._to_db(capture.undo_until), self._to_db(capture.decided_at),
                 capture.decided_via),
            ).fetchone()
            if row is None:  # the conflict path: this provider message was already proposed
                existing = self._execute(
                    "SELECT * FROM captures WHERE source = %s AND dedupe_key = %s",
                    (capture.source, dedupe_key),
                ).fetchone()
                assert existing is not None  # the conflicting row is there by definition
                return self._capture(existing), False
        capture.id = int(row["id"])
        return capture, True

    def get_capture(self, capture_id: int) -> Capture | None:
        row = self._execute("SELECT * FROM captures WHERE id = %s", (capture_id,)).fetchone()
        return self._capture(row) if row else None

    def confirm_capture(self, capture_id: int, via: str, now: datetime, undo_until: datetime) -> bool:
        with self._transaction():
            cur = self._execute(
                "UPDATE captures SET status = %s, decided_at = %s, decided_via = %s, undo_until = %s"
                " WHERE id = %s AND status = %s AND expires_at > %s",
                (str(CaptureStatus.APPLIED), self._to_db(now), via, self._to_db(undo_until),
                 capture_id, str(CaptureStatus.PENDING), self._to_db(now)),
            )
        return cur.rowcount > 0

    def cancel_capture(self, capture_id: int, via: str, now: datetime) -> bool:
        with self._transaction():
            cur = self._execute(
                "UPDATE captures SET status = %s, decided_at = %s, decided_via = %s"
                " WHERE id = %s AND status = %s",
                (str(CaptureStatus.CANCELLED), self._to_db(now), via, capture_id,
                 str(CaptureStatus.PENDING)),
            )
        return cur.rowcount > 0

    def undo_capture(self, capture_id: int, via: str, now: datetime) -> bool:
        with self._transaction():
            cur = self._execute(
                "UPDATE captures SET status = %s, decided_at = %s, decided_via = %s"
                " WHERE id = %s AND status = %s AND undo_until > %s",
                (str(CaptureStatus.UNDONE), self._to_db(now), via, capture_id,
                 str(CaptureStatus.APPLIED), self._to_db(now)),
            )
        return cur.rowcount > 0

    def expire_captures(self, now: datetime) -> int:
        with self._transaction():
            cur = self._execute(
                "UPDATE captures SET status = %s, decided_at = %s WHERE status = %s AND expires_at <= %s",
                (str(CaptureStatus.EXPIRED), self._to_db(now), str(CaptureStatus.PENDING),
                 self._to_db(now)),
            )
        return cur.rowcount

    def captures(self, status: str | None = None, source: str | None = None,
                 address: str | None = None) -> builtins.list[Capture]:
        sql: str = "SELECT * FROM captures"
        params: tuple[object, ...] = ()
        clauses = [("status = %s", status), ("source = %s", source), ("address = %s", address)]
        where = [(c, v) for c, v in clauses if v is not None]
        if where:
            sql += " WHERE " + " AND ".join(c for c, _ in where)
            params = tuple(v for _, v in where)
        sql += " ORDER BY created_at DESC, id DESC"
        return [self._capture(r) for r in self._execute(sql, params)]

    def link_capture(self, capture_id: int, item_ids: builtins.list[int]) -> None:
        with self._transaction():
            for item_id in item_ids:
                self._execute("UPDATE items SET capture_id = %s WHERE id = %s", (capture_id, item_id))

    def items_for_capture(self, capture_id: int) -> builtins.list[Item]:
        rows = self._execute("SELECT * FROM items WHERE capture_id = %s ORDER BY id", (capture_id,))
        return [self._item(r) for r in rows]

    # --- readings that need a look (#126) ---------------------------------------------------

    def needs_look(self, source: str | None = None, address: str | None = None,
                   since: datetime | None = None) -> builtins.list[Item]:
        # LEFT JOIN, so a gapped item whose capture row is gone is still listed rather than hidden.
        sql = ("SELECT i.* FROM items i LEFT JOIN captures c ON c.id = i.capture_id"
               " WHERE i.gap IS NOT NULL AND i.status != %s")
        params: builtins.list[object] = [str(Status.DONE)]
        for clause, value in (("c.source = %s", source), ("c.address = %s", address),
                              ("i.created_at >= %s", self._to_db(since) if since else None)):
            if value is not None:
                sql += f" AND {clause}"
                params.append(value)
        sql += " ORDER BY i.created_at DESC, i.id DESC"
        return [self._item(r) for r in self._execute(sql, tuple(params))]

    def fill_gap(self, item_id: int, due_at: datetime | None = None, recurrence: str | None = None,
                 channels: builtins.list[str] | None = None, text: str | None = None,
                 kind: Kind | None = None) -> bool:
        # A dict, so `kind` given alongside a `due_at` replaces what the time implied: PostgreSQL
        # refuses two assignments to the same column in one UPDATE.
        sets: dict[str, object] = {"gap": None}
        if due_at is not None:
            sets |= {"due_at": self._to_db(due_at), "status": str(Status.PENDING),
                     "kind": str(Kind.REMINDER), "recurrence": recurrence}
        if kind is not None:
            sets["kind"] = str(kind)
        if channels is not None:
            sets["channels"] = ",".join(channels) or None
        if text is not None:
            sets["text"] = text
        # The column names are this method's own literals; every value is bound.
        assignments = ", ".join(f"{name} = %s" for name in sets)
        with self._transaction():
            # `gap IS NOT NULL` is the check-and-set: two answers racing, one fills it.
            cur = self._execute(
                f"UPDATE items SET {assignments} WHERE id = %s AND gap IS NOT NULL",
                (*sets.values(), item_id),
            )
        return cur.rowcount > 0

    def last_delivered(self, channel: str) -> int | None:
        row = self._execute(
            "SELECT d.item_id FROM deliveries d JOIN items i ON i.id = d.item_id"
            # Everything after the first colon, as SQLite's substr/instr does: split_part would
            # stop at a second colon and the two backends would disagree.
            " WHERE (d.channel = %s OR (d.channel LIKE 'escalation%%' AND"
            "        substring(d.channel from position(':' in d.channel) + 1) = %s))"
            " AND d.delivered_at IS NOT NULL ORDER BY d.delivered_at DESC LIMIT 1",
            (channel, channel),  # escalation stages on the same channel ("escalation2:<channel>") count too
        ).fetchone()
        return int(row["item_id"]) if row else None

    def _recent_delivery(self, item_id: int, now: datetime | None) -> datetime | None:
        """The occurrence of the item's latest delivery recent enough to react to."""
        now = now or datetime.now(self.tz)
        row = self._execute(
            "SELECT occurrence FROM deliveries WHERE item_id = %s AND delivered_at IS NOT NULL"
            " AND delivered_at >= %s ORDER BY occurrence DESC LIMIT 1",
            (item_id, self._to_db(logic.ack_cutoff(now))),
        ).fetchone()
        return row["occurrence"] if row else None

    def _acknowledge(self, item_id: int, via: str, now: datetime | None,
                     occurrence: datetime | None = None) -> bool:
        """Acknowledge the item's latest delivery if it was recent (inside a transaction); how recent
        counts is `logic.ack_cutoff`."""
        # A reply that names its delivery acknowledges exactly that one, however late it comes.
        stored = self._to_db(occurrence) if occurrence else self._recent_delivery(item_id, now)
        if stored is None:
            return False  # nothing sent recently: nothing to acknowledge
        cur = self._execute(
            "INSERT INTO acknowledgements (item_id, occurrence, acknowledged_at, via)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (item_id, occurrence) DO NOTHING",
            (item_id, stored, self._to_db(now or datetime.now(self.tz)), via),
        )
        return cur.rowcount > 0

    def acknowledgement(self, item_id: int, occurrence: datetime) -> Acknowledgement | None:
        row = self._execute(
            "SELECT acknowledged_at, via FROM acknowledgements WHERE item_id = %s AND occurrence = %s",
            (item_id, self._to_db(occurrence)),
        ).fetchone()
        return Acknowledgement(self._required_from_db(row["acknowledged_at"]), row["via"]) if row else None

    _UNACKNOWLEDGED = """
        FROM items i
        JOIN (SELECT item_id, MAX(occurrence) AS occurrence FROM deliveries
              WHERE delivered_at IS NOT NULL GROUP BY item_id) d ON d.item_id = i.id
        LEFT JOIN acknowledgements a ON a.item_id = i.id AND a.occurrence = d.occurrence
        WHERE a.item_id IS NULL AND i.status != %s"""

    def unacknowledged(self, since: datetime | None = None) -> builtins.list[tuple[Item, datetime]]:
        recent = " AND d.occurrence >= %s" if since else ""
        params: tuple[object, ...] = ((str(Status.DONE), self._to_db(since)) if since
                                      else (str(Status.DONE),))
        rows = self._execute(
            f"SELECT i.*, d.occurrence AS delivered_occurrence {self._UNACKNOWLEDGED}{recent}"
            " ORDER BY d.occurrence, i.id",
            params,
        )
        return [(self._item(r), self._required_from_db(r["delivered_occurrence"])) for r in rows]

    def unacknowledged_ids(self) -> frozenset[int]:
        rows = self._execute(f"SELECT i.id {self._UNACKNOWLEDGED}", (str(Status.DONE),))
        return frozenset(r["id"] for r in rows)

    # --- the scheduler lease (ADR 0007) -----------------------------------------------------------

    def lease_holder(self, now: datetime) -> str | None:
        row = self._execute(
            "SELECT owner FROM scheduler_lease WHERE id = 1 AND expires_at > %s", (self._to_db(now),)
        ).fetchone()
        return str(row["owner"]) if row else None

    def acquire_lease(self, owner: str, now: datetime, expires_at: datetime) -> tuple[bool, str]:
        with self._transaction():
            # Held for the whole check-and-set, so two schedulers can't both see the lease as free.
            self._lock(_LEASE_LOCK)
            row = self._execute(
                "SELECT owner, expires_at FROM scheduler_lease WHERE id = 1"
            ).fetchone()
            if (row is not None and row["owner"] != owner
                    and row["expires_at"] > self._to_db(now)):
                return False, str(row["owner"])
            self._execute(
                "INSERT INTO scheduler_lease (id, owner, expires_at) VALUES (1, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET owner = excluded.owner,"
                " expires_at = excluded.expires_at",
                (owner, self._to_db(expires_at)),
            )
        return True, owner

    def release_lease(self, owner: str) -> None:
        with self._transaction():
            self._execute("DELETE FROM scheduler_lease WHERE id = 1 AND owner = %s", (owner,))
