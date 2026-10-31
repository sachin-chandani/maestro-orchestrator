"""Telling an operator which database Maestro is using, and what went wrong with it.

A PostgreSQL install fails in ways a SQLite file never does — a server that moved, a password that
changed, a database that isn't there. Every one of those should come back as a sentence, name the
backend it was talking to, and never print a password.
"""

import pgsupport
import pytest
from conftest import TZ

from maestro import doctor
from maestro.cli import main
from maestro.config import Config
from maestro.observability import METRICS
from maestro.storage import SQLiteStore, open_store
from maestro.storage.sqlite import MIGRATIONS

UNREACHABLE = "postgresql://maestro:hunter2@127.0.0.1:1/maestro"  # refused at once, nothing on the network


def run_doctor(config):
    lines = []
    ok = doctor.run(config, out=lines.append)
    return ok, lines


# --- which database am I using? ----------------------------------------------------------------


def test_a_sqlite_store_describes_its_file(tmp_path):
    with SQLiteStore(tmp_path / "m.db", tz=TZ) as store:
        assert store.backend == "sqlite"
        assert store.describe() == str(tmp_path / "m.db")
    with SQLiteStore(":memory:", tz=TZ) as store:
        assert store.describe() == "in memory"


@pgsupport.needs_postgres
def test_a_postgres_store_describes_its_server_without_the_password():
    with open_store(pgsupport.URL, tz=TZ) as store:
        assert store.backend == "postgresql"
        described = store.describe()
    assert described.startswith("postgresql ")
    assert "hunter2" not in described and "password" not in described
    version = described.split()[1]
    assert version.replace(".", "").isdigit(), described  # e.g. "16.2"


def test_doctor_reports_a_sqlite_database_exactly_as_it_always_has(tmp_path):
    ok, lines = run_doctor(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t"))
    assert ok
    assert lines[1] == f"✓ database: {tmp_path / 'm.db'}, schema {len(MIGRATIONS)}, wal journal, 0 items"


@pgsupport.needs_postgres
def test_doctor_names_the_postgres_server_it_reached():
    with open_store(pgsupport.URL, tz=TZ) as store:
        pgsupport.truncate(store)
    ok, lines = run_doctor(Config(db_path=pgsupport.URL, channels=["console"], api_token="t"))
    assert ok, lines
    assert lines[1].startswith("✓ database: postgresql ")
    assert ", schema 1, 0 items" in lines[1]
    assert "journal" not in lines[1]  # that is a SQLite thing
    assert "hunter2" not in lines[1]


def test_doctor_reports_a_server_it_cannot_reach():
    ok, lines = run_doctor(Config(db_path=UNREACHABLE, channels=["console"], api_token="t"))
    assert not ok
    assert lines[1].startswith("✗ database: postgresql://maestro:***@127.0.0.1:1/maestro: ")
    assert "hunter2" not in lines[1]


# --- the CLI says what is wrong instead of printing a traceback -------------------------------------


def test_the_cli_reports_an_unreachable_database(capsys):
    assert main(["--db", UNREACHABLE, "list"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Database error: ")
    assert "Traceback" not in err and "hunter2" not in err


def test_the_cli_reports_a_database_it_cannot_create(capsys, tmp_path):
    unwritable = tmp_path / "file"
    unwritable.write_text("not a directory")
    assert main(["--db", str(unwritable / "m.db"), "list"]) == 2
    assert capsys.readouterr().err.startswith("Database error: ")


def test_the_cli_reports_a_database_from_a_newer_maestro(capsys, tmp_path):
    path = tmp_path / "m.db"
    with SQLiteStore(path, tz=TZ) as store:
        store.conn.execute("PRAGMA user_version = 99")
        store.conn.commit()
    assert main(["--db", str(path), "list"]) == 2
    err = capsys.readouterr().err
    assert "Database error: Database schema 99 is newer" in err
    assert "Traceback" not in err


def test_a_bug_still_raises_instead_of_being_called_a_database_error(tmp_path, monkeypatch):
    """Only database problems are reported this way; anything else must keep its traceback."""
    monkeypatch.setattr("maestro.cli._dispatch", _explode)
    with pytest.raises(ZeroDivisionError):
        main(["--db", str(tmp_path / "m.db"), "list"])


def _explode(*args, **kwargs):
    raise ZeroDivisionError("a bug, not a database problem")


# --- metrics and health ------------------------------------------------------------------------------


@pytest.fixture
def client_factory():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    def make(store):
        return TestClient(create_app(store, api_token="t0ken"))

    return make


def test_health_names_the_backend(client_factory):
    with SQLiteStore(":memory:", tz=TZ) as store, client_factory(store) as client:
        assert client.get("/health").json()["backend"] == "sqlite"


def test_metrics_report_the_backend_in_use(client_factory):
    with SQLiteStore(":memory:", tz=TZ) as store, client_factory(store) as client:
        body = client.get("/metrics", headers={"Authorization": "Bearer t0ken"}).text
    assert 'maestro_store_info{backend="sqlite"} 1' in body
    assert "# TYPE maestro_store_info gauge" in body
    # The backend is a label on its own metric, never added to the existing ones: a new label on
    # maestro_unacknowledged or maestro_items would break anyone's dashboards and alerts.
    labelled = [line for line in body.splitlines() if 'backend="sqlite"' in line]
    assert labelled == ['maestro_store_info{backend="sqlite"} 1']
    assert "maestro_unacknowledged 0" in body  # still unlabelled


@pgsupport.needs_postgres
def test_health_and_metrics_name_postgres(client_factory):
    with open_store(pgsupport.URL, tz=TZ) as store, client_factory(store) as client:
        assert client.get("/health").json()["backend"] == "postgresql"
        body = client.get("/metrics", headers={"Authorization": "Bearer t0ken"}).text
    assert 'maestro_store_info{backend="postgresql"} 1' in body


def test_the_reconnect_counter_is_published_even_before_it_fires(client_factory):
    with SQLiteStore(":memory:", tz=TZ) as store, client_factory(store) as client:
        body = client.get("/metrics", headers={"Authorization": "Bearer t0ken"}).text
    assert "# TYPE maestro_store_reconnects_total counter" in body
    assert METRICS.get("maestro_store_reconnects_total") == 0


# --- the compose profile -------------------------------------------------------------------------------


def test_the_postgres_compose_profile_is_opt_in():
    """`docker compose up` must not start a database for someone happy with SQLite."""
    import re
    from pathlib import Path

    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    db_service = compose.split("\n  db:", 1)
    assert len(db_service) == 2, "the compose file should define a db service"
    assert re.search(r'profiles:\s*\["postgres"\]', db_service[1]), "the db service must be opt-in"
    assert "MAESTRO_DB_URL=postgresql://" in compose  # and say how to point Maestro at it
