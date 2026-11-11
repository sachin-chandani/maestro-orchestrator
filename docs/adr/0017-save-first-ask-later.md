# 0017: The app saves first and asks later

Status: Accepted (2026-11-10). Changes the default of ADR 0016. Design: `docs/design/mobile/README.md`. Tracking: #109.

## Context
ADR 0016 lets Maestro hold an unsure capture as a proposal until the person confirms it, and holds the whole message when any part of it is unsure. On a phone that's friction. Someone who says "book flights, remind me Friday at nine to check in, and call the plumber sometime next week" is sure about two of those, and shouldn't have to approve them because of the third. Asking before saving makes every capture a conversation.

## Decision
- **Never block.** Each reminder in a capture is judged sure or unsure on its own (#126). Sure ones are saved and scheduled at once.
- **Undo instead of asking.** Every capture returns one undo token that removes exactly what it added, valid for 10 minutes. The app shows Undo with a 10-second countdown. The server saves immediately and does not wait for those 10 seconds; if it did, closing the app during the countdown would lose the reminder.
- **Ask later, in place.** An unsure reminder is saved as "needs a look", with its gap named (`when`, `what` or `which channel`). It is visible at the top of the list, never fires and never disappears on its own. One tap fills the gap (#128).
- **One list of reminders.** The app has no tabs. A task is a reminder with no time, shown under Anytime. Notes are found through search (#105). Lists wait for #88 and #119.
- **The same on every channel.** SMS and Telegram answer "Added 2. 'Call the plumber' needs a time: reply with one."
- **Where it lives (#126).** A needs-a-look entry is an item with `items.gap` set, not a `captures` row, because captures expire and a question must not. `Store.due` skips gapped items. An unconfigured channel is not a gap: the item is saved and goes out on the default channels (ADR 0011). Details: `docs/design/split-captures-implementation.md`.
- **Settings.** `MAESTRO_CONFIRM=split` (this behaviour) becomes the default. `unsure` keeps ADR 0016's hold-the-whole-message; `never` treats everything as sure.

## Consequences
- ADR 0016's proposals, claim-then-act and undo remain; only the default and the unit (per reminder rather than per message) change.
- The app needs a capture call that returns what was added and what needs a look, an upcoming feed (#22), and a way to fill a gap.
- The preview-while-typing idea (#94) and the tabbed shell (#93) are dropped.
- Dictation uses the browser's speech recognition where available, off until the person turns it on, with the keyboard's own mic as the fallback (#127). Audio never reaches the Maestro server in v0.5.
