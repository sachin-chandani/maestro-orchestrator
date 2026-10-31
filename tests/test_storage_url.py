"""Which database a setting names, and how the backend behind it is opened.

`MAESTRO_DB` has always been a SQLite path and still is; `MAESTRO_DB_URL` is the way to name any
other backend. A wrong setting must say so clearly, and a URL's password must never be printed.
"""

import pytest
from conftest import TZ

from maestro.config import DEFAULT_DB, Config
from maestro.storage import SQLiteStore, StoreConfigError, Target, open_store, parse_url
from maestro.storage.factory import redact

PATHS = [
    ("~/.maestro/maestro.db", "~/.maestro/maestro.db"),
    ("/data/maestro.db", "/data/maestro.db"),
    ("./relative.db", "./relative.db"),
    ("maestro.db", "maestro.db"),
    (":memory:", ":memory:"),
    ("", ""),  # a private temporary database
    ("/tmp/odd:name.db", "/tmp/odd:name.db"),
]

SQLITE_URLS = [
    ("sqlite:///data/maestro.db", "/data/maestro.db"),
    ("sqlite:////data/maestro.db", "//data/maestro.db"),  # SQLAlchemy's spelling, same file
    ("sqlite://./relative.db", "./relative.db"),
    ("sqlite:relative.db", "relative.db"),
    ("sqlite://:memory:", ":memory:"),
    ("SQLite:///data/maestro.db", "/data/maestro.db"),
]


@pytest.mark.parametrize("setting, path", PATHS)
def test_a_plain_path_is_a_sqlite_file(setting, path):
    assert parse_url(setting) == Target("sqlite", path)


@pytest.mark.parametrize("url, path", SQLITE_URLS)
def test_a_sqlite_url_names_the_file_it_reads_as(url, path):
    assert parse_url(url) == Target("sqlite", path)


@pytest.mark.parametrize("url", ["postgresql://maestro@db/maestro", "postgres://db/maestro",
                                 "POSTGRESQL://db/maestro"])
def test_both_postgres_spellings_are_understood(url):
    assert parse_url(url) == Target("postgresql", url)


@pytest.mark.parametrize("url", ["mysql://db/maestro", "redis://localhost", "http://example.com/db"])
def test_an_unsupported_backend_says_what_is_supported(url):
    with pytest.raises(StoreConfigError, match="Maestro supports postgresql, sqlite"):
        parse_url(url)


def test_a_bad_url_never_shows_its_password():
    with pytest.raises(StoreConfigError) as caught:
        parse_url("mysql://maestro:hunter2@db/maestro")
    assert "hunter2" not in str(caught.value) and "***" in str(caught.value)


@pytest.mark.parametrize("url, shown", [
    ("postgresql://maestro:hunter2@db:5432/maestro", "postgresql://maestro:***@db:5432/maestro"),
    ("postgresql://:hunter2@db/maestro", "postgresql://***@db/maestro"),
    ("postgresql://maestro@db/maestro", "postgresql://maestro@db/maestro"),  # no password to hide
    # libpq takes the password as a query parameter too.
    ("postgresql://db/maestro?password=hunter2&sslmode=require",
     "postgresql://db/maestro?password=***&sslmode=require"),
    ("/data/maestro.db", "/data/maestro.db"),
    ("", ""),
])
def test_passwords_are_redacted_everywhere_a_url_is_shown(url, shown):
    assert redact(url) == shown
    assert parse_url(url).display == shown


@pytest.mark.parametrize("url", ["postgresql://maestro:hunter2@db:notaport/maestro",
                                 "postgresql://maestro:hunter2@", "postgres://u:hunter2@h/db@odd"])
def test_a_malformed_url_is_still_redacted_rather_than_crashing(url):
    """This runs on whatever someone typed into the setting, on the way to an error message."""
    assert "hunter2" not in redact(url)


def test_a_port_is_not_mistaken_for_a_password():
    """Without an `@` there are no credentials: `host:5432` is a host and a port, and hiding it
    would only make a connection problem harder to read."""
    assert redact("postgresql://db:5432/maestro") == "postgresql://db:5432/maestro"


# --- opening ----------------------------------------------------------------------------------


def test_a_path_opens_a_sqlite_store(tmp_path):
    with open_store(tmp_path / "m.db", tz=TZ) as store:
        assert isinstance(store, SQLiteStore)
        assert store.tz == TZ
        assert store.schema_version > 0
    assert (tmp_path / "m.db").exists()


def test_a_sqlite_url_and_its_path_are_the_same_database(tmp_path):
    from datetime import datetime

    from maestro.models import Item, Kind

    with open_store(f"sqlite://{tmp_path}/m.db", tz=TZ) as store:
        store.add(Item(text="Buy milk", kind=Kind.NOTE, created_at=datetime.now(TZ)))
    with open_store(str(tmp_path / "m.db"), tz=TZ) as store:
        assert [i.text for i in store.list()] == ["Buy milk"]


def test_opening_an_unsupported_backend_fails_before_touching_anything():
    with pytest.raises(StoreConfigError):
        open_store("mysql://db/maestro")


def test_a_postgres_url_reaches_the_postgres_backend():
    """Without a server to talk to, the driver's own error proves the URL got that far. Port 1 on
    loopback refuses at once, so there is no name to resolve and nothing on the network."""
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.OperationalError):
        open_store("postgresql://maestro@127.0.0.1:1/maestro")


def test_a_backend_maestro_does_not_have_says_so_plainly(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr("importlib.import_module", missing)
    with pytest.raises(StoreConfigError, match="no PostgreSQL backend yet"):
        open_store("postgresql://db/maestro")


def test_a_missing_driver_points_at_the_extra(monkeypatch):
    def no_psycopg(name):
        raise ModuleNotFoundError("No module named 'psycopg'", name="psycopg")

    monkeypatch.setattr("importlib.import_module", no_psycopg)
    with pytest.raises(StoreConfigError, match=r"pip install 'maestro-orchestrator\[postgres\]'"):
        open_store("postgresql://db/maestro")


def test_a_postgres_url_is_handed_to_the_backend_whole(monkeypatch):
    """What Phase 4 plugs into: the factory passes the URL and timezone through untouched."""
    opened = {}

    class FakeModule:
        @staticmethod
        def PostgresStore(url, tz):  # noqa: N802 - mirrors the class it stands in for
            opened.update(url=url, tz=tz)
            return "a store"

    monkeypatch.setattr("importlib.import_module", lambda name: FakeModule)
    assert open_store("postgresql://maestro@db/maestro", tz=TZ) == "a store"
    assert opened == {"url": "postgresql://maestro@db/maestro", "tz": TZ}


# --- the setting ---------------------------------------------------------------------------------


def test_maestro_db_is_still_a_sqlite_path():
    assert Config.from_env({"MAESTRO_DB": "/data/maestro.db"}).db_path == "/data/maestro.db"


def test_maestro_db_url_names_any_backend():
    config = Config.from_env({"MAESTRO_DB_URL": "postgresql://db/maestro"})
    assert parse_url(config.db_path).backend == "postgresql"


def test_the_url_wins_when_both_are_set():
    env = {"MAESTRO_DB_URL": "postgresql://db/maestro", "MAESTRO_DB": "/data/maestro.db"}
    assert Config.from_env(env).db_path == "postgresql://db/maestro"


def test_neither_setting_means_the_default_file():
    assert Config.from_env({}).db_path == str(DEFAULT_DB)
    assert parse_url(Config.from_env({}).db_path).backend == "sqlite"
