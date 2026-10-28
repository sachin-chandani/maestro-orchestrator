# 0009: Done and snooze on a recurring reminder apply to one occurrence

**Status:** Accepted (#80)

## Context
A recurring reminder was a single row, and `due_at` was its next occurrence. Done set the whole row to `done`, so replying "done" to today's "take pills" stopped every later day. Snooze overwrote `due_at`, and later occurrences are computed from `due_at`'s wall-clock time, so a daily 09:00 reminder snoozed by 10 minutes moved to 09:10 for good. Once there was escalation (#12) and phone calls (#11), "done" became the normal reply to a daily reminder, which made both bugs serious.

## Decision
- **Which occurrence:** an action on a recurring reminder is about one occurrence.
  - A reply to a specific delivery names it: an email reply, a call key press, or a "done"/"snooze" reply without a number, which answers the last message on that channel. That holds however late the reply comes.
  - Otherwise (the web UI, "done 3") it's the latest delivery, if that was recent (12h) and nobody has acknowledged it yet.
  - Otherwise it's the upcoming occurrence: a pending snooze first, then the series' next.
- **Done** acknowledges that occurrence. For a delivered one, it stops further retries and drops its snooze re-send. For an upcoming one, it skips it ("I took them early"). The series keeps running.
- **Stop** (`maestro stop`, `stop <id>` in chat, `POST /items/{id}/stop`, and a web UI button) ends a series. It always needs an explicit item number. Delete still works too.
- **Snooze** on a recurring reminder sets `snoozed_until` (migration 8, indexed) and leaves `due_at` on the series' time.
  - The scheduler sends the snoozed occurrence first, with that time as its occurrence (message ids, call URLs and acknowledgements key on it), then clears it.
  - Series occurrences up to the snooze time are skipped.
  - If the series' next occurrence is also due (after downtime), it is sent instead and the snooze is dropped: one catch-up notice, not two.
- One-off reminders are unchanged.
- `Store.complete`, `Store.snooze` and `Store.stop` stay the only paths, so every interface (SMS, Telegram, email replies, call key presses, API, web UI, CLI) behaves the same.

## Consequences
- `snoozed_until` is always earlier than `due_at`. The scheduler relies on this to send the snoozed occurrence first.
- A recurring item only ends through stop or delete. Clients that used done to end a series must call stop.
- Telegram's Done/snooze buttons don't yet carry their occurrence, so a press on an old message falls back to the rule for un-anchored actions. #61 (Telegram reply mapping) adds it.
- A reply that arrives after the person rescheduled the item by hand can still snooze or acknowledge it. Telling those apart would need a record of manual edits; this is revisited with item editing (#21).
