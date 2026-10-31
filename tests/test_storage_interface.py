"""The storage interface is the contract between Maestro and a backend.

A second backend (PostgreSQL, #34) is only safe if the interface is the whole story: everything the
rest of the code calls is declared on `Store`, and a backend that forgets a method fails loudly
rather than at runtime in production. mypy checks the types; these tests check the shape.
"""

import ast
import inspect
from pathlib import Path

import pgsupport
import pytest

from maestro.storage import SQLiteStore, Store

# What each backend has of its own, which no other backend must provide. Anything else public on a
# backend is either part of the interface or something callers should not be reaching for.
SQLITE_ONLY = {"journal_mode", "conn", "path"}
POSTGRES_ONLY = {"conn", "url"}

BACKENDS = {SQLiteStore: SQLITE_ONLY}
try:
    from maestro.storage.postgres import PostgresStore

    BACKENDS[PostgresStore] = POSTGRES_ONLY
except ImportError:  # psycopg isn't installed; the backend can't be checked
    pass


def public_names(cls):
    """Methods and properties, plus the attributes the interface declares (`tz`, `closed`), which
    are set per instance and so don't show up on the class."""
    members = {name for name, _ in inspect.getmembers(cls) if not name.startswith("_")}
    return members | {name for name in getattr(cls, "__annotations__", {}) if not name.startswith("_")}


def test_the_interface_cannot_be_used_on_its_own():
    with pytest.raises(TypeError, match="abstract"):
        Store()  # type: ignore[abstract]


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda b: b.__name__)
def test_every_backend_implements_every_method(backend):
    assert backend.__abstractmethods__ == frozenset()
    assert issubclass(backend, Store)


def test_a_backend_that_forgets_a_method_cannot_be_instantiated():
    class Forgetful(Store):
        pass

    with pytest.raises(TypeError, match="abstract"):
        Forgetful()  # type: ignore[abstract]
    assert "add" in Forgetful.__abstractmethods__


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda b: b.__name__)
def test_a_backend_adds_nothing_public_beyond_the_interface(backend):
    """A new public method on one backend is a method every other backend has to grow too; put it
    on the interface, or keep it private."""
    own = BACKENDS[backend]
    methods_and_properties = {name for name in own if isinstance(getattr(backend, name, None), property)}
    assert public_names(backend) - public_names(Store) == methods_and_properties


def test_the_sqlite_backend_adds_only_its_own_attributes():
    with SQLiteStore() as store:
        assert {n for n in vars(store) if not n.startswith("_")} - public_names(Store) \
            == SQLITE_ONLY - {"journal_mode"}


@pgsupport.needs_postgres
def test_the_postgres_backend_adds_only_its_own_attributes():
    from maestro.storage import open_store

    with open_store(pgsupport.URL) as store:
        assert {n for n in vars(store) if not n.startswith("_")} - public_names(Store) \
            == POSTGRES_ONLY


METHODS = sorted(name for name, member in inspect.getmembers(Store, callable) if not name.startswith("_"))


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda b: b.__name__)
@pytest.mark.parametrize("name", METHODS)
def test_signatures_match_the_interface(name, backend):
    """Including argument names and defaults: callers use keywords, and a backend that renames an
    argument would break them only at the call site that uses it."""
    assert inspect.signature(getattr(backend, name)) == inspect.signature(getattr(Store, name))


def store_attributes(source):
    """Every `store.<name>` touched in a Python source file, with its line number."""
    tree = ast.parse(source)
    return [(node.lineno, node.attr) for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "store"]


def test_everything_the_rest_of_maestro_calls_is_on_the_interface():
    """Catches a caller quietly depending on a SQLite-only attribute. `maestro.doctor` is excluded:
    it reports on a SQLite file on purpose and opens `SQLiteStore` directly."""
    package = Path(inspect.getfile(Store)).parent.parent
    allowed = public_names(Store)
    offenders = [f"{path.name}:{line} store.{attr}"
                 for path in sorted(package.glob("*.py")) if path.name not in ("doctor.py", "store.py")
                 for line, attr in store_attributes(path.read_text())
                 if attr not in allowed]
    assert offenders == []


# --- the conformance suite is the interface's test ---------------------------------------------

CONFORMANCE = Path(__file__).parent / "test_storage_conformance.py"


def test_the_conformance_suite_exercises_every_method():
    """A method nobody conforms to is a method a second backend can get wrong unnoticed."""
    exercised = {attr for _, attr in store_attributes(CONFORMANCE.read_text())}
    assert set(METHODS) - exercised == set()


def test_the_conformance_suite_stays_backend_independent():
    """It must pass against PostgreSQL unchanged, so it may not reach for SQLite's connection or
    its file, and it may not drive a database driver itself."""
    tree = ast.parse(CONFORMANCE.read_text())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert {attr for _, attr in store_attributes(CONFORMANCE.read_text())} & SQLITE_ONLY == set()
    assert "sqlite3" not in imported and "psycopg" not in imported
