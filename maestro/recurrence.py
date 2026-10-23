"""Recurring reminders.

Rules are stored as compact strings so they survive schema changes and are easy to read in the DB:

    daily            every day
    weekdays         Monday to Friday
    weekly:<0-6>     one weekday (0 = Monday)
    monthly:<1-31>   a day of the month, clamped to short months
    interval:<min>   every N minutes
"""

import calendar
import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_INTERVAL_RE = re.compile(r"\bevery\s+(\d+)\s+(minute|min|hour|hr)s?\b", re.I)
_DAILY_RE = re.compile(r"\b(?:every\s*day|daily)\b", re.I)
_WEEKDAYS_RE = re.compile(r"\b(?:every|on)\s+weekdays?\b", re.I)
_WEEKLY_RE = re.compile(r"\bevery\s+(" + "|".join(WEEKDAYS) + r")\b", re.I)
_EVERY_WEEK_RE = re.compile(r"\b(?:every\s+week|weekly)\b", re.I)
_MONTHLY_RE = re.compile(
    r"\b(?:every\s+month\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"|on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+of\s+every\s+month"
    r"|every\s+(\d{1,2})(?:st|nd|rd|th)?\s+of\s+the\s+month)\b",
    re.I,
)
_EVERY_MONTH_RE = re.compile(r"\b(?:every\s+month|monthly)\b", re.I)


def extract(text: str, today: date) -> tuple[str | None, re.Match | None]:
    """Find a recurrence phrase. Returns (rule, match) or (None, None)."""
    if m := _INTERVAL_RE.search(text):
        minutes = int(m[1]) * (60 if m[2].lower() in ("hour", "hr") else 1)
        return f"interval:{minutes}", m
    if m := _WEEKDAYS_RE.search(text):
        return "weekdays", m
    if m := _WEEKLY_RE.search(text):
        return f"weekly:{WEEKDAYS.index(m[1].lower())}", m
    if m := _DAILY_RE.search(text):
        return "daily", m
    if m := _EVERY_WEEK_RE.search(text):
        return f"weekly:{today.weekday()}", m
    if m := _MONTHLY_RE.search(text):
        day = int(m[1] or m[2] or m[3])
        if 1 <= day <= 31:
            return f"monthly:{day}", m
    if m := _EVERY_MONTH_RE.search(text):
        return f"monthly:{today.day}", m
    return None, None


def _month_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_occurrence(rule: str, after: datetime, at: time) -> datetime:
    """The first occurrence of `rule` at time `at` strictly after `after`."""
    kind, _, arg = rule.partition(":")
    if kind == "interval":
        return (after + timedelta(minutes=int(arg))).replace(second=0, microsecond=0)

    if kind == "monthly":
        day = int(arg)
        candidate = datetime.combine(_month_day(after.year, after.month, day), at)
        if candidate <= after:
            year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
            candidate = datetime.combine(_month_day(year, month, day), at)
        return candidate

    def matches(d: date) -> bool:
        if kind == "daily":
            return True
        if kind == "weekdays":
            return d.weekday() < 5
        if kind == "weekly":
            return d.weekday() == int(arg)
        raise ValueError(f"Unknown recurrence rule: {rule!r}")

    for offset in range(8):
        d = after.date() + timedelta(days=offset)
        candidate = datetime.combine(d, at)
        if candidate > after and matches(d):
            return candidate
    raise ValueError(f"No occurrence found for {rule!r}")


def describe(rule: str) -> str:
    kind, _, arg = rule.partition(":")
    if kind == "interval":
        minutes = int(arg)
        return f"every {minutes // 60}h" if minutes % 60 == 0 else f"every {minutes}m"
    if kind == "weekly":
        return f"every {WEEKDAYS[int(arg)].capitalize()}"
    if kind == "monthly":
        return f"monthly on day {arg}"
    return kind
