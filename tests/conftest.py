from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

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
def _close_stores(monkeypatch):
    """Close every Store a test opens, so leaked connections in product code stand out."""
    from maestro.store import Store

    opened = []
    original_init = Store.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Store, "__init__", tracking_init)
    yield
    for store in opened:
        store.close()


@pytest.fixture(autouse=True)
def _fresh_metrics():
    from maestro.observability import METRICS

    METRICS.reset()
