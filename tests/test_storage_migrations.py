"""The migration sequence, without a database.

`maestro.storage.migrations.apply_pending` is what both backends use to get from the version a
database is at to the version this Maestro expects. SQLite's own migrations are tested against
real old databases in `tests/test_channels.py` and `tests/test_acknowledgements.py`.
"""

import pytest

from maestro.storage import SQLiteStore, migrations
from maestro.storage.base import SchemaTooNewError


class FakeDatabase:
    """A database that remembers its version and what was applied to it."""

    def __init__(self, version=0):
        self.version = version
        self.applied = []
        self.history = []  # every version ever written, in order

    def read_version(self):
        return self.version

    def write_version(self, number):
        self.version = number
        self.history.append(number)

    def apply_one(self, migration):
        if isinstance(migration, Exception):
            raise migration
        self.applied.append(migration)

    def run(self, *migrations_to_run):
        return migrations.apply_pending(list(migrations_to_run), self.read_version,
                                        self.write_version, self.apply_one)


def test_a_fresh_database_gets_every_migration_in_order():
    db = FakeDatabase()
    assert db.run("one", "two", "three") == 3
    assert db.applied == ["one", "two", "three"]
    assert db.history == [1, 2, 3]


def test_an_existing_database_gets_only_what_it_is_missing():
    db = FakeDatabase(version=2)
    assert db.run("one", "two", "three") == 3
    assert db.applied == ["three"]
    assert db.history == [3]


def test_an_up_to_date_database_is_left_alone():
    db = FakeDatabase(version=3)
    assert db.run("one", "two", "three") == 3
    assert db.applied == [] and db.history == []


def test_a_database_from_a_newer_maestro_is_refused():
    db = FakeDatabase(version=9)
    with pytest.raises(SchemaTooNewError, match="schema 9 is newer than this Maestro supports"):
        db.run("one", "two")
    assert db.applied == []


def test_a_failing_migration_stops_the_run_before_its_version_is_recorded():
    """The caller holds the transaction, so nothing here is committed; what matters is that the
    version is never written for a migration that raised, and later ones don't run."""
    db = FakeDatabase()
    boom = RuntimeError("disk I/O error")
    with pytest.raises(RuntimeError, match="disk I/O error"):
        db.run("one", boom, "three")
    assert db.applied == ["one"]
    assert db.history == [1]


def test_the_version_is_recorded_after_each_step_not_only_at_the_end():
    """An interrupted upgrade must be resumable from where it stopped, not restarted."""
    seen = []
    db = FakeDatabase()
    db.apply_one = lambda migration: seen.append((migration, db.version))
    db.run("one", "two", "three")
    assert seen == [("one", 0), ("two", 1), ("three", 2)]


def test_the_sqlite_backend_reports_the_version_it_migrated_to():
    from maestro.storage.sqlite import MIGRATIONS

    with SQLiteStore() as store:
        assert store.schema_version == len(MIGRATIONS)
