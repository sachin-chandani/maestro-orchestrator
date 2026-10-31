"""Both backends must mean the same thing by "a write".

The conformance suite covers what a write does when it succeeds. These tests cover what it does
when it doesn't: the failure is injected into the driver, so each backend needs its own version,
and the two are written side by side so a difference is obvious.

| Property                                              | SQLite | PostgreSQL |
|-------------------------------------------------------|--------|------------|
| A multi-statement write rolls back whole               | here   | here       |
| An interrupted write is never half-visible             | here   | here       |
| A failed statement doesn't wedge later writes          | here, and tests/test_lease.py | here |
| Another connection can't see an uncommitted write      | here   | here       |
| An acknowledgement metric counts only committed work   | tests/test_acknowledgements.py | same code path |
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pgsupport
import pytest
from conftest import TZ, dt

from maestro.models import Delivery, Item, Kind
from maestro.storage import SQLiteStore, open_store

NOW = dt(2026, 10, 26, 9, 0)


@pytest.fixture
def sqlite_store(tmp_path):
    """On disk, so a second connection can look at it from outside the transaction."""
    with SQLiteStore(tmp_path / "m.db", tz=TZ) as store:
        yield store


@pytest.fixture
def postgres_store():
    if not pgsupport.URL:
        pytest.skip("set MAESTRO_TEST_POSTGRES_URL to a disposable database to run these")
    with open_store(pgsupport.URL, tz=TZ) as store:
        yield pgsupport.truncate(store)


def reminder(store):
    return store.add(Item(text="Take pills", kind=Kind.REMINDER, created_at=NOW, due_at=NOW))


def with_a_delivery(store):
    """An item with everything `delete` has to clean up: a delivery and an acknowledgement."""
    item = reminder(store)
    store.record_delivery(item.id, NOW, Delivery(channel="console", attempts=1, delivered_at=NOW))
    store.complete(item.id, via="cli", now=NOW)
    return item


def assert_delete_rolled_back(store, item):
    assert store.get(item.id) is not None
    assert set(store.deliveries(item.id, NOW)) == {"console"}
    assert store.acknowledgement(item.id, NOW).via == "cli"


# --- a multi-statement write rolls back whole ---------------------------------------------------
# `delete` removes acknowledgements, deliveries, inbound claims and then the item. If the last
# statement fails, the first three must come back.


class FailingOn:
    """A connection that raises on one statement, everything else passed through.
    `sqlite3.Connection.execute` can't be replaced, so the connection itself is wrapped."""

    def __init__(self, inner, fragment, error):
        self.inner, self.fragment, self.error = inner, fragment, error

    def execute(self, sql, *args, **kwargs):
        if self.fragment in sql:
            raise self.error
        return self.inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_sqlite_rolls_back_a_delete_that_fails_part_way(sqlite_store):
    import sqlite3

    item = with_a_delivery(sqlite_store)
    sqlite_store.conn = FailingOn(sqlite_store.conn, "DELETE FROM items",
                                  sqlite3.OperationalError("disk I/O error"))
    with pytest.raises(sqlite3.OperationalError):
        sqlite_store.delete(item.id)
    sqlite_store.conn = sqlite_store.conn.inner
    assert_delete_rolled_back(sqlite_store, item)


def test_postgres_rolls_back_a_delete_that_fails_part_way(postgres_store):
    import psycopg

    item = with_a_delivery(postgres_store)
    real_execute = postgres_store._execute

    def fail_on_deleting_the_item(sql, params=None):
        if "DELETE FROM items" in sql:
            raise psycopg.OperationalError("connection lost mid-delete")
        return real_execute(sql, params)

    postgres_store._execute = fail_on_deleting_the_item
    with pytest.raises(psycopg.OperationalError):
        postgres_store.delete(item.id)
    postgres_store._execute = real_execute
    assert_delete_rolled_back(postgres_store, item)


# --- a failed statement doesn't wedge later writes ------------------------------------------------


def test_sqlite_keeps_working_after_a_failed_statement(sqlite_store):
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        sqlite_store.conn.execute("SELECT * FROM no_such_table")
    item = reminder(sqlite_store)
    assert sqlite_store.complete(item.id, via="cli", now=NOW) is True


def test_postgres_keeps_working_after_a_failed_statement(postgres_store):
    import psycopg

    with pytest.raises(psycopg.errors.UndefinedTable):
        postgres_store.conn.execute("SELECT * FROM no_such_table")
    item = reminder(postgres_store)
    assert postgres_store.complete(item.id, via="cli", now=NOW) is True


# --- another connection can't see an uncommitted write ---------------------------------------------


def test_sqlite_hides_an_uncommitted_write_from_another_connection(sqlite_store, tmp_path):
    class Crash(Exception):
        pass

    with SQLiteStore(tmp_path / "m.db", tz=TZ) as onlooker:
        with pytest.raises(Crash):
            with sqlite_store._transaction():
                sqlite_store.conn.execute(
                    "INSERT INTO items (text, kind, status, created_at)"
                    " VALUES ('ghost', 'note', 'pending', '2026-10-26T13:00:00+00:00')")
                assert onlooker.search("ghost") == []  # not visible while in flight
                raise Crash
        assert onlooker.search("ghost") == []  # and never visible, because it rolled back
    assert sqlite_store.search("ghost") == []


def test_postgres_hides_an_uncommitted_write_from_another_connection(postgres_store):
    class Crash(Exception):
        pass

    with open_store(pgsupport.URL, tz=TZ) as onlooker:
        with pytest.raises(Crash):
            with postgres_store._transaction():
                postgres_store.conn.execute(
                    "INSERT INTO items (text, kind, status, created_at)"
                    " VALUES ('ghost', 'note', 'pending', '2026-10-26T13:00:00+00:00')")
                assert onlooker.search("ghost") == []
                raise Crash
        assert onlooker.search("ghost") == []
    assert postgres_store.search("ghost") == []


# --- a committed write is visible to another connection at once --------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_a_committed_write_is_visible_to_another_connection(backend, request, tmp_path):
    store = request.getfixturevalue(f"{backend}_store")
    second = (SQLiteStore(tmp_path / "m.db", tz=TZ) if backend == "sqlite"
              else open_store(pgsupport.URL, tz=TZ))
    with second:
        item = reminder(store)
        assert second.get(item.id) is not None
        store.complete(item.id, via="cli", now=NOW)
        assert second.get(item.id).status == "done"


def test_the_clock_used_for_storage_is_the_same_on_both_backends():
    """Both store UTC to the second, so an occurrence written by one reads back the same from the
    other. (The value, not the column type: see tests/test_storage_postgres.py for that.)"""
    awkward = datetime(2026, 11, 1, 1, 30, 45, 654321, tzinfo=ZoneInfo("America/New_York"))
    with SQLiteStore(":memory:", tz=TZ) as store:
        sqlite_value = store.get(store.add(Item(text="x", kind=Kind.REMINDER,
                                                created_at=NOW, due_at=awkward)).id).due_at
    if not pgsupport.URL:
        pytest.skip("needs PostgreSQL to compare against")
    with open_store(pgsupport.URL, tz=TZ) as store:
        pgsupport.truncate(store)
        postgres_value = store.get(store.add(Item(text="x", kind=Kind.REMINDER,
                                                  created_at=NOW, due_at=awkward)).id).due_at
    assert sqlite_value == postgres_value == awkward.replace(microsecond=0)
