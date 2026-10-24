# 0001: Store timestamps in UTC, compute in the user's timezone

**Status:** Accepted (#1)

## Context
Early versions stored naive local time. Inside Docker the process clock is UTC, so "5pm" fired at 5pm UTC, and a server moving between zones (or DST) shifted every reminder.

## Decision
- The database stores ISO-8601 UTC strings (`2026-10-24T21:00:00+00:00`), so string order is time order and indexes work.
- `Store` returns aware datetimes in the configured zone (`MAESTRO_TIMEZONE`, else the system zone).
- Calendar rules ("every day at 8am") are computed in wall-clock time in that zone; elapsed-time phrases ("in 2 hours", "every 2 hours") add real time via `tz.add_elapsed`.
- Migration 4 converted old rows by interpreting them in the configured zone.

## Consequences
- Every new timestamp column must go through `Store._to_db` / `_from_db`.
- Tests pin `America/New_York` so DST transitions are exercised.
- Changing `MAESTRO_TIMEZONE` later keeps instants fixed and moves wall-clock display, which is what a travelling user expects for one-off reminders. Recurring rules keep their original wall-clock time.
