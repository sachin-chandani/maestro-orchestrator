"""Quiet hours: hold non-urgent reminders overnight.

    MAESTRO_QUIET_HOURS=22:00-07:00

During the window (local time, MAESTRO_TIMEZONE) due reminders aren't sent, on any channel (retries
included), and escalation waits too. They stay due, so each is sent once when the window ends: an
hourly reminder held overnight sends one notice at 07:00, not eight. Escalation stages that came due
in the window are timed again from its end, so they stay spaced out instead of arriving together.

A reminder is urgent, and ignores quiet hours, when its `urgent` flag is set (#20): the parser sets
it from a leading "!" (e.g. "!take insulin at 11pm") or the word "urgent" (but not "not urgent").
Items stored before the flag existed carry None and are read by those same text rules, so upgrading
never changes which reminders break through.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from maestro.models import Item
from maestro.notifier import ConfigError
from maestro.parser import text_says_urgent

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def is_urgent(item: Item) -> bool:
    if item.urgent is not None:
        return item.urgent
    return text_says_urgent(item.text)  # stored before the flag existed: the text decides


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` (aware, in the user's timezone) falls in the window. The end is exclusive."""
        now = moment.time().replace(second=0, microsecond=0, tzinfo=None)
        if self.start < self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end  # wraps past midnight

    @property
    def length(self) -> timedelta:
        minutes = (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)
        return timedelta(minutes=minutes % (24 * 60))

    def holds(self, item: Item, moment: datetime) -> bool:
        return self.contains(moment) and not is_urgent(item)

    def ends_after(self, moment: datetime) -> datetime:
        """The next end of the window at or after `moment` (when held reminders go out)."""
        end = self._end_on(moment)
        return end if end > moment else end + timedelta(days=1)

    def last_end(self, moment: datetime) -> datetime:
        """The latest end of the window at or before `moment`."""
        end = self._end_on(moment)
        return end if end <= moment else end - timedelta(days=1)

    def _end_on(self, moment: datetime) -> datetime:
        return moment.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)


def parse_quiet_hours(spec: str) -> QuietHours | None:
    """'22:00-07:00' -> QuietHours; '' -> None."""
    spec = spec.strip()
    if not spec:
        return None
    start_text, sep, end_text = (p.strip() for p in spec.partition("-"))
    parts = [_TIME.match(start_text), _TIME.match(end_text)]
    if not sep or not all(parts):
        raise ConfigError(f"MAESTRO_QUIET_HOURS {spec!r} should look like 22:00-07:00")
    start, end = (time(int(m.group(1)), int(m.group(2))) for m in parts if m)
    if start == end:
        raise ConfigError("MAESTRO_QUIET_HOURS start and end must differ")
    return QuietHours(start, end)
