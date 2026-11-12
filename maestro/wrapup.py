"""The evening wrap-up (#128, ADR 0017): one notice a day, only when something still needs a look.

Opt-in with `MAESTRO_WRAPUP=20:00` (local time, `MAESTRO_TIMEZONE`). From that moment until
midnight, the first scheduler tick to claim the day — `store.claim_daily` is atomic, so several
processes send one notice between them, and a restart can't repeat it — sends one message on the
default channels: how many readings are waiting and what the first few say. Nothing is sent on a
day with nothing waiting, and the claim is only taken when a notice actually goes, so a reading
captured at 23:00 still gets that evening's wrap-up.
"""

import logging
import re
from dataclasses import replace
from datetime import datetime, time

from maestro.models import Item, Kind
from maestro.notifier import ConfigError, Notifier
from maestro.observability import METRICS
from maestro.storage import Store

log = logging.getLogger(__name__)

MARK = "wrapup"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
#: How many waiting readings the notice names before saying "and N more".
NAMED = 3


def parse_wrapup(spec: str) -> time | None:
    """MAESTRO_WRAPUP as a local time, None when unset. Anything unreadable is a config error."""
    if not spec.strip():
        return None
    if not (m := _TIME_RE.match(spec.strip())):
        raise ConfigError(f"MAESTRO_WRAPUP must be a time like 20:00, not {spec!r}")
    return time(int(m[1]), int(m[2]))


def wording(waiting: list[Item]) -> str:
    """One sentence: "2 reminders still need a look: Call the plumber; Book the mot." """
    count = len(waiting)
    names = "; ".join(item.text for item in waiting[:NAMED])
    more = f" and {count - NAMED} more" if count > NAMED else ""
    noun = "reminder still needs" if count == 1 else "reminders still need"
    return f"{count} {noun} a look: {names}{more}. Reply with a time, or open the app."


def maybe_send(store: Store, notifier: Notifier, at: time, now: datetime | None = None) -> bool:
    """Send today's wrap-up if it is due, hasn't gone yet, and something is waiting."""
    now = (now or datetime.now(store.tz)).astimezone(store.tz)
    if now.time() < at:
        return False
    waiting = store.needs_look()
    if not waiting:
        return False
    if not store.claim_daily(MARK, now.date()):
        return False  # today's already went, from this process or another
    notice = replace(waiting[0], text=wording(waiting), kind=Kind.REMINDER, due_at=now)
    try:
        notifier.send(notice)
    except Exception:
        log.exception("The evening wrap-up failed to send; it will not retry until tomorrow")
        METRICS.inc("maestro_wrapup_total", result="failed")
        return False
    METRICS.inc("maestro_wrapup_total", result="sent")
    log.info("Evening wrap-up sent: %d reading(s) still need a look", len(waiting))
    return True
