"""Running tests against a real PostgreSQL server.

Set `MAESTRO_TEST_POSTGRES_URL` to a database Maestro may own completely; everything in it is
truncated between tests. CI starts a `postgres:16` service for this, and CONTRIBUTING.md says how
to run one locally. Without the variable, every PostgreSQL test skips.

This is the one place tests touch a database driver: keeping it here is what lets
`test_storage_conformance.py` stay backend-independent.
"""

import os

import pytest

URL = os.environ.get("MAESTRO_TEST_POSTGRES_URL")

needs_postgres = pytest.mark.skipif(
    not URL, reason="set MAESTRO_TEST_POSTGRES_URL to a disposable database to run these"
)

TABLES = "items, deliveries, acknowledgements, inbound_messages, scheduler_lease"


def truncate(store):
    """Empty every table, as a fresh in-memory SQLite database would be. The schema and its
    version are left alone: migrations ran when the store opened."""
    store.conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
    return store


def drop_everything(store):
    """Put the database back to empty, schema included, so the next open migrates from scratch."""
    store.conn.execute(f"DROP TABLE IF EXISTS {TABLES}, schema_version")
    return store
