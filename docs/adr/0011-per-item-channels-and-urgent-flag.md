# 0011: Per-item channels and the urgent flag are stored, with fallbacks that never drop a reminder

**Status:** Accepted (#20)

## Context
Every reminder went to every channel in `MAESTRO_CHANNELS`, and urgency (for quiet hours, #13) was re-derived from the item's text on every read: a leading `!` or the word "urgent". #20 lets one capture pick its own channels ("pay rent friday, text me") and asks for a real urgent flag set at parse time. Both need schema and a rule for what happens when a chosen channel isn't configured — the channel factories normally fail at startup, but an item's choice is data, and data must not be able to stop delivery.

## Decision
- **Parsing is conservative.** Only clear, self-directed instructions on a clause boundary pick channels: trailing ("…, text me", "… and email me") or leading with a connector ("text me to stretch…"). Anything ambiguous ("call me a cab", "text Sara back", "text me at 5") keeps its text and the default channels. The corpus carries positive and negative cases.
- **Schema:** `items.channels` (TEXT, a comma-separated list of channel names) and `items.urgent`, both nullable, added as SQLite migration 9 and PostgreSQL migration 2.
  - `channels` NULL means "use `MAESTRO_CHANNELS`" — a per-item choice is the exception, not a copy of the defaults into every row.
  - `urgent` NULL means "written before the flag existed": `quiet.is_urgent` then falls back to the old text rules, so an upgrade never changes which reminders break through quiet hours. New items always carry an explicit True/False.
- **Unconfigured choices fall back, loudly, twice:**
  - At capture, where the configuration is known (CLI, API server), a channel without its settings is dropped from the item with a message, so the user hears it immediately. Chat captures (Telegram, SMS, email-in) skip this check and store the choice.
  - At delivery, chosen channels that are unavailable are reported — logged and counted (`maestro_channel_fallbacks_total`), once per occurrence when it first fires — and the rest of the choice is used; with nothing left, the defaults are, because a reminder is never silently dropped.
- **A choice widens what is built, not what is sent by default.** `build_notifier` also builds the requestable channels (`twilio_sms`, `twilio_call`, `email`, `telegram`) that are configured but not in `MAESTRO_CHANNELS`, kept in `MultiNotifier.extra`; they are only used for items that asked.
- **Escalation ignores per-item channels.** The choice applies to the first notice; escalation stages are the operator's "make sure they see it" ladder and keep their configured channels, so an item choosing a quiet channel can't silence its own escalation.

## Consequences
- Delivery behaviour now depends on row data: tests cover choice, fallback and defaults per occurrence.
- The urgent text rules live on indefinitely for pre-migration rows; they can only be retired by a data migration that backfills `urgent`, which was deliberately not done (re-parsing old text at migration time risks changing behaviour).
- Channel names are stored as plain text; renaming a channel would need a data migration.
- "Configured" at capture means the factory builds, not that the provider works; `maestro doctor --send-test` remains the live check.
