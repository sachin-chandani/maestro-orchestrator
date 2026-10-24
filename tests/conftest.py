import inspect
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import settings

# CI runners are slow and noisy: no per-example deadline there, so timing never fails a property test.
settings.register_profile("ci", deadline=None, print_blob=True)
if os.environ.get("CI"):
    settings.load_profile("ci")

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
    from maestro.store import Store

    tests_dir = str(Path(__file__).parent)
    by_tests, by_product = [], []
    original_init = Store.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        caller = inspect.stack(context=0)[1].filename
        (by_tests if caller.startswith(tests_dir) else by_product).append((self, caller))

    monkeypatch.setattr(Store, "__init__", tracking_init)
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
