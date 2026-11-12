"""The upcoming feed: one call for everything the list screen shows (#22, ADR 0017).

The app is one list of reminders ordered by time. This module builds its data: every occurrence
of every pending reminder in the next few days — a recurring series expanded to its occurrences,
a snoozed one at its snoozed time, a delivered one that nobody has answered at the time it went
out — plus the tasks (reminders with no time, shown under Anytime) and the readings that still
need a look (#126). The server reports times and states; the client groups them into days in the
user's timezone, so the list moves on at midnight without asking the server again.

Pure: nothing here writes, and every judgement is the scheduler's own. A reminder is *held* here
exactly when `maestro.quiet` would hold it; it *escalates* when `maestro.escalation`'s stages
say so. Only the first occurrence of a series takes Done and +1 hour (ADR 0009: they act on the
next occurrence); later ones are shown so the week reads right, and say so with `acts=False`.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from maestro import recurrence
from maestro.escalation import Stage, is_escalation_key
from maestro.models import Item, Kind, Status
from maestro.quiet import QuietHours, is_urgent
from maestro.storage import Store

#: How many days the feed covers by default, and at most. A week reads at a glance; a month does not.
DEFAULT_DAYS = 7
MAX_DAYS = 31
#: One series never fills the week on its own: at most one row per day, and this many rows in all.
MAX_PER_SERIES = 31

STATES = ("scheduled", "overdue", "awaiting", "held", "snoozed")

# How a channel is named in an escalation label ("text at 17:12"), matching the app's chips.
_CHANNEL_WORDS = {"twilio_sms": "text", "twilio_call": "call", "email": "email",
                  "telegram": "Telegram", "ntfy": "push", "webpush": "push", "desktop": "desktop",
                  "webhook": "webhook", "console": "console", "macos": "desktop"}


@dataclass(frozen=True)
class Occurrence:
    """One time a reminder goes (or went) out, as a row in the list."""

    item: Item
    at: datetime  # when it goes out: the snoozed time, the due time, or the delivered occurrence
    state: str  # one of STATES
    held_until: datetime | None = None  # when quiet hours let it out, while `state` is "held"
    escalation_label: str | None = None  # "text at 17:12": the next stage, while awaiting an answer
    acts: bool = True  # Done and +1 hour apply to this row (the first occurrence of its series)


@dataclass
class Feed:
    now: datetime
    until: datetime  # the end of the window, exclusive
    needs_look: list[Item] = field(default_factory=list)
    upcoming: list[Occurrence] = field(default_factory=list)  # by `at`
    anytime: list[Item] = field(default_factory=list)  # tasks, oldest first
    later: int = 0  # reminders whose next time is past the window; none of their rows are listed


def window_end(now: datetime, days: int) -> datetime:
    """Midnight at the start of the day `days` from today, in `now`'s zone: the window covers
    today and the `days - 1` days after it, whole, whatever DST does in between."""
    first_day_out: date = now.date() + timedelta(days=days)
    return datetime.combine(first_day_out, time(0), tzinfo=now.tzinfo)


def build(store: Store, now: datetime | None = None, days: int = DEFAULT_DAYS,
          quiet: QuietHours | None = None, escalation: list[Stage] | None = None) -> Feed:
    """The feed as of `now` (default: now, in the store's timezone)."""
    now = (now or datetime.now(store.tz)).astimezone(store.tz).replace(microsecond=0)
    days = max(1, min(days, MAX_DAYS))
    feed = Feed(now=now, until=window_end(now, days))
    feed.needs_look = store.needs_look()
    awaiting = {item.id: occurrence for item, occurrence in store.unacknowledged()}

    for item in store.list():
        if item.gap is not None or item.status == Status.DONE:
            continue  # needing a look is listed above; done is history
        if item.kind == Kind.TASK:
            feed.anytime.append(item)
        elif item.kind == Kind.REMINDER and item.due_at is not None:
            rows = list(_occurrences(store, item, awaiting.get(item.id), feed, quiet, escalation))
            if rows:
                feed.upcoming.extend(rows)
            else:
                feed.later += 1
    feed.anytime.sort(key=lambda i: (i.created_at, i.id or 0))
    feed.upcoming.sort(key=lambda o: (o.at, o.item.id or 0))
    return feed


def _occurrences(store: Store, item: Item, delivered: datetime | None, feed: Feed,
                 quiet: QuietHours | None, escalation: list[Stage] | None) -> Iterator[Occurrence]:
    """The rows one reminder puts in the window, first occurrence first."""
    assert item.due_at is not None
    if delivered is not None:
        # Delivered and not yet answered: it is "now" to the person however long ago it went out,
        # and Done or +1 hour act on that occurrence (ADR 0009).
        yield Occurrence(item, delivered.astimezone(feed.now.tzinfo), "awaiting",
                         escalation_label=_escalation_label(store, item, delivered, feed.now,
                                                            quiet, escalation))
        acts = False  # the row above owns the buttons
    else:
        acts = True
    first = item.snoozed_until or item.due_at
    seen_days: set[date] = set()
    count = 0
    for at in _times(item, first):
        if at >= feed.until:
            return
        if at.date() in seen_days:
            continue  # one row per day per series
        if delivered is not None and at <= delivered:
            continue  # already out: the awaiting row covers it
        seen_days.add(at.date())
        state, held_until = _state(item, at, feed.now, quiet, snoozed=(at == item.snoozed_until))
        yield Occurrence(item, at, state, held_until=held_until, acts=acts)
        acts = False
        count += 1
        if count >= MAX_PER_SERIES:
            return


def _times(item: Item, first: datetime) -> Iterator[datetime]:
    """The occurrences of a reminder from `first` on: one for a one-off, the series otherwise."""
    assert item.due_at is not None
    yield first
    if not item.recurrence:
        return
    at = item.due_at
    if first != at:
        yield at  # a snoozed re-send comes first; the series' own next time follows (ADR 0009)
    while True:
        at = recurrence.next_occurrence(item.recurrence, at, item.due_at.time(), anchor=at)
        yield at


def _state(item: Item, at: datetime, now: datetime, quiet: QuietHours | None,
           snoozed: bool) -> tuple[str, datetime | None]:
    """What this occurrence is doing, in the scheduler's terms."""
    if quiet is not None and not is_urgent(item) and quiet.contains(at.astimezone(now.tzinfo)):
        return "held", quiet.ends_after(at.astimezone(now.tzinfo))
    if at <= now:
        return "overdue", None  # due and not sent yet: the scheduler is about to, or is behind
    if snoozed:
        return "snoozed", None
    return "scheduled", None


def _escalation_label(store: Store, item: Item, occurrence: datetime, now: datetime,
                      quiet: QuietHours | None, stages: list[Stage] | None) -> str | None:
    """"text at 17:12": the next escalation stage still to go out for a delivered occurrence, timed
    the way `maestro.escalation` times it (from the first notice, or from the end of quiet hours)."""
    if not stages or item.id is None:
        return None
    state = store.deliveries(item.id, occurrence)
    first_notice = min((d.delivered_at for key, d in state.items()
                        if not is_escalation_key(key) and d.delivered_at), default=None)
    if first_notice is None:
        return None
    start = first_notice
    if quiet is not None and not is_urgent(item):
        resume = quiet.last_end(now.astimezone(store.tz))
        start = max(first_notice, resume)
    for stage in stages:
        if all((d := state.get(key)) and d.delivered_at for key in stage.channels):
            continue  # this stage already went out
        at = (start + stage.delay).astimezone(store.tz)
        names = ", ".join(_CHANNEL_WORDS.get(name, name) for name in stage.names)
        return f"{names} at {at:%H:%M}"
    return None
