"""Turn plain-language captures into items.

Times, relative offsets and weekdays are handled with explicit rules because
dateparser misreads common phrases ("at 7am" becomes July, "next friday" a year out).
dateparser is only used for calendar dates such as "Nov 1" or "2027-03-15".
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from dateparser.search import search_dates

from maestro import recurrence
from maestro.models import Item, Kind

DEFAULT_TIME = time(9, 0)
TONIGHT_TIME = time(20, 0)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remind me(?:\s+(?:to|that|about))?|remember(?:\s+(?:to|that))?"
    r"|don'?t forget(?:\s+(?:to|that))?)\s+",
    re.I,
)
TIME_12H_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
TIME_24H_RE = re.compile(r"\bat\s+([01]?\d|2[0-3]):([0-5]\d)\b", re.I)
NOON_RE = re.compile(r"\b(?:at\s+)?(noon|midnight)\b", re.I)
RELATIVE_RE = re.compile(
    r"\bin\s+(\d+|an?|one)\s+(minute|min|hour|hr|day|week)s?\b", re.I
)
DAY_WORD_RE = re.compile(r"\b(today|tonight|tomorrow)\b", re.I)
WEEKDAY_RE = re.compile(r"\b(?:(?:next|this|on)\s+)?(" + "|".join(WEEKDAYS) + r")\b", re.I)

UNIT_DELTAS = {
    "minute": timedelta(minutes=1), "min": timedelta(minutes=1),
    "hour": timedelta(hours=1), "hr": timedelta(hours=1),
    "day": timedelta(days=1), "week": timedelta(weeks=1),
}


@dataclass
class _Parts:
    text: str
    when_date: date | None = None
    when_time: time | None = None
    exact: datetime | None = None


def _cut(parts: _Parts, match: re.Match) -> None:
    parts.text = parts.text[: match.start()] + " " + parts.text[match.end():]


def _extract_time(parts: _Parts) -> None:
    if m := TIME_12H_RE.search(parts.text):
        hour, minute = int(m[1]) % 12, int(m[2] or 0)
        if m[3].lower() == "pm":
            hour += 12
        if hour < 24 and minute < 60:
            parts.when_time = time(hour, minute)
            _cut(parts, m)
    elif m := TIME_24H_RE.search(parts.text):
        parts.when_time = time(int(m[1]), int(m[2]))
        _cut(parts, m)
    elif m := NOON_RE.search(parts.text):
        parts.when_time = time(12, 0) if m[1].lower() == "noon" else time(0, 0)
        _cut(parts, m)


def _extract_relative(parts: _Parts, now: datetime) -> None:
    if not (m := RELATIVE_RE.search(parts.text)):
        return
    count = 1 if m[1].lower() in ("a", "an", "one") else int(m[1])
    unit = m[2].lower()
    target = now + count * UNIT_DELTAS[unit]
    if unit in ("day", "week"):
        parts.when_date = target.date()
    else:
        parts.exact = target.replace(second=0, microsecond=0)
    _cut(parts, m)


def _extract_day(parts: _Parts, now: datetime) -> None:
    if m := DAY_WORD_RE.search(parts.text):
        word = m[1].lower()
        parts.when_date = now.date() + timedelta(days=1 if word == "tomorrow" else 0)
        if word == "tonight" and parts.when_time is None:
            parts.when_time = TONIGHT_TIME
        _cut(parts, m)
    elif m := WEEKDAY_RE.search(parts.text):
        # Always the next occurrence after today, so "friday" said on a Friday means next week.
        ahead = (WEEKDAYS.index(m[1].lower()) - now.weekday()) % 7 or 7
        parts.when_date = now.date() + timedelta(days=ahead)
        _cut(parts, m)
    else:
        found = search_dates(
            parts.text,
            languages=["en"],
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now,
                "REQUIRE_PARTS": ["day", "month"],
            },
        )
        if found:
            phrase, dt = found[0]
            parts.when_date = dt.date()
            start = parts.text.index(phrase)
            parts.text = parts.text[:start] + " " + parts.text[start + len(phrase):]


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?:\s+\b(?:on|at|by)\b)+$", "", text, flags=re.I)
    return text.strip(" ,.;:-")


DURATION_RE = re.compile(r"^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.I)


def parse_duration(text: str) -> timedelta:
    """Parse a snooze length like "10m", "2h" or "1 day"."""
    if not (m := DURATION_RE.match(text)):
        raise ValueError(f"Can't read duration {text!r}; try 10m, 2h or 1d")
    unit = m[2][0].lower()
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: int(m[1])})


def parse(source: str, now: datetime | None = None) -> Item:
    """Parse a capture like "remind me to call mom tomorrow at 5pm" into an Item."""
    now = now or datetime.now()
    parts = _Parts(text=PREFIX_RE.sub("", source, count=1))

    rule, m = recurrence.extract(parts.text, now.date())
    if m:
        _cut(parts, m)
        _extract_time(parts)
        at = parts.when_time or DEFAULT_TIME
        parts.exact = recurrence.next_occurrence(rule, now, at)
    else:
        _extract_relative(parts, now)
        _extract_time(parts)
        if parts.exact is None:
            _extract_day(parts, now)

    due_at = parts.exact
    if due_at is None and (parts.when_date or parts.when_time):
        due_at = datetime.combine(parts.when_date or now.date(), parts.when_time or DEFAULT_TIME)
        # A bare time that has already passed today means tomorrow.
        if parts.when_date is None and due_at <= now:
            due_at += timedelta(days=1)

    text = _clean(parts.text) or source.strip()
    return Item(
        text=text[0].upper() + text[1:],
        kind=Kind.REMINDER if due_at else Kind.NOTE,
        created_at=now.replace(microsecond=0),
        due_at=due_at,
        source_text=source,
        recurrence=rule,
    )
