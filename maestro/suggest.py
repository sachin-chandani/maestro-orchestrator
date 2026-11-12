"""Suggested times for a reading that needs one (#128, ADR 0017).

"You said 'sometime next week'" gets three concrete times, each with a reason, worked out from
the vague words the reading kept. The server computes them, so the app, SMS and Telegram can all
offer the same three. Pure: wall-clock rules in the user's timezone, so a suggestion on a DST
weekend still says 09:00; never in the past, and never inside quiet hours (a held suggestion
would just sit until the window ends, so the window's end is offered instead).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from maestro.quiet import QuietHours

#: How many suggestions one question gets. Three reads at a glance; a list is a picker.
COUNT = 3

MORNING = time(9, 0)
EVENING = time(18, 0)
WEEKEND_TIME = time(10, 0)


@dataclass(frozen=True)
class Suggestion:
    at: datetime
    reason: str  # a few words: "start of the week"


# The vague spans the rules keep in the text (maestro.parser masks them from dateparser). Checked
# in order; the first match decides the shape of the suggestions.
_TONIGHT_RE = re.compile(r"\b(?:tonight|tonite|2nite|this\s+evening)\b", re.I)
_SOON_RE = re.compile(r"\b(?:soon|later|asap|in\s+a\s+(?:bit|while)|at\s+some\s+point)\b", re.I)
_WEEK_RE = re.compile(r"\b(?:next|this|the\s+coming)\s+week\b|\beow\b", re.I)
_MONTH_RE = re.compile(r"\b(?:next|this|the\s+coming)\s+month\b|\bend\s+of\s+(?:the\s+)?month\b", re.I)
_WEEKEND_RE = re.compile(r"\bweekend\b", re.I)
_AFTER_WORK_RE = re.compile(r"\bafter\s+(?:work|school|class)\b", re.I)
_MORNING_RE = re.compile(r"\bmorning\b", re.I)
_AFTERNOON_RE = re.compile(r"\bafternoon\b", re.I)
_SOMETIME_RE = re.compile(r"\bsome\s?time\b", re.I)

#: What the person said, for the question's heading: the first vague span found in the text.
_SAID_RE = re.compile(
    r"\b(?:some\s?time\s+(?:next|this|the\s+coming)\s+(?:week|month|year|quarter)"
    r"|(?:next|this|the\s+coming)\s+(?:week|month|year|quarter)"
    r"|end\s+of\s+(?:the\s+)?(?:month|year|week)"
    r"|after\s+(?:work|school|lunch|dinner|class)"
    r"|at\s+some\s+point|some\s?time|soon|later|eventually|asap"
    r"|tonight|tonite|this\s+evening|morning|afternoon|evening|weekend"
    r"|before|until|from|between)\b",
    re.I,
)


def said(text: str) -> str:
    """The vague words the question is about, or the whole text when none stand out."""
    m = _SAID_RE.search(text)
    return m[0] if m else text


def suggest(text: str, now: datetime, quiet: QuietHours | None = None) -> list[Suggestion]:
    """Three times `text`'s vague words could mean, soonest first, all after `now`."""
    candidates = _candidates(text, now)
    # Fallback and padding: tomorrow morning and evening, then the weekend — always sayable.
    tomorrow = now.date() + timedelta(days=1)
    candidates += [
        Suggestion(_at(tomorrow, MORNING, now), "tomorrow morning"),
        Suggestion(_at(tomorrow, EVENING, now), "tomorrow evening"),
        Suggestion(_at(_next_weekday(now.date(), 5), WEEKEND_TIME, now), "the weekend"),
    ]
    chosen: list[Suggestion] = []
    for candidate in candidates:
        at = _outside_quiet(candidate.at, quiet)
        if at <= now or any(c.at == at for c in chosen):
            continue
        chosen.append(Suggestion(at, candidate.reason))
        if len(chosen) == COUNT:
            break
    return sorted(chosen, key=lambda s: s.at)


def _candidates(text: str, now: datetime) -> list[Suggestion]:
    today = now.date()
    if _TONIGHT_RE.search(text):
        evening = [Suggestion(_at(today, time(h, 0), now), "tonight") for h in (19, 20, 21)]
        if all(s.at <= now for s in evening):  # said late at night: tomorrow evening, then
            evening = [Suggestion(_at(today + timedelta(days=1), time(h, 0), now), "tomorrow evening")
                       for h in (19, 20, 21)]
        return evening
    if _WEEK_RE.search(text):
        monday = _next_weekday(today, 0)
        return [
            Suggestion(_at(monday, MORNING, now), "start of the week"),
            Suggestion(_at(monday, EVENING, now), "start of the week, evening"),
            Suggestion(_at(_next_weekday(monday, 5), WEEKEND_TIME, now), "the weekend"),
        ]
    if _MONTH_RE.search(text):
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        mid = today.replace(day=15) if today.day < 15 else first_next.replace(day=15)
        return [
            Suggestion(_at(_next_weekday(today, 0), MORNING, now), "the coming Monday"),
            Suggestion(_at(mid, MORNING, now), "mid-month"),
            Suggestion(_at(first_next, MORNING, now), "the 1st"),
        ]
    if _WEEKEND_RE.search(text):
        saturday = _next_weekday(today, 5)
        return [
            Suggestion(_at(saturday, WEEKEND_TIME, now), "Saturday morning"),
            Suggestion(_at(saturday, time(16, 0), now), "Saturday afternoon"),
            Suggestion(_at(saturday + timedelta(days=1), WEEKEND_TIME, now), "Sunday morning"),
        ]
    if _AFTER_WORK_RE.search(text):
        first = today if _at(today, EVENING, now) > now else today + timedelta(days=1)
        return [
            Suggestion(_at(first, EVENING, now), "after work"),
            Suggestion(_at(first + timedelta(days=1), EVENING, now), "the day after"),
            Suggestion(_at(_next_weekday(today, 4), EVENING, now), "Friday evening"),
        ]
    if _MORNING_RE.search(text):
        day = today if _at(today, MORNING, now) > now else today + timedelta(days=1)
        return [Suggestion(_at(day, time(h, 0), now), "the morning") for h in (8, 9, 10)]
    if _AFTERNOON_RE.search(text):
        day = today if _at(today, time(14, 0), now) > now else today + timedelta(days=1)
        return [Suggestion(_at(day, time(h, 0), now), "the afternoon") for h in (14, 15, 16)]
    if _SOON_RE.search(text) or _SOMETIME_RE.search(text):
        in_an_hour = (now + timedelta(hours=1)).replace(second=0, microsecond=0)
        in_an_hour -= timedelta(minutes=in_an_hour.minute % 5)  # on a whole five minutes
        return [
            Suggestion(in_an_hour, "in an hour"),
            Suggestion(_at(today, EVENING, now), "this evening"),
            Suggestion(_at(today + timedelta(days=1), MORNING, now), "tomorrow morning"),
        ]
    return []


def _at(day: date, at: time, now: datetime) -> datetime:
    """The wall-clock moment `day` at `at` in now's zone: 09:00 stays 09:00 across a DST change."""
    return datetime.combine(day, at, tzinfo=now.tzinfo)


def _next_weekday(after: date, weekday: int) -> date:
    """The next `weekday` (0 = Monday) strictly after `after`."""
    ahead = (weekday - after.weekday()) % 7 or 7
    return after + timedelta(days=ahead)


def _outside_quiet(at: datetime, quiet: QuietHours | None) -> datetime:
    """A suggestion inside quiet hours would just be held; offer when it would really go out."""
    if quiet is None or not quiet.contains(at):
        return at
    return quiet.ends_after(at)
