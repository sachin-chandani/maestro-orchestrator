"""Phrases added in #16: parts of the day, deadlines, weekends, days of the month, every N days/weeks."""

import pytest
from conftest import dt

from maestro.models import Kind, Status
from maestro.notifier import MultiNotifier
from maestro.parser import parse
from maestro.scheduler import fire_due
from maestro.store import Store

FRIDAY_EVENING = dt(2026, 10, 23, 18, 0)
TUESDAY_MORNING = dt(2026, 10, 27, 8, 0)


@pytest.mark.parametrize(
    "source, now, text, due",
    [
        ("call bank tomorrow morning", FRIDAY_EVENING, "Call bank", dt(2026, 10, 24, 9, 0)),
        ("gym tomorrow evening", FRIDAY_EVENING, "Gym", dt(2026, 10, 24, 18, 0)),
        ("read tomorrow morning at 7:30am", FRIDAY_EVENING, "Read", dt(2026, 10, 24, 7, 30)),
        ("water plants this afternoon", TUESDAY_MORNING, "Water plants", dt(2026, 10, 27, 14, 0)),
        ("lock the door at night", TUESDAY_MORNING, "Lock the door", dt(2026, 10, 27, 21, 0)),
        ("send report by end of day", TUESDAY_MORNING, "Send report", dt(2026, 10, 27, 17, 0)),
        ("submit eod", FRIDAY_EVENING, "Submit", dt(2026, 10, 24, 17, 0)),  # today's EOD has passed
        ("finish deck by end of week", TUESDAY_MORNING, "Finish deck", dt(2026, 10, 30, 17, 0)),
        ("finish deck eow", FRIDAY_EVENING, "Finish deck", dt(2026, 10, 30, 17, 0)),  # next Friday
        ("clean garage this weekend", TUESDAY_MORNING, "Clean garage", dt(2026, 10, 31, 10, 0)),
        ("clean garage this weekend", dt(2026, 10, 25, 8, 0), "Clean garage", dt(2026, 10, 25, 10, 0)),
        ("dentist on the 5th", FRIDAY_EVENING, "Dentist", dt(2026, 11, 5, 9, 0)),
        ("pay bill by the 28th at 6pm", FRIDAY_EVENING, "Pay bill", dt(2026, 10, 28, 18, 0)),
        ("pay bill on the 23rd", FRIDAY_EVENING, "Pay bill", dt(2026, 11, 23, 9, 0)),  # today counts as past
        ("call grandma on the 31st", dt(2026, 11, 2, 9, 0), "Call grandma", dt(2026, 11, 30, 9, 0)),
        ("call mom in 3 days at 5pm", FRIDAY_EVENING, "Call mom", dt(2026, 10, 26, 17, 0)),
    ],
)
def test_one_off_phrases(source, now, text, due):
    item = parse(source, now=now)
    assert (item.text, item.due_at, item.recurrence) == (text, due, None)


@pytest.mark.parametrize(
    "source, text, rule, first_due",
    [
        ("backup every 3 days", "Backup", "days:3", dt(2026, 10, 24, 9, 0)),
        ("stretch every other day at 7am", "Stretch", "days:2", dt(2026, 10, 24, 7, 0)),
        ("water plants every other week", "Water plants", "weeks:2:4", dt(2026, 10, 30, 9, 0)),
        ("review every 2 weeks on monday", "Review", "weeks:2:0", dt(2026, 10, 26, 9, 0)),
        ("standup every other friday at 9am", "Standup", "weeks:2:4", dt(2026, 10, 30, 9, 0)),
        ("biweekly sync on tuesday", "Sync", "weeks:2:1", dt(2026, 10, 27, 9, 0)),
        ("payroll fortnightly", "Payroll", "weeks:2:4", dt(2026, 10, 30, 9, 0)),
        ("team lunch every week on thursday at noon", "Team lunch", "weekly:3", dt(2026, 10, 29, 12, 0)),
        ("meditate every night", "Meditate", "daily", dt(2026, 10, 23, 21, 0)),
        ("journal every evening at 9:30pm", "Journal", "daily", dt(2026, 10, 23, 21, 30)),
        ("every 1 days check mail", "Check mail", "daily", dt(2026, 10, 24, 9, 0)),
    ],
)
def test_recurring_phrases(source, text, rule, first_due):
    item = parse(source, now=FRIDAY_EVENING)
    assert (item.text, item.recurrence, item.due_at) == (text, rule, first_due)


@pytest.mark.parametrize("source", ["the 5th element is a film", "read about the 1st world war",
                                    "the weekly report template is in drive",
                                    "my daily standup notes are in notion",
                                    "our monthly budget sheet lives on the nas",
                                    "the biweekly newsletter draft is in docs"])
def test_ordinary_text_stays_a_note(source):
    item = parse(source, now=FRIDAY_EVENING)
    assert item.kind == Kind.NOTE


def _fire(store, item_id, now):
    fire_due(store, MultiNotifier({"push": type("N", (), {"send": lambda self, i: None})()}), now=now)
    return store.get(item_id).due_at


def test_every_other_week_keeps_its_rhythm_after_an_outage():
    store = Store()
    item = store.add(parse("standup every other friday at 9am", now=FRIDAY_EVENING))
    assert item.due_at == dt(2026, 10, 30, 9, 0)

    assert _fire(store, item.id, dt(2026, 10, 30, 9, 0)) == dt(2026, 11, 13, 9, 0)
    # Server down for three weeks: skips ahead but stays on the same alternate Fridays.
    assert _fire(store, item.id, dt(2026, 12, 2, 12, 0)) == dt(2026, 12, 11, 9, 0)
    assert store.get(item.id).status == Status.PENDING


def test_every_three_days_steps_from_the_last_occurrence():
    store = Store()
    item = store.add(parse("backup every 3 days", now=FRIDAY_EVENING))
    assert _fire(store, item.id, dt(2026, 10, 24, 9, 0)) == dt(2026, 10, 27, 9, 0)
    assert _fire(store, item.id, dt(2026, 10, 27, 9, 5)) == dt(2026, 10, 30, 9, 0)
    # Late by 5 days: next is on the original 3-day grid (Nov 2), not 3 days from now.
    assert _fire(store, item.id, dt(2026, 11, 4, 9, 0)) == dt(2026, 11, 5, 9, 0)


@pytest.mark.parametrize("now, due", [
    (dt(2026, 10, 24, 9, 0), dt(2026, 10, 24, 10, 0)),   # Saturday morning: later today
    (dt(2026, 10, 24, 10, 0), dt(2026, 10, 25, 10, 0)),  # Saturday at/after 10: Sunday (found by #56)
    (dt(2026, 10, 25, 12, 0), dt(2026, 10, 31, 10, 0)),  # Sunday afternoon: next Saturday
])
def test_this_weekend_never_lands_in_the_past(now, due):
    assert parse("clean garage this weekend", now=now).due_at == due
