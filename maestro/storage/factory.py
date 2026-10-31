"""Choosing a backend from a database URL.

One setting says where data lives. It is a filesystem path by default, because that is what almost
every install uses and what Maestro has always accepted:

    ~/.maestro/maestro.db            a SQLite file (MAESTRO_DB, --db)
    :memory:                         a throwaway SQLite database
    sqlite:///data/maestro.db        the same file, said as a URL
    postgresql://user:pw@host/db     PostgreSQL (#34), needs maestro-orchestrator[postgres]

A backend is imported only when its URL asks for it, so a SQLite install never needs psycopg.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.storage.base import Store

SQLITE = "sqlite"
POSTGRESQL = "postgresql"
# Both spellings of the PostgreSQL scheme are in the wild (psql prints one, libpq accepts both).
SCHEMES = {SQLITE: SQLITE, POSTGRESQL: POSTGRESQL, "postgres": POSTGRESQL}
# libpq also takes the password as a query parameter, so that spelling is masked too.
_PASSWORD_PARAMETER = re.compile(r"(?i)\b(password=)[^&]*")


class StoreConfigError(ValueError):
    """The database URL names a backend Maestro can't open."""


@dataclass(frozen=True)
class Target:
    """What a database URL resolves to: which backend, and what to hand it."""

    backend: str
    locator: str  # a filesystem path for SQLite, the URL itself for PostgreSQL

    @property
    def display(self) -> str:
        """The target as it can safely be shown: a URL's password is never printed or logged."""
        return self.locator if self.backend == SQLITE else redact(self.locator)


def redact(url: str) -> str:
    """Replace the password in a URL with `***`, leaving everything else readable.

    Plain string work on purpose: this runs on whatever someone put in the setting, including
    malformed URLs that a parser would reject, and it must never raise or leak on the way to an
    error message.
    """
    scheme, separator, rest = url.partition("://")
    netloc, slash, path = rest.partition("/")
    rest = _PASSWORD_PARAMETER.sub(r"\1***", f"{slash}{path}")
    if not separator or "@" not in netloc:
        return f"{scheme}{separator}{netloc}{rest}"
    credentials, _, host = netloc.rpartition("@")
    if ":" not in credentials:
        return url  # a username with no password
    user = credentials.partition(":")[0]
    return f"{scheme}://{f'{user}:***' if user else '***'}@{host}{rest}"


def parse_url(url: str | Path) -> Target:
    """Work out which backend `url` names. Paths and URLs both go through here."""
    text = str(url)
    scheme, separator, rest = text.partition(":")
    backend = SCHEMES.get(scheme.lower()) if separator else None
    if backend is None:
        if separator and rest.startswith("//") and scheme.isalpha():
            raise StoreConfigError(
                f"Unsupported database URL {redact(text)!r}: Maestro supports "
                f"{', '.join(sorted(set(SCHEMES.values())))}"
            )
        return Target(SQLITE, text)  # a plain path, including ":memory:" and ""
    if backend == SQLITE:
        # Everything after the scheme is the path, with an optional "//" in front of it. That makes
        # sqlite:///data/x.db and sqlite:////data/x.db (SQLAlchemy's spelling) the same file, and
        # sqlite:x.db relative, so no URL quietly points somewhere else than it reads.
        return Target(SQLITE, rest[2:] if rest.startswith("//") else rest)
    return Target(backend, text)


def driver_errors(backend: str = SQLITE) -> tuple[type[BaseException], ...]:
    """What `backend` raises when the database is unreachable or refuses a statement.

    Callers that report a problem instead of showing a traceback catch these. A backend that was
    never asked for is never imported, so a SQLite install doesn't load psycopg to find out.
    """
    errors: list[type[BaseException]] = [sqlite3.Error]
    if backend == POSTGRESQL:
        try:
            postgres = importlib.import_module("maestro.storage.postgres")
        except ImportError:
            pass  # nothing to catch: opening it will fail with StoreConfigError instead
        else:
            errors.append(postgres.DriverError)
    return tuple(errors)


def open_store(url: str | Path = ":memory:", tz: ZoneInfo | None = None) -> Store:
    """Open the database `url` names, migrating it if needed."""
    target = parse_url(url)
    if target.backend == SQLITE:
        from maestro.storage.sqlite import SQLiteStore

        return SQLiteStore(target.locator, tz=tz)
    return _postgres(target, tz)


def _postgres(target: Target, tz: ZoneInfo | None) -> Store:
    try:
        module = importlib.import_module("maestro.storage.postgres")
    except ModuleNotFoundError as e:
        raise StoreConfigError(_missing_postgres(e)) from e
    store: Store = module.PostgresStore(target.locator, tz=tz)
    return store


def _missing_postgres(e: ModuleNotFoundError) -> str:
    if e.name == "maestro.storage.postgres":
        return ("This Maestro has no PostgreSQL backend yet (tracked in #34); "
                "use a SQLite path or sqlite:// URL")
    return (f"PostgreSQL needs the {e.name} driver: "
            "pip install 'maestro-orchestrator[postgres]'")
