
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


# --- per-item channels and the urgent flag (#20) ------------------------------------------------


@pytest.mark.parametrize(
    "source, text, channels",
    [
        ("pay rent friday, text me", "Pay rent", ["twilio_sms"]),
        ("feed the cat tonight and call me", "Feed the cat", ["twilio_call"]),
        ("water the plants tomorrow, text me and email me", "Water the plants",
         ["twilio_sms", "email"]),
        ("take out the bins tonight, on telegram", "Take out the bins", ["telegram"]),
        ("submit the report by eod; via telegram", "Submit the report", ["telegram"]),
        ("text me to stretch at 3pm", "Stretch", ["twilio_sms"]),
        ("email me about the invoice on monday", "The invoice", ["email"]),
        ("remind me to renew the passport on monday, sms me", "Renew the passport", ["twilio_sms"]),
    ],
)
def test_clear_channel_instructions_pick_channels(source, text, channels):
    item = parse(source, now=NOW)
    assert item.text == text
    assert item.channels == channels
    assert item.source_text == source  # the capture is kept as said


@pytest.mark.parametrize(
    "source, text",
    [
        # The object isn't "me", or the phrase is part of the task, not a delivery instruction.
        ("remind me to call me a cab at 5pm", "Call me a cab"),
        ("remind me to text sara back tonight", "Text sara back"),
        ("email the team the agenda tomorrow", "Email the team the agenda"),
        ("ask if the kids will call me tomorrow", "Ask if the kids will call me"),
        ("message him on telegram tomorrow", "Message him on telegram"),
        ("text me at 5", "Text me"),  # mid-text, not on a clause boundary: kept as the task
    ],
)
def test_unclear_phrases_keep_their_text_and_the_default_channels(source, text):
    item = parse(source, now=NOW)
    assert item.text == text
    assert item.channels is None


def test_a_channel_said_twice_counts_once():
    item = parse("pay rent tomorrow, text me and sms me", now=NOW)
    assert item.channels == ["twilio_sms"]


def test_a_leading_bang_sets_urgent_and_is_dropped_from_the_text():
    item = parse("!take insulin at 11pm", now=NOW)
    assert item.urgent is True
    assert item.text == "Take insulin"
    assert item.source_text == "!take insulin at 11pm"  # kept as said


def test_the_word_urgent_sets_the_flag_but_stays_in_the_text():
    item = parse("urgent: call the plumber at 2pm", now=NOW)
    assert item.urgent is True
    assert item.text == "Urgent: call the plumber"


def test_not_urgent_is_not_urgent():
    assert parse("the backup check tomorrow is not urgent", now=NOW).urgent is False


def test_new_items_are_explicitly_not_urgent_by_default():
    # False, not None: only rows from before the flag existed carry None (ADR 0011).
    assert parse("call mom tomorrow at 5pm", now=NOW).urgent is False


@pytest.mark.parametrize(
    "source, text, channels",
    [
        # Regressions: "and then", dictation's sentence-final period, and "please" are boundaries.
        ("pay rent friday and then call me", "Pay rent", ["twilio_call"]),
        ("pay rent friday, text me and then email me", "Pay rent", ["twilio_sms", "email"]),
        ("Pay rent Friday. Text me.", "Pay rent", ["twilio_sms"]),
        ("water the plants tomorrow, please text me", "Water the plants", ["twilio_sms"]),
    ],
)
def test_connector_and_sentence_boundaries_are_recognised(source, text, channels):
    item = parse(source, now=NOW)
    assert item.text == text
    assert item.channels == channels


@pytest.mark.parametrize(
    "source, text",
    [
        # Regression: when time phrases consume everything else, the message body must not get the
        # channel phrase back — it was an instruction, never part of the task.
        ("every day at 9, text me", "Every day at 9"),
        ("tomorrow, text me", "Tomorrow"),
        ("in 10 minutes, call me", "In 10 minutes"),
    ],
)
def test_channel_phrase_never_leaks_back_into_an_emptied_text(source, text):
    item = parse(source, now=NOW)
    assert item.text == text
    assert item.channels is not None
