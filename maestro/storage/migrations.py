"""Applying schema migrations: the part that is the same whatever the database is.

Each backend owns its migrations (SQLite DDL is not PostgreSQL DDL) and owns where the applied
version is kept — `PRAGMA user_version` in SQLite, a row in PostgreSQL. What they share is the
sequence: take the write lock, read the version, refuse a database from the future, then apply
what is missing and record each step.

The caller holds the transaction, so a half-applied migration rolls back with its version bump
(ENGINEERING.md: migrations are forward-only and run inside the migration transaction).
"""

from collections.abc import Callable, Sequence
from typing import TypeVar

from maestro.storage.base import SchemaTooNewError

Migration = TypeVar("Migration")


def apply_pending(
    migrations: Sequence[Migration],
    read_version: Callable[[], int],
    write_version: Callable[[int], None],
    apply_one: Callable[[Migration], None],
) -> int:
    """Bring a database up to date and return the version it is now at.

    Raises SchemaTooNewError if it was migrated by a newer Maestro: an older Maestro must not run
    against a newer schema, because it would ignore features such as the scheduler lease and, for
    example, deliver reminders twice.
    """
    version = read_version()
    if version > len(migrations):
        raise SchemaTooNewError(
            f"Database schema {version} is newer than this Maestro supports ({len(migrations)}); "
            "upgrade Maestro before using this database"
        )
    for number, migration in enumerate(migrations[version:], start=version + 1):
        apply_one(migration)
        write_version(number)
    return len(migrations)
