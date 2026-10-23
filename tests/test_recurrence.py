from datetime import time

import pytest
from conftest import dt

from maestro import recurrence
from maestro.models import Status
from maestro.parser import parse, parse_duration
from maestro.scheduler import fire_due
from maestro.store import Store

NOW = dt(2026, 10, 23, 18, 0)  # a Friday evening


@pytest.mark.parametrize(
    "source, text, rule, first_due",
    [
        ("stand-up every weekday at 9:30am", "Stand-up", "weekdays", dt(2026, 10, 26, 9, 30)),
        ("take vitamins every day at 8am", "Take vitamins", "daily", dt(2026, 10, 24, 8, 0)),
        ("journal daily at 9pm", "Journal", "daily", dt(2026, 10, 23, 21, 0)),
        ("gym every monday at 7am", "Gym", "weekly:0", dt(2026, 10, 26, 7, 0)),
        ("pay rent on the 1st of every month", "Pay rent", "monthly:1", dt(2026, 11, 1, 9, 0)),
        ("drink water every 2 hours", "Drink water", "interval:120", dt(2026, 10, 23, 20, 0)),
        ("review budget every week", "Review budget", "weekly:4", dt(2026, 10, 30, 9, 0)),
    ],
)
def test_parse_recurring(source, text, rule, first_due):
    item = parse(source, now=NOW)
    assert (item.text, item.recurrence, item.due_at) == (text, rule, first_due)


@pytest.mark.parametrize(
    "rule, after, expected",
    [
        ("daily", dt(2026, 10, 23, 9, 0), dt(2026, 10, 24, 9, 0)),
        ("weekdays", dt(2026, 10, 23, 9, 0), dt(2026, 10, 26, 9, 0)),  # Fri -> Mon
        ("weekly:4", dt(2026, 10, 23, 9, 0), dt(2026, 10, 30, 9, 0)),
        ("monthly:31", dt(2026, 10, 31, 9, 0), dt(2026, 11, 30, 9, 0)),  # clamped
        ("monthly:15", dt(2026, 12, 20, 9, 0), dt(2027, 1, 15, 9, 0)),  # year rollover
    ],
)
def test_next_occurrence(rule, after, expected):
    assert recurrence.next_occurrence(rule, after, time(9, 0)) == expected


def test_recurring_reminder_reschedules_instead_of_finishing():
    store = Store()
    item = store.add(parse("take vitamins every day at 8am", now=NOW))
    sent = []
    notifier = type("N", (), {"send": lambda self, i: sent.append(i.id)})()

    fire_due(store, notifier, now=dt(2026, 10, 24, 8, 0))
    after = store.get(item.id)
    assert after.status == Status.PENDING
    assert after.due_at == dt(2026, 10, 25, 8, 0)


def test_long_outage_fires_once_and_skips_ahead():
    store = Store()
    item = store.add(parse("take vitamins every day at 8am", now=NOW))
    notifier = type("N", (), {"send": lambda self, i: None})()

    fired = fire_due(store, notifier, now=dt(2026, 10, 30, 12, 0))
    assert len(fired) == 1
    assert store.get(item.id).due_at == dt(2026, 10, 31, 8, 0)


@pytest.mark.parametrize("text, minutes", [("10m", 10), ("2h", 120), ("1 day", 1440), ("3 mins", 3)])
def test_parse_duration(text, minutes):
    assert parse_duration(text).total_seconds() == minutes * 60


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")
