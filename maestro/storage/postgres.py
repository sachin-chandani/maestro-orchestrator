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

from maestro.models import Acknowledgement, Delivery, Item, Kind, Status
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
        )

    _INSERT_ITEM = (
        "INSERT INTO items (text, kind, status, due_at, created_at, source_text, recurrence)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id"
    )

    def _insert(self, item: Item) -> int:
        row = self._execute(
            self._INSERT_ITEM,
            (item.text, str(item.kind), str(item.status), self._to_db(item.due_at),
             self._to_db(item.created_at), item.source_text, item.recurrence),
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
        # Reminders by when they next go out first, then notes by creation time. The id
        # breaks ties (times are stored to the second), so the order is the same on every
        # backend and a list never reshuffles after an unrelated update.
        sql += " ORDER BY due_at IS NULL, COALESCE(snoozed_until, due_at), created_at, id"
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
        rows = self._execute(
            "SELECT * FROM items WHERE status = %s AND (due_at <= %s OR snoozed_until <= %s)"
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
