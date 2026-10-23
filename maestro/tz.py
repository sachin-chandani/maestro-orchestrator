"""Timezone handling.

The database stores every timestamp in UTC. Parsing ("tomorrow at 5pm"), recurrence and display
happen in the user's timezone, so a reminder keeps its wall-clock time across DST changes and a
server running in UTC (e.g. Docker) still fires "5pm" at the user's 5pm.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["UTC", "get_timezone", "localize", "add_elapsed"]


def _system_zone_name() -> str | None:
    if tz := os.environ.get("TZ", "").lstrip(":"):
        return tz
    # macOS and most Linux distros symlink /etc/localtime into the zoneinfo database.
    target = str(Path("/etc/localtime").resolve())
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    return None


def get_timezone(name: str | None = None) -> ZoneInfo:
    """Resolve the user's timezone: explicit name, then MAESTRO_TIMEZONE, then the system zone, then UTC."""
    name = name or os.environ.get("MAESTRO_TIMEZONE") or _system_zone_name() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown timezone {name!r}; use an IANA name like 'Europe/London'") from e


def localize(dt: datetime, tz: ZoneInfo) -> datetime:
    """Express dt in tz. Naive datetimes are taken to already be wall-clock time in tz."""
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def add_elapsed(dt: datetime, delta: timedelta) -> datetime:
    """Add real elapsed time (not wall-clock time), so "in 2 hours" stays 2 hours across a DST change."""
    return (dt.astimezone(UTC) + delta).astimezone(dt.tzinfo)
