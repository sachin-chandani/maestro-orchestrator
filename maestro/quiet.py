"""Quiet hours: hold non-urgent reminders overnight.

    MAESTRO_QUIET_HOURS=22:00-07:00

During the window (local time, MAESTRO_TIMEZONE) due reminders aren't sent, on any channel, and
escalation waits too. They stay due, so each is sent once when the window ends: an hourly reminder
held overnight sends one notice at 07:00, not eight.

A reminder is urgent, and ignores quiet hours, when its text starts with "!" or contains the word
"urgent", e.g. "!take insulin at 11pm".
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol

from maestro.notifier import ConfigError

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_URGENT = re.compile(r"\burgent\b", re.IGNORECASE)


class HasText(Protocol):
    text: str


def is_urgent(item: HasText) -> bool:
    return item.text.lstrip().startswith("!") or bool(_URGENT.search(item.text))


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

    def holds(self, item: HasText, moment: datetime) -> bool:
        return self.contains(moment) and not is_urgent(item)

    def ends_after(self, moment: datetime) -> datetime:
        """The wall-clock end of the window that `moment` is in (for messages and the API)."""
        end = moment.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        return end if end > moment else end + timedelta(days=1)


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
