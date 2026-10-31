"""PostgreSQL against a real server: connecting, migrating, storing, failing and restarting.

What a backend must *do* is in `test_storage_conformance.py`, which runs against both backends.
What is here is everything that only means something with a server on the other end: a database
that outlives the process, transactions that must roll back whole, locks between connections, and
a server that goes away mid-sentence.

These skip unless `MAESTRO_TEST_POSTGRES_URL` is set; see `pgsupport`.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pgsupport
import psycopg
import pytest
from conftest import TZ, dt

from maestro.models import Delivery, Item, Kind, Status
from maestro.observability import METRICS
from maestro.storage import SchemaTooNewError, SQLiteStore, open_store
from maestro.storage.postgres import MIGRATIONS, PostgresStore

pytestmark = pgsupport.needs_postgres

NOW = dt(2026, 10, 26, 9, 0)
LATER = dt(2026, 10, 26, 17, 0)


@pytest.fixture
def fresh():
    """An empty database, as a new install would find it: no tables at all."""
    with open_store(pgsupport.URL, tz=TZ) as store:
        pgsupport.drop_everything(store)
    yield
    with open_store(pgsupport.URL, tz=TZ) as store:  # leave it migrated for the next test
        pgsupport.truncate(store)


@pytest.fixture
def store():
    with open_store(pgsupport.URL, tz=TZ) as store:
        yield pgsupport.truncate(store)


def reminder(store, text="Take pills", due=NOW, recurrence=None):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=NOW, due_at=due,
                          recurrence=recurrence, source_text=text))


# --- connecting -------------------------------------------------------------------------------


def test_a_postgres_url_opens_a_postgres_store(store):
    assert isinstance(store, PostgresStore)
    assert store.tz == TZ and store.closed is False


def test_an_unreachable_server_says_so_instead_of_hanging():
    with pytest.raises(psycopg.OperationalError):
        open_store("postgresql://maestro@127.0.0.1:1/maestro")


def test_a_database_that_does_not_exist_is_reported(monkeypatch):
    with pytest.raises(psycopg.OperationalError, match='"no_such_database"'):
        open_store(pgsupport.URL.replace("/maestro_test", "/no_such_database"))


def test_a_failed_connection_leaves_nothing_open():
    """__init__ closes its own connection when migrating fails, because the caller never gets the
    object and so can never close it."""
    broken = []

    class FailsToMigrate(PostgresStore):
        def _migrate(self):
            broken.append(self)
            raise psycopg.OperationalError("server went away")

    with pytest.raises(psycopg.OperationalError):
        FailsToMigrate(pgsupport.URL, tz=TZ)
    assert [s.closed for s in broken] == [True]


# --- migrating --------------------------------------------------------------------------------


def test_an_empty_database_is_migrated_on_first_open(fresh):
    with open_store(pgsupport.URL, tz=TZ) as store:
        assert store.schema_version == len(MIGRATIONS)
        assert store.list() == []  # every table the store reads from exists
        assert store.counts() == {} and store.unacknowledged() == []
        assert store.lease_holder(NOW) is None


def test_opening_an_up_to_date_database_changes_nothing(fresh):
    with open_store(pgsupport.URL, tz=TZ) as store:
        reminder(store)
    with open_store(pgsupport.URL, tz=TZ) as store:
        assert store.schema_version == len(MIGRATIONS)
        assert [i.text for i in store.list()] == ["Take pills"]


def test_processes_starting_together_migrate_a_fresh_database_once(fresh):
    """`maestro serve` and `maestro add` racing on a new database, each with its own connection."""
    def open_and_report(_):
        with open_store(pgsupport.URL, tz=TZ) as store:
            return store.schema_version

    with ThreadPoolExecutor(8) as pool:
        versions = list(pool.map(open_and_report, range(8)))
    assert versions == [len(MIGRATIONS)] * 8
    with open_store(pgsupport.URL, tz=TZ) as store:
        rows = store.conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
        assert rows["n"] == 1


def test_a_database_from_a_newer_maestro_is_refused(store):
    store.conn.execute("UPDATE schema_version SET version = 99")
    with pytest.raises(SchemaTooNewError, match="schema 99 is newer"):
        open_store(pgsupport.URL, tz=TZ)
    store.conn.execute("UPDATE schema_version SET version = %s", (len(MIGRATIONS),))


def test_both_backends_describe_the_same_schema(store):
    """A column added to one backend and forgotten in the other would pass every behaviour test
    until the feature using it shipped. Names only: the types are deliberately different."""
    postgres = {}
    for row in store.conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name <> 'schema_version'"
    ):
        postgres.setdefault(row["table_name"], set()).add(row["column_name"])

    sqlite = {}
    with SQLiteStore(":memory:", tz=TZ) as other:
        tables = other.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in tables:
            columns = other.conn.execute(f"PRAGMA table_info({name})").fetchall()
            sqlite[name] = {c["name"] for c in columns}

    assert postgres == sqlite


def test_both_backends_have_the_same_indexes(store):
    """An index added to one backend and forgotten in the other is invisible until someone's
    database is big enough to feel it. Only the ones Maestro names: primary keys are implicit and
    spelled differently by each backend."""
    postgres = {row["indexname"] for row in store.conn.execute(
        r"SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname LIKE 'idx\_%'")}
    with SQLiteStore(":memory:", tz=TZ) as other:
        sqlite = {row["name"] for row in other.conn.execute(
            r"SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx\_%' ESCAPE '\'")}
    assert postgres == sqlite != set()


# --- storing ----------------------------------------------------------------------------------


def test_times_are_stored_as_utc_timestamps(store):
    item = reminder(store, due=dt(2026, 11, 1, 1, 30))  # inside the US DST fold
    row = store.conn.execute(
        "SELECT pg_typeof(due_at)::text AS type, due_at AT TIME ZONE 'UTC' AS utc"
        " FROM items WHERE id = %s", (item.id,)).fetchone()
    assert row["type"] == "timestamp with time zone"
    assert row["utc"] == datetime(2026, 11, 1, 5, 30)  # EDT, before the clocks went back
    assert store.get(item.id).due_at == dt(2026, 11, 1, 1, 30)


def test_times_are_stored_to_the_second_so_occurrences_match(store):
    """An occurrence is part of a primary key. SQLite stores seconds; PostgreSQL would keep
    microseconds, and a delivery recorded at …:00.123456 would never match the item's due time."""
    item = reminder(store, due=NOW.replace(microsecond=123456))
    occurrence = store.get(item.id).due_at
    assert occurrence == NOW
    store.record_delivery(item.id, NOW.replace(microsecond=654321),
                          Delivery(channel="console", attempts=1, delivered_at=NOW))
    assert set(store.deliveries(item.id, NOW)) == {"console"}
    assert store.latest_delivered_occurrence(item.id) == NOW


def test_ids_come_from_the_sequence_and_keep_counting(store):
    first, second = reminder(store, text="First"), reminder(store, text="Second")
    assert second.id == first.id + 1
    store.delete(second.id)
    assert reminder(store, text="Third").id == second.id + 1  # never reused


def test_text_with_awkward_characters_survives(store):
    text = "Zahnarzt um 17 Uhr – don't forget the 100% ✅ \\ \" ' ; DROP TABLE items;--"
    item = store.add(Item(text=text, kind=Kind.NOTE, created_at=NOW, source_text=text))
    assert store.get(item.id).text == text
    assert [i.id for i in store.search("DROP TABLE")] == [item.id]
    assert store.counts() == {(Kind.NOTE, Status.PENDING): 1}  # the table is still there


# --- transactions that fail --------------------------------------------------------------------


def test_a_write_that_fails_part_way_leaves_nothing_behind(store):
    """Delete touches four tables; if the last statement fails, the first three must come back."""
    item = reminder(store)
    store.record_delivery(item.id, NOW, Delivery(channel="console", attempts=1, delivered_at=NOW))
    store.complete(item.id, via="cli", now=NOW)

    real_execute = store.conn.execute

    def fail_on_deleting_the_item(sql, *args, **kwargs):
        if "DELETE FROM items" in sql:
            raise psycopg.OperationalError("connection lost mid-delete")
        return real_execute(sql, *args, **kwargs)

    store.conn.execute = fail_on_deleting_the_item
    with pytest.raises(psycopg.OperationalError):
        store.delete(item.id)
    store.conn.execute = real_execute

    assert store.get(item.id) is not None
    assert set(store.deliveries(item.id, NOW)) == {"console"}
    assert store.acknowledgement(item.id, NOW).via == "cli"


def test_an_interrupted_transaction_is_never_half_visible(store):
    """What a crash between two statements of one write looks like."""
    class Crash(Exception):
        pass

    with pytest.raises(Crash):
        with store.conn.transaction():
            store.conn.execute("INSERT INTO items (text, kind, status, created_at)"
                               " VALUES ('ghost', 'note', 'pending', now())")
            raise Crash
    assert store.search("ghost") == []


def test_a_failed_statement_does_not_wedge_the_connection(store):
    """PostgreSQL refuses every statement after an error until the transaction ends; the store must
    not leave one open, or every later write would fail too (the SQLite lesson, ADR 0003)."""
    with pytest.raises(psycopg.errors.UndefinedTable):
        store.conn.execute("SELECT * FROM no_such_table")
    item = reminder(store)
    assert store.get(item.id) is not None
    assert store.complete(item.id, via="cli", now=NOW) is True


def test_a_constraint_violation_is_raised_not_swallowed(store):
    item = reminder(store)
    store.record_delivery(item.id, NOW, Delivery(channel="console", attempts=1))
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.conn.execute(
            "INSERT INTO deliveries (item_id, occurrence, channel) VALUES (%s, %s, 'console')",
            (item.id, store.get(item.id).due_at))
    assert store.deliveries(item.id, NOW)["console"].attempts == 1


def test_a_server_that_disconnects_is_reported_not_silently_ignored(store):
    """A failover or a restarted server: the call in flight must raise, so the scheduler logs and
    retries on the next tick instead of believing a reminder was recorded."""
    disconnect(store)
    with pytest.raises(psycopg.OperationalError):
        reminder(store)


def disconnect(store):
    """Drop the store's connection from the server's side, as a restart or failover does."""
    with open_store(pgsupport.URL, tz=TZ) as killer:
        killer.conn.execute("SELECT pg_terminate_backend(%s)", (store.conn.info.backend_pid,))


def test_the_store_reconnects_for_the_next_call_after_the_server_comes_back(store, caplog):
    """Without this a `maestro serve` stops delivering for good after the database restarts: the
    scheduler loop survives, but every tick fails on the dead connection until someone notices."""
    before = reminder(store, text="Before the restart")
    disconnect(store)
    with pytest.raises(psycopg.OperationalError):
        store.list()  # the call that was in flight still fails

    with caplog.at_level(logging.WARNING):
        after = reminder(store, text="After the restart")  # the next one reconnects
    assert "reconnecting" in caplog.text.lower()
    assert [i.text for i in store.list()] == ["Before the restart", "After the restart"]
    assert store.get(before.id) is not None and store.get(after.id) is not None
    assert METRICS.get("maestro_store_reconnects_total") == 1


def test_a_reconnect_never_happens_in_the_middle_of_a_transaction(store):
    """Half a write on one connection and half on another would be worse than failing: the write
    must fail whole, and only the next call reconnects."""
    item = reminder(store)
    store.record_delivery(item.id, NOW, Delivery(channel="console", attempts=1, delivered_at=NOW))

    real_execute = store._execute

    def die_half_way(sql, params=None):
        if "DELETE FROM items" in sql:
            disconnect(store)
        return real_execute(sql, params)

    store._execute = die_half_way
    with pytest.raises(psycopg.OperationalError):
        store.delete(item.id)  # deletes acknowledgements and deliveries first, then the item
    store._execute = real_execute

    assert store.get(item.id) is not None  # the whole delete rolled back
    assert set(store.deliveries(item.id, NOW)) == {"console"}


def test_a_store_the_caller_closed_stays_closed(store):
    """Reconnecting must not resurrect a store someone deliberately closed."""
    store.close()
    with pytest.raises(psycopg.OperationalError):
        store.list()
    assert store.closed is True


# --- restarts ---------------------------------------------------------------------------------


def test_everything_acknowledged_to_the_user_survives_a_restart(store):
    item = reminder(store, due=NOW, recurrence="daily")
    note = store.add(Item(text="Gate code is 4321", kind=Kind.NOTE, created_at=NOW))
    store.record_delivery(item.id, NOW, Delivery(channel="telegram", attempts=2, delivered_at=NOW))
    store.snooze(item.id, LATER, via="telegram", now=NOW)
    store.add_once("sms", "SM1", Item(text="From a text", kind=Kind.NOTE, created_at=NOW))
    before = store.list()

    store.close()  # as a `docker stop` would
    with open_store(pgsupport.URL, tz=TZ) as restarted:
        assert restarted.list() == before
        assert restarted.get(item.id).snoozed_until == LATER
        assert restarted.get(item.id).due_at == dt(2026, 10, 27, 9, 0)
        assert restarted.get(note.id).text == "Gate code is 4321"
        assert restarted.deliveries(item.id, NOW)["telegram"].attempts == 2
        assert restarted.acknowledgement(item.id, NOW).via == "telegram"
        assert restarted.is_claimed("sms", "SM1") is True
        assert restarted.search("gate")[0].id == note.id


def test_the_scheduler_picks_up_where_it_left_off_after_a_restart(store):
    item = reminder(store, due=NOW, recurrence="daily")
    assert [i.id for i in store.due(NOW)] == [item.id]
    store.close()

    with open_store(pgsupport.URL, tz=TZ) as restarted:
        assert [i.id for i in restarted.due(NOW)] == [item.id]  # not yet delivered: still due
        restarted.finish_occurrence(item.id, NOW, next_due=dt(2026, 10, 27, 9, 0))
        assert restarted.due(NOW) == []


def test_a_lease_outlives_the_process_that_took_it(store):
    """A scheduler that dies without releasing holds the lease until it expires, and no longer."""
    store.acquire_lease("gone", NOW, NOW + timedelta(seconds=90))
    store.close()  # killed, never released

    with open_store(pgsupport.URL, tz=TZ) as standby:
        assert standby.lease_holder(NOW) == "gone"
        assert standby.acquire_lease("standby", NOW, NOW + timedelta(seconds=90)) == (False, "gone")
        expired = NOW + timedelta(seconds=91)
        assert standby.lease_holder(expired) is None
        assert standby.acquire_lease("standby", expired, expired + timedelta(seconds=90))[0] is True


# --- two processes on one database ---------------------------------------------------------------


def test_only_one_of_two_schedulers_gets_the_lease(store):
    with open_store(pgsupport.URL, tz=TZ) as other:
        assert store.acquire_lease("a", NOW, NOW + timedelta(minutes=1)) == (True, "a")
        assert other.acquire_lease("b", NOW, NOW + timedelta(minutes=1)) == (False, "a")
        assert other.lease_holder(NOW) == "a"
        store.release_lease("a")
        assert other.acquire_lease("b", NOW, NOW + timedelta(minutes=1)) == (True, "b")


def test_schedulers_racing_for_the_lease_produce_exactly_one_winner(store):
    """ADR 0007 on PostgreSQL: the check-and-set is atomic across connections, so no reminder can
    go out twice. Every contender connects first and then waits on a barrier, so they really do
    call acquire_lease at once, and the race is run repeatedly from a clean lease each round."""
    contenders = 8
    start = threading.Barrier(contenders)

    def try_to_acquire(owner):
        with open_store(pgsupport.URL, tz=TZ) as contender:
            start.wait(timeout=10)
            return contender.acquire_lease(f"s{owner}", NOW, NOW + timedelta(minutes=1))

    for _ in range(10):
        store.conn.execute("DELETE FROM scheduler_lease")
        start.reset()
        with ThreadPoolExecutor(contenders) as pool:
            results = list(pool.map(try_to_acquire, range(contenders)))
        assert sum(acquired for acquired, _ in results) == 1, "two schedulers both held the lease"
        holders = {holder for _, holder in results}
        assert len(holders) == 1 and store.lease_holder(NOW) in holders


def test_the_same_inbound_message_arriving_twice_at_once_captures_one_item(store):
    """Twilio retrying a webhook while the first request is still in flight."""
    def capture(_):
        with open_store(pgsupport.URL, tz=TZ) as receiver:
            item, created = receiver.add_once("sms", "SM-same",
                                              Item(text="Buy milk", kind=Kind.NOTE, created_at=NOW))
            return item.id, created

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(capture, range(8)))
    assert sum(created for _, created in results) == 1
    assert len({item_id for item_id, _ in results}) == 1
    assert len(store.list()) == 1
