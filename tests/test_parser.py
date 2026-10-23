
import pytest
from conftest import dt

from maestro.models import Kind
from maestro.parser import parse

NOW = dt(2026, 10, 23, 18, 0)  # a Friday evening


@pytest.mark.parametrize(
    "source, text, due",
    [
        ("remind me to call mom tomorrow at 5pm", "Call mom", dt(2026, 10, 24, 17, 0)),
        ("pay rent on Nov 1", "Pay rent", dt(2026, 11, 1, 9, 0)),
        ("dentist appointment in 2 hours", "Dentist appointment", dt(2026, 10, 23, 20, 0)),
        ("buy milk at 7am", "Buy milk", dt(2026, 10, 24, 7, 0)),
        ("submit form next friday at 10am", "Submit form", dt(2026, 10, 30, 10, 0)),
        ("water the plants on monday", "Water the plants", dt(2026, 10, 26, 9, 0)),
        ("take out the trash tonight", "Take out the trash", dt(2026, 10, 23, 20, 0)),
        ("stand up at 19:30", "Stand up", dt(2026, 10, 23, 19, 30)),
        ("call the bank at noon", "Call the bank", dt(2026, 10, 24, 12, 0)),
        ("renew passport in 3 weeks", "Renew passport", dt(2026, 11, 13, 9, 0)),
        ("check the oven in an hour", "Check the oven", dt(2026, 10, 23, 19, 0)),
        ("don't forget to file taxes on 2027-03-15", "File taxes", dt(2027, 3, 15, 9, 0)),
    ],
)
def test_reminders(source, text, due):
    item = parse(source, now=NOW)
    assert item.kind == Kind.REMINDER
    assert item.text == text
    assert item.due_at == due
    assert item.source_text == source


def test_note_without_time():
    item = parse("remember that my passport is in the blue drawer", now=NOW)
    assert item.kind == Kind.NOTE
    assert item.due_at is None
    assert item.text == "My passport is in the blue drawer"


def test_time_later_today_stays_today():
    item = parse("call dad at 9pm", now=NOW)
    assert item.due_at == dt(2026, 10, 23, 21, 0)
