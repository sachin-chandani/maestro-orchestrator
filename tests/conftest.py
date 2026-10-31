import inspect
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import settings

# No per-example deadline anywhere: dateparser's cold start and Store setup make first examples slow,
# and a timing failure says nothing about correctness. CI also prints a reproduction blob.
settings.register_profile("local", deadline=None)
settings.register_profile("ci", deadline=None, print_blob=True)
settings.load_profile("ci" if os.environ.get("CI", "").lower() in ("1", "true", "yes") else "local")

# A zone with DST: in 2026 US clocks fall back on Sun Nov 1, just after the dates most tests use.
TZ = ZoneInfo("America/New_York")


def dt(*args: int) -> datetime:
    """An aware datetime in the test timezone."""
    return datetime(*args, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    # Results must not depend on the machine running the tests.
    monkeypatch.setenv("MAESTRO_TIMEZONE", TZ.key)


@pytest.fixture(autouse=True)
def _no_leaked_stores(monkeypatch):
    """Stores opened by a test are closed for it; a Store opened by product code and left open
    fails the test. (ResourceWarning can't do this: sqlite3 only emits it on 3.13+, at GC time.)"""
    import maestro.storage
    from maestro.storage import SQLiteStore

    backends = [SQLiteStore]
    try:  # PostgreSQL stores hold a server connection; a leak matters at least as much
        from maestro.storage.postgres import PostgresStore

        backends.append(PostgresStore)
    except ImportError:  # psycopg isn't installed
        pass

    tests_dir = str(Path(__file__).parent)
    # `open_store` opens the store on its caller's behalf, so look past the storage package for
    # whoever asked: otherwise every store would look like product code.
    storage_dir = str(Path(maestro.storage.__file__).parent)
    by_tests, by_product = [], []

    def track(backend):
        original_init = backend.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            caller = next((frame.filename for frame in inspect.stack(context=0)[1:]
                           if not frame.filename.startswith(storage_dir)), "")
            (by_tests if caller.startswith(tests_dir) else by_product).append((self, caller))

        monkeypatch.setattr(backend, "__init__", tracking_init)

    for backend in backends:
        track(backend)
    yield
    for store, _ in by_tests:
        store.close()
    leaked = [caller for store, caller in by_product if not store.closed]
    for store, _ in by_product:
        store.close()
    assert not leaked, f"Store opened in product code was never closed: {leaked}"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    from maestro.observability import METRICS

    METRICS.reset()
