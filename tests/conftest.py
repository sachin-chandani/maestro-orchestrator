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
