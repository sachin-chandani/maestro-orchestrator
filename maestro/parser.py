"""Turn plain-language captures into items.

Times, relative offsets and weekdays are handled with explicit rules because
dateparser misreads common phrases ("at 7am" becomes July, "next friday" a year out).
dateparser is only used for calendar dates such as "Nov 1" or "2027-03-15".
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from dateparser.search import search_dates

from maestro import recurrence
from maestro.models import Item, Kind
from maestro.tz import add_elapsed, get_timezone, localize

DEFAULT_TIME = time(9, 0)
TONIGHT_TIME = time(20, 0)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remind me(?:\s+(?:to|that|about))?|remember(?:\s+(?:to|that))?"
    r"|don'?t forget(?:\s+(?:to|that))?)\s+",
    re.I,
)
# "at", "around", "after", "by" lead a time and leave the text with it. ("from" and "before" are not
# here: ranges and lead times aren't modelled, see KNOWN_GAPS in tests/test_corpus.py.)
_TIME_LEAD = r"(?:(?:at|around|about|after|by)\s+)"
TIME_12H_RE = re.compile(rf"\b{_TIME_LEAD}?(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)\b", re.I)
TIME_24H_RE = re.compile(rf"\b{_TIME_LEAD}([01]?\d|2[0-3]):([0-5]\d)\b", re.I)
# "at 6" with no am/pm. Needs "at", and must be followed by the end of the text, punctuation or a
# word that continues a time phrase, so "at 5 main street" and "2 cups" stay text.
_TIME_FOLLOWERS = "|".join(["today", "tomorrow", "tonight", "on", "this", "next", "in", "every", "and",
                            "then", "for", "with", "morning", "afternoon", "evening", "night", *WEEKDAYS])
TIME_BARE_RE = re.compile(
    rf"\b{_TIME_LEAD}(1[0-2]|[1-9])(?:\s*o'?clock)?(?=\s*(?:$|[,.;!?)]|(?:{_TIME_FOLLOWERS})\b))", re.I
)
_PM_CONTEXT_RE = re.compile(r"\b(?:tonight|evening|night|afternoon|pm)\b", re.I)
_AM_CONTEXT_RE = re.compile(r"\b(?:morning|am)\b", re.I)


def _conversational_hour(hour: int, text: str) -> int:
    """An hour said without am/pm. Context wins ("8 tonight", "6 in the morning"); otherwise 7-11 is
    morning, 12 is noon and 1-6 is afternoon/evening, the way people use it in conversation."""
    if _PM_CONTEXT_RE.search(text):
        return hour % 12 + 12
    if _AM_CONTEXT_RE.search(text):
        return hour % 12
    return hour if 7 <= hour <= 12 else hour + 12
MERIDIEM_RE = re.compile(r"\b([ap])\.\s?m\.?(?=\s|$|[,;!?)])", re.I)
NOON_RE = re.compile(r"\b(?:at\s+)?(noon|midnight)\b", re.I)
RELATIVE_RE = re.compile(
    r"\bin\s+(\d+|an?|one)\s+(minute|min|hour|hr|day|week)s?\b", re.I
)
DAY_WORD_RE = re.compile(r"\b(today|tonight|tomorrow)\b", re.I)
WEEKDAY_RE = re.compile(r"\b(?:(?:next|this|on)\s+)?(" + "|".join(WEEKDAYS) + r")\b", re.I)
PART_OF_DAY = {"morning": time(9, 0), "afternoon": time(14, 0), "evening": time(18, 0), "night": time(21, 0)}
_PARTS = "morning|afternoon|evening|night"
_DAY_WORDS = "|".join(["today", "tomorrow", "tonight", "weekend", *WEEKDAYS])
# Only as a time when the phrase says so, so "movie night saturday" keeps its name:
#   "this evening", "in the morning", "at night"  -> the whole phrase is removed
#   "tomorrow night", "friday morning"           -> only the part of day; the day word stays for later
# In a recurrence, a part of the day right after the rule counts: "every night", "every thursday night".
PART_OF_DAY_RE = re.compile(
    rf"\b(?:(?:this|in\s+the|at)\s+(?P<qualified>{_PARTS})"
    rf"|(?:{_DAY_WORDS})\s+(?P<after_day>{_PARTS}))\b",
    re.I,
)
_PART_RIGHT_AFTER_RE = re.compile(rf"\s+({_PARTS})\b", re.I)
END_OF_DAY_RE = re.compile(r"\b(?:by\s+)?(?:the\s+)?(?:end\s+of\s+(?:the\s+)?day|eod)\b", re.I)
END_OF_WEEK_RE = re.compile(r"\b(?:by\s+)?(?:the\s+)?(?:end\s+of\s+(?:the\s+)?week|eow)\b", re.I)
END_OF_DAY = time(17, 0)
WEEKEND_RE = re.compile(r"\b(?:this\s+|the\s+|on\s+the\s+)?weekend\b", re.I)
WEEKEND_TIME = time(10, 0)
# Needs "on"/"by" so "the 5th element" stays text.
DAY_OF_MONTH_RE = re.compile(r"\b(?:on|by)\s+the\s+(\d{1,2})(?:st|nd|rd|th)\b", re.I)

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
        hour = int(m[1])
        # 13:00-23:59 and 00:xx are unambiguous; "6:30" is read like "at 6" (so it isn't 12 h off).
        if 1 <= hour <= 12:
            hour = _conversational_hour(hour, parts.text)
        parts.when_time = time(hour, int(m[2]))
        _cut(parts, m)
    elif m := NOON_RE.search(parts.text):
        parts.when_time = time(12, 0) if m[1].lower() == "noon" else time(0, 0)
        _cut(parts, m)
    elif m := TIME_BARE_RE.search(parts.text):
        parts.when_time = time(_conversational_hour(int(m[1]), parts.text), 0)
        _cut(parts, m)
    _extract_part_of_day(parts)


def _extract_part_of_day(parts: _Parts) -> None:
    """"tomorrow morning" sets a time; "tomorrow morning at 8am" keeps 8am but still drops the word."""
    if not (m := PART_OF_DAY_RE.search(parts.text)):
        return
    name = next(group for group, value in m.groupdict().items() if value)
    parts.when_time = parts.when_time or PART_OF_DAY[m[name].lower()]
    start = m.start(name) if name == "after_day" else m.start()
    parts.text = parts.text[:start] + " " + parts.text[m.end(name):]


def _extract_deadline(parts: _Parts, now: datetime) -> None:
    """End of day is 17:00; end of week is Friday 17:00 (next Friday once the weekend has started)."""
    if m := END_OF_WEEK_RE.search(parts.text):
        parts.when_time = parts.when_time or END_OF_DAY
        ahead = (4 - now.weekday()) % 7
        if ahead == 0 and parts.when_time <= now.time():
            ahead = 7  # said on Friday after the deadline: next Friday
        parts.when_date = now.date() + timedelta(days=ahead)
        _cut(parts, m)
    elif m := END_OF_DAY_RE.search(parts.text):
        parts.when_time = parts.when_time or END_OF_DAY
        _cut(parts, m)


def _extract_relative(parts: _Parts, now: datetime) -> None:
    if not (m := RELATIVE_RE.search(parts.text)):
        return
    count = 1 if m[1].lower() in ("a", "an", "one") else int(m[1])
    unit = m[2].lower()
    if unit in ("day", "week"):
        parts.when_date = (now + count * UNIT_DELTAS[unit]).date()
    else:
        parts.exact = add_elapsed(now, count * UNIT_DELTAS[unit]).replace(second=0, microsecond=0)
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
    elif m := WEEKEND_RE.search(parts.text):
        # The next weekend slot that hasn't passed: Saturday, then Sunday, then the following Saturday.
        parts.when_time = parts.when_time or WEEKEND_TIME
        slot = now.date()
        while slot.weekday() < 5 or datetime.combine(slot, parts.when_time, tzinfo=now.tzinfo) <= now:
            slot += timedelta(days=1)
        parts.when_date = slot
        _cut(parts, m)
    elif (m := DAY_OF_MONTH_RE.search(parts.text)) and 1 <= int(m[1]) <= 31:
        # The next "5th" after today, clamped to short months ("the 31st" in November is the 30th).
        day = int(m[1])
        year, month = now.year, now.month
        if day <= now.day:
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        parts.when_date = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        _cut(parts, m)
    else:
        _extract_calendar_date(parts, now)


def _mask_non_year_numbers(text: str) -> str:
    """Hide long numbers that can't be a year ("form 1040", "0100") from dateparser, which would
    otherwise read them as one. Same length, so positions still line up with the original."""
    return re.sub(r"\b\d{3,}\b", lambda m: m[0] if 1900 <= int(m[0]) <= 2099 else "#" * len(m[0]), text)


def _extract_calendar_date(parts: _Parts, now: datetime) -> None:
    masked = _mask_non_year_numbers(parts.text)
    for _ in range(3):  # each retry hides one more number that was mistaken for a year
        retry = _find_calendar_date(parts, masked, now)
        if retry is None:
            return
        masked = retry


def _find_calendar_date(parts: _Parts, masked: str, now: datetime) -> str | None:
    """Apply the first plausible date found in `masked`. Returns a new mask to retry with when the
    only candidates had implausible years ("form 1999 by april 15"), otherwise None."""
    found = search_dates(
        masked,
        languages=["en"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now.replace(tzinfo=None),
            "REQUIRE_PARTS": ["day", "month"],
        },
    )
    retry = None
    for phrase, dt in found or []:
        if not now.year - 1 <= dt.year <= now.year + 50:
            # A number was read as the year: hide it and try again.
            year = re.search(rf"\b0*{dt.year}\b", masked)
            if year and retry is None:
                retry = masked[:year.start()] + "#" * len(year[0]) + masked[year.end():]
            continue
        # dateparser can return the phrase with its whitespace normalised (tabs, form feeds), so
        # locate it with flexible whitespace rather than an exact substring search.
        pattern = r"\s+".join(re.escape(word) for word in phrase.split())
        if not pattern or not (m := re.search(pattern, masked)):
            continue
        parts.when_date = dt.date()
        parts.text = parts.text[:m.start()] + " " + parts.text[m.end():]
        return None
    return retry


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?:\s+\b(?:on|at|by)\b)+$", "", text, flags=re.I)
    return text.strip(" ,.;:-")


DURATION_UNITS = r"(?:m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)"
DURATION_RE = re.compile(rf"^\s*(\d+)\s*({DURATION_UNITS})\s*$", re.I)


def parse_duration(text: str) -> timedelta:
    """Parse a snooze length like "10m", "2h" or "1 day"."""
    if not (m := DURATION_RE.match(text)):
        raise ValueError(f"Can't read duration {text!r}; try 10m, 2h or 1d")
    unit = m[2][0].lower()
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: int(m[1])})


def parse(source: str, now: datetime | None = None, tz: tzinfo | None = None) -> Item:
    """Parse a capture like "remind me to call mom tomorrow at 5pm" into an Item.

    Times are read as wall-clock time in `tz` (default: the configured user timezone) and the
    resulting datetimes are timezone-aware. A naive `now` is taken to be in `tz`.
    """
    zone: tzinfo = tz or (now.tzinfo if now and now.tzinfo else None) or get_timezone()
    now = localize(now, zone) if now else datetime.now(zone)
    prefix = PREFIX_RE.match(source)
    # "remind me in 15 minutes to check the oven": the prefix had no connector, so once the time is
    # removed a leading "to" is left over. ("remind me about that email" keeps "that".)
    strip_connector = prefix is not None and not re.search(r"\b(?:to|that|about)\s*$", prefix[0], re.I)
    # Dictation and careful typists write "5 p.m."; the rules below expect "5 pm".
    text = MERIDIEM_RE.sub(lambda m: f"{m[1]}m", source)
    parts = _Parts(text=PREFIX_RE.sub("", text, count=1))

    rule, matches = recurrence.extract(parts.text, now.date())
    if rule:
        spans = [(m.start(), m.end()) for m in matches]
        for m in matches:
            if after := _PART_RIGHT_AFTER_RE.match(parts.text, m.end()):
                parts.when_time = PART_OF_DAY[after[1].lower()]
                spans.append((after.start(), after.end()))
        # Cut from the end so earlier positions stay valid.
        for start, end in sorted(spans, reverse=True):
            parts.text = parts.text[:start] + " " + parts.text[end:]
        _extract_time(parts)
        at = parts.when_time or DEFAULT_TIME
        parts.exact = recurrence.next_occurrence(rule, now, at)
    else:
        _extract_relative(parts, now)
        _extract_time(parts)
        _extract_deadline(parts, now)
        if parts.exact is None and parts.when_date is None:
            _extract_day(parts, now)

    due_at = parts.exact
    if due_at is None and (parts.when_date or parts.when_time):
        due_at = datetime.combine(parts.when_date or now.date(), parts.when_time or DEFAULT_TIME, tzinfo=zone)
        # One rule for every phrase that lands on today ("at 9am", "today", "tonight", "eod"): a time
        # that has already passed means the same time tomorrow, so a new reminder is never overdue.
        if due_at <= now and (parts.when_date is None or parts.when_date == now.date()):
            due_at += timedelta(days=1)

    text = _clean(parts.text)
    if strip_connector:
        text = re.sub(r"^(?:to|that|about)\s+", "", text, flags=re.I)
    text = text or source.strip()
    return Item(
        text=text[0].upper() + text[1:],
        kind=Kind.REMINDER if due_at else Kind.NOTE,
        created_at=now.replace(microsecond=0),
        due_at=due_at,
        source_text=source,
        recurrence=rule,
    )
