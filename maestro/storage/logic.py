"""Decisions a store makes that have nothing to do with SQL.

What "done" and "snooze" mean for a recurring reminder (ADR 0009) is a product rule, not a storage
detail: every backend must answer it the same way. The rules live here as pure functions over plain
values so both backends call the same code and a test can check them without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from maestro import recurrence

# A done/snooze acknowledges the latest delivery only if it was sent within this window.
ACK_WINDOW = timedelta(hours=12)


@dataclass(frozen=True)
class Series:
    """A recurring item that is still running, as the store holds it."""

    rule: str
    next_due: datetime  # the series' own next occurrence
    snoozed_until: datetime | None  # a single occurrence pushed back by the person, or None


@dataclass(frozen=True)
class Move:
    """Where an action leaves a recurring item."""

    due_at: datetime
    snoozed_until: datetime | None


def _advance(series: Series, after: datetime) -> datetime:
    """The series' first occurrence strictly after `after`, keeping its wall-clock time and rhythm."""
    return recurrence.next_occurrence(series.rule, after, series.next_due.time(), anchor=series.next_due)


def complete(series: Series, target: datetime | None) -> Move:
    """Where "done" leaves a recurring item: one occurrence is finished, the series carries on.

    `target` is the delivered occurrence being acknowledged, or None when the person means the
    upcoming one ("I took them early"), which is then skipped.
    """
    snoozed = series.snoozed_until
    next_due = series.next_due
    if target is not None:
        if snoozed == target:
            snoozed = None  # done with the snoozed re-send
        if next_due <= target:  # still on that occurrence (e.g. another channel retrying)
            next_due = _advance(series, target)
    elif snoozed is not None:
        snoozed = None  # the upcoming one was the snoozed re-send
    else:
        next_due = _advance(series, next_due)
    return Move(next_due, snoozed)


def snooze(series: Series, until: datetime, target: datetime | None) -> Move:
    """Where a snooze leaves a recurring item: the occurrence goes out again at `until` while the
    series keeps its own time, and any occurrence falling on or before `until` is skipped."""
    if target is None and series.snoozed_until is None:
        passed = max(until, series.next_due)  # snoozing the upcoming occurrence itself: skip it
    else:
        passed = max(until, target) if target is not None else until
    next_due = _advance(series, passed) if series.next_due <= passed else series.next_due
    return Move(next_due, until)


def ack_cutoff(now: datetime) -> datetime:
    """The oldest delivery a reply arriving at `now` can be reacting to.

    Anything sent before this isn't what the person means (e.g. snoozing today's reminder after
    ignoring yesterday's), so it stays unacknowledged.
    """
    return now - ACK_WINDOW
