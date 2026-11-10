# 0016: Unsure captures are proposals the person confirms, with undo

**Status:** Accepted (#117). Its default changes in ADR 0017: captures split per reminder, and only unsure reminders wait (#126).

## Context
ADR 0015 says unsure interpretations become proposals the user confirms in the channel they used,
and that every capture can be undone for a short time. A model (or the rules) can misread; a
reminder that silently goes wrong is worse than one that asks.

## Decision
- **Opt-in.** `MAESTRO_CONFIRM=never` is the default: captures apply at once, exactly as before,
  whatever the interpreter. `unsure` holds a capture for confirmation when the rules' confidence
  check found doubts (`maestro.confidence`, #116) or one message asked for several things. The
  model never grades itself: a forwarded email could talk it into waving its own reading through.
- **A proposal is not an item.** It lives in its own `captures` table as validated action JSON.
  The scheduler, lists, counts and search read `items`, so nothing unconfirmed can be delivered.
- **Claim-then-act, claim first.** Confirming does one atomic check-and-set (pending → applied,
  expiry checked in the same statement), and only the winner applies the actions — with a dedupe
  key derived from the capture id, so a retry straddling a crash saves nothing twice. A crash
  between claim and apply loses that capture's items, never duplicates them; the reply is sent
  only after both, so nothing acknowledged is lost. Act-first was rejected: it could save items
  for a capture a concurrent cancel or expiry had already decided against.
- **Replies are matched conservatively.** "yes", "no" and "undo" (with an optional `#id`) answer
  a confirmation only while that sender has a capture the word could answer, and only their own
  (same channel, same chat id / phone number); otherwise the text is captured as ever (ADR 0005).
  A bare word means the most recent candidate; the question always quotes its id.
- **Expiry: visible, never applied.** Default 24 hours (`MAESTRO_CONFIRM_HOURS`), or the earliest
  proposed due time if sooner — an unanswered "oven in 1 hour" past the hour is noise. The
  confirm statement checks the clock itself; the scheduler sweep only keeps lists and metrics
  honest. Expired captures stay in `maestro list --proposals` and the API rather than vanishing.
- **Undo is for confirmations.** An applied capture can be undone for 10 minutes: exactly the
  items it created (`items.capture_id`) are deleted, deliveries and acknowledgements included.
  An occurrence that already fired stays sent — a message can't be unsent. Direct captures are
  untouched by this feature and can't be undone; items from before it have NULL `capture_id`.

## Consequences
- Voice keeps applying directly (the ask-and-answer won't fit Twilio's ~15 s webhook window), and
  email-in does too until the worker can send a question mail; both are listed in the issue.
- The proposal stores `source_text`, as items always have; logs carry only capture ids and
  outcomes, never the text.
- SQLite schema 12 and PostgreSQL schema 5; both are migration-tested from the previous schema.
