# 0009: Done and snooze on a recurring reminder apply to one occurrence

**Status:** Accepted (#80)

## Context
A recurring reminder was a single row, and `due_at` was its next occurrence. Done set the whole row to `done`, so replying "done" to today's "take pills" stopped every later day. Snooze overwrote `due_at`, and later occurrences are computed from `due_at`'s wall-clock time, so a daily 09:00 reminder snoozed by 10 minutes moved to 09:10 for good. Once there was escalation (#12) and phone calls (#11), "done" became the normal reply to a daily reminder, which made both bugs serious.

## Decision
- **Done** on a recurring reminder is about one occurrence. If something was delivered within the acknowledgement window, done acknowledges it and drops any pending snooze of it. Otherwise it skips the upcoming occurrence ("I took them early"). The series keeps running.
- **Stop** (`maestro stop`, `stop <id>` in chat, `POST /items/{id}/stop`, and a web UI button) ends a series. It always needs an explicit item number. Delete still works too.
- **Snooze** on a recurring reminder sets `snoozed_until` (migration 8) and leaves `due_at` on the series' time. The scheduler sends the snoozed occurrence first, with that time as its occurrence (message ids, call URLs and acknowledgements key on it), then clears it. If the snooze reaches past the next occurrence, the occurrences in between are skipped. If nothing was delivered yet, the snoozed occurrence is the upcoming one.
- One-off reminders are unchanged.
- `Store.complete`, `Store.snooze` and `Store.stop` stay the only paths, so every interface (SMS, Telegram, email replies, call key presses, API, web UI, CLI) behaves the same.

## Consequences
- `snoozed_until` is always earlier than `due_at`. The scheduler relies on this to send the snoozed occurrence first.
- A recurring item only ends through stop or delete. Clients that used done to end a series must call stop.
- A reply that arrives after the person rescheduled the item by hand can still snooze or acknowledge it. Telling those apart would need a record of manual edits; this is revisited with item editing (#21).
