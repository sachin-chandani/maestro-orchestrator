"""Recurring reminders.

Rules are stored as compact strings so they survive schema changes and are easy to read in the DB:

    daily            every day
    weekdays         Monday to Friday
    weekly:<0-6>     one weekday (0 = Monday)
    monthly:<1-31>   a day of the month, clamped to short months
    interval:<min>   every N minutes
    days:<n>         every N days
    weeks:<n>:<0-6>  every N weeks on one weekday

`days` and `weeks` are anchored to the previous occurrence, so "every other Friday" keeps its
rhythm even if the server was down for a while.
"""

import calendar
import re
from datetime import date, datetime, time, timedelta

from maestro.tz import add_elapsed

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Bare "daily"/"weekly"/"monthly" after a determiner describe a noun ("the weekly report"), not a schedule.
_NOT_ADJECTIVE = "".join(rf"(?<!\b{w}\s)" for w in ("the", "a", "an", "my", "our", "your", "this", "that"))

_INTERVAL_RE = re.compile(r"\bevery\s+(\d+)\s+(minute|min|hour|hr)s?\b", re.I)
# "every night" matches only "every", leaving "night" for the parser to read as the time.
_DAILY_RE = re.compile(
    rf"\b(?:every\s*day|{_NOT_ADJECTIVE}daily|every(?=\s+(?:morning|afternoon|evening|night)\b))\b", re.I
)
_WEEKDAYS_RE = re.compile(r"\b(?:every|on)\s+weekdays?\b", re.I)
_WEEKLY_RE = re.compile(r"\bevery\s+(" + "|".join(WEEKDAYS) + r")\b", re.I)
_EVERY_WEEK_RE = re.compile(rf"\b(?:every\s+week|{_NOT_ADJECTIVE}weekly)\b", re.I)
_MONTHLY_RE = re.compile(
    rf"\b(?:(?:every\s+month|{_NOT_ADJECTIVE}monthly)\s+on\s+the\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    r"|on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+of\s+every\s+month"
    r"|every\s+(\d{1,2})(?:st|nd|rd|th)?\s+of\s+the\s+month)\b",
    re.I,
)
_EVERY_MONTH_RE = re.compile(rf"\b(?:every\s+month|{_NOT_ADJECTIVE}monthly)\b", re.I)
_WEEKDAY_ALT = "|".join(WEEKDAYS)
_EVERY_N_DAYS_RE = re.compile(r"\bevery\s+(other|\d+)\s+days?\b", re.I)
_EVERY_N_WEEKS_RE = re.compile(
    rf"\b(?:every\s+(other|\d+)\s+weeks?|({_NOT_ADJECTIVE}biweekly|{_NOT_ADJECTIVE}fortnightly|every\s+fortnight))"
    rf"(?:\s+on\s+({_WEEKDAY_ALT}))?\b",
    re.I,
)
_EVERY_OTHER_WEEKDAY_RE = re.compile(rf"\bevery\s+other\s+({_WEEKDAY_ALT})\b", re.I)
_ON_WEEKDAY_RE = re.compile(rf"\bon\s+({_WEEKDAY_ALT})s?\b", re.I)


def _count(word: str) -> int:
    return 2 if word.lower() == "other" else int(word)


def _weekday_elsewhere(text: str, rule_match: re.Match, today: date) -> tuple[int, list[re.Match]]:
    """For "biweekly sync on tuesday": a weekday that isn't next to the rule phrase."""
    for m in _ON_WEEKDAY_RE.finditer(text):
        if m.start() >= rule_match.end() or m.end() <= rule_match.start():
            return WEEKDAYS.index(m[1].lower()), [m]
    return today.weekday(), []


def extract(text: str, today: date) -> tuple[str | None, list[re.Match]]:
    """Find a recurrence phrase. Returns (rule, matches to remove from the text) or (None, [])."""
    if (m := _EVERY_N_DAYS_RE.search(text)) and _count(m[1]) >= 1:
        n = _count(m[1])
        return ("daily" if n == 1 else f"days:{n}"), [m]
    if m := _EVERY_OTHER_WEEKDAY_RE.search(text):
        return f"weeks:2:{WEEKDAYS.index(m[1].lower())}", [m]
    if (m := _EVERY_N_WEEKS_RE.search(text)) and (m[2] or _count(m[1]) >= 1):
        n = 2 if m[2] else _count(m[1])
        extra: list[re.Match[str]] = []
        if m[3]:
            weekday = WEEKDAYS.index(m[3].lower())
        else:
            weekday, extra = _weekday_elsewhere(text, m, today)
        return (f"weekly:{weekday}" if n == 1 else f"weeks:{n}:{weekday}"), [m, *extra]
    if m := _INTERVAL_RE.search(text):
        minutes = int(m[1]) * (60 if m[2].lower() in ("hour", "hr") else 1)
        return f"interval:{minutes}", [m]
    if m := _WEEKDAYS_RE.search(text):
        return "weekdays", [m]
    if m := _WEEKLY_RE.search(text):
        return f"weekly:{WEEKDAYS.index(m[1].lower())}", [m]
    if m := _DAILY_RE.search(text):
        return "daily", [m]
    if m := _EVERY_WEEK_RE.search(text):
        weekday, extra = _weekday_elsewhere(text, m, today)
        return f"weekly:{weekday}", [m, *extra]
    if m := _MONTHLY_RE.search(text):
        day = int(m[1] or m[2] or m[3])
        if 1 <= day <= 31:
            return f"monthly:{day}", [m]
    if m := _EVERY_MONTH_RE.search(text):
        return f"monthly:{today.day}", [m]
    return None, []


def _month_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_occurrence(rule: str, after: datetime, at: time, anchor: datetime | None = None) -> datetime:
    """The first occurrence of `rule` at wall-clock time `at` strictly after `after`.

    Calendar rules are evaluated in after's timezone, so "every day at 8am" stays at 8am across
    DST changes. Interval rules add real elapsed time. `anchor` is the previous occurrence; the
    every-N-days/weeks rules step from it so they keep their rhythm.
    """
    kind, _, arg = rule.partition(":")
    tz = after.tzinfo
    if kind == "interval":
        return add_elapsed(after, timedelta(minutes=int(arg))).replace(second=0, microsecond=0)

    if kind in ("days", "weeks"):
        n, _, weekday = arg.partition(":")
        step = timedelta(days=int(n)) if kind == "days" else timedelta(weeks=int(n))
        if anchor is None:
            # First occurrence: like a daily or weekly rule; later ones step from it.
            first_rule = "daily" if kind == "days" else f"weekly:{weekday}"
            return next_occurrence(first_rule, after, at)
        start = anchor.astimezone(tz).date()
        k = max(1, (after.date() - start) // step)
        while (candidate := datetime.combine(start + k * step, at, tzinfo=tz)) <= after:
            k += 1
        return candidate

    if kind == "monthly":
        day = int(arg)
        candidate = datetime.combine(_month_day(after.year, after.month, day), at, tzinfo=tz)
        if candidate <= after:
            year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
            candidate = datetime.combine(_month_day(year, month, day), at, tzinfo=tz)
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
        candidate = datetime.combine(d, at, tzinfo=tz)
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
    if kind == "days":
        return "every other day" if arg == "2" else f"every {arg} days"
    if kind == "weeks":
        n, _, weekday = arg.partition(":")
        day = WEEKDAYS[int(weekday)].capitalize()
        return f"every other {day}" if n == "2" else f"every {n} weeks on {day}"
    if kind == "monthly":
        return f"monthly on day {arg}"
    return kind
