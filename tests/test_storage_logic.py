"""What done and snooze mean for a recurring reminder (ADR 0009), without a database.

These rules used to live inside the SQLite store. They are the same on every backend, so they are
tested here directly; `tests/test_recurring_actions.py` still covers them through a real store.
"""

from datetime import timedelta

import pytest
from conftest import TZ, dt
from hypothesis import given
from hypothesis import strategies as st

from maestro.storage import logic
from maestro.tz import UTC

# Pills at 9am every day; the person was reminded at Monday's 9am.
MONDAY = dt(2026, 10, 26, 9, 0)
TUESDAY = dt(2026, 10, 27, 9, 0)
WEDNESDAY = dt(2026, 10, 28, 9, 0)


def daily(next_due=MONDAY, snoozed_until=None):
    return logic.Series("daily", next_due, snoozed_until)


# --- done ---------------------------------------------------------------------------------


def test_done_on_the_upcoming_occurrence_skips_it():
    # "I took them early": nothing was delivered, so done is about the one still to come.
    assert logic.complete(daily(), target=None) == logic.Move(TUESDAY, None)


def test_done_on_the_delivered_occurrence_moves_the_series_past_it():
    assert logic.complete(daily(), target=MONDAY) == logic.Move(TUESDAY, None)


def test_done_on_an_old_delivery_leaves_a_series_that_already_moved_on():
    # A late reply to Monday's reminder must not skip Tuesday, which is already the next one.
    assert logic.complete(daily(next_due=TUESDAY), target=MONDAY) == logic.Move(TUESDAY, None)


def test_done_on_a_snoozed_re_send_clears_the_snooze_only():
    snoozed = daily(next_due=TUESDAY, snoozed_until=MONDAY)
    assert logic.complete(snoozed, target=MONDAY) == logic.Move(TUESDAY, None)


def test_done_with_a_snooze_pending_and_no_delivery_cancels_the_re_send():
    # The upcoming occurrence *is* the snoozed re-send, so the series itself doesn't move.
    snoozed = daily(next_due=TUESDAY, snoozed_until=MONDAY)
    assert logic.complete(snoozed, target=None) == logic.Move(TUESDAY, None)


def test_done_keeps_a_snooze_belonging_to_a_different_occurrence():
    snoozed = daily(next_due=MONDAY, snoozed_until=dt(2026, 10, 26, 11, 0))
    move = logic.complete(snoozed, target=MONDAY)
    assert move == logic.Move(TUESDAY, dt(2026, 10, 26, 11, 0))


def test_done_keeps_the_wall_clock_time_across_a_dst_change():
    # US clocks go back on Sun Nov 1, 2026: 9am stays 9am, which is 25 real hours later.
    move = logic.complete(daily(next_due=dt(2026, 10, 31, 9, 0)), target=None)
    assert move.due_at == dt(2026, 11, 1, 9, 0)
    elapsed = move.due_at.astimezone(UTC) - dt(2026, 10, 31, 9, 0).astimezone(UTC)
    assert elapsed == timedelta(hours=25)


def test_done_on_an_interval_rule_advances_by_the_interval():
    series = logic.Series("interval:120", dt(2026, 10, 26, 9, 0), None)
    assert logic.complete(series, target=None).due_at == dt(2026, 10, 26, 11, 0)


def test_done_on_every_other_day_keeps_its_rhythm():
    series = logic.Series("days:2", MONDAY, None)
    assert logic.complete(series, target=None).due_at == WEDNESDAY


# --- snooze -------------------------------------------------------------------------------


def test_snooze_within_the_day_re_sends_later_and_skips_the_occurrence():
    later = dt(2026, 10, 26, 9, 30)
    assert logic.snooze(daily(), later, target=None) == logic.Move(TUESDAY, later)


def test_snooze_past_the_next_occurrences_skips_all_of_them():
    until = dt(2026, 10, 28, 8, 0)
    assert logic.snooze(daily(), until, target=None) == logic.Move(WEDNESDAY, until)


def test_snooze_of_a_delivered_occurrence_leaves_the_series_alone():
    # Monday was delivered and the series already points at Tuesday; a 30-minute snooze of Monday's
    # reminder must not push Tuesday away.
    later = dt(2026, 10, 26, 9, 30)
    assert logic.snooze(daily(next_due=TUESDAY), later, target=MONDAY) == logic.Move(TUESDAY, later)


def test_snooze_of_a_delivered_occurrence_past_the_next_one_skips_it():
    until = dt(2026, 10, 27, 12, 0)
    assert logic.snooze(daily(next_due=TUESDAY), until, target=MONDAY) == logic.Move(WEDNESDAY, until)


def test_snoozing_again_moves_only_the_re_send():
    first = dt(2026, 10, 26, 9, 30)
    second = dt(2026, 10, 26, 10, 0)
    series = daily(next_due=TUESDAY, snoozed_until=first)
    assert logic.snooze(series, second, target=None) == logic.Move(TUESDAY, second)


# --- invariants ---------------------------------------------------------------------------

RULES = ["daily", "weekdays", "weekly:2", "monthly:31", "interval:90", "days:3", "weeks:2:4"]
_times = st.datetimes(min_value=dt(2026, 1, 1).replace(tzinfo=None),
                      max_value=dt(2027, 12, 31).replace(tzinfo=None),
                      timezones=st.just(TZ))


@given(rule=st.sampled_from(RULES), next_due=_times, until=_times, delivered=st.booleans())
def test_a_snooze_always_leaves_the_series_after_the_re_send(rule, next_due, until, delivered):
    """Invariant: snoozed_until is always before due_at, as `Item` documents, so the scheduler
    sends the re-send first and the series afterwards."""
    series = logic.Series(rule, next_due, None)
    move = logic.snooze(series, until, target=next_due if delivered else None)
    assert move.snoozed_until == until
    assert move.due_at > until


@given(rule=st.sampled_from(RULES), next_due=_times, delivered=st.booleans())
def test_done_never_moves_a_series_backwards(rule, next_due, delivered):
    move = logic.complete(logic.Series(rule, next_due, None), target=next_due if delivered else None)
    assert move.due_at > next_due


# --- the acknowledgement window -------------------------------------------------------------


@pytest.mark.parametrize("age, expected", [(timedelta(hours=11), True), (timedelta(hours=13), False)])
def test_only_recent_deliveries_are_what_a_reply_is_about(age, expected):
    now = dt(2026, 10, 26, 20, 0)
    assert ((now - age) >= logic.ack_cutoff(now)) is expected
