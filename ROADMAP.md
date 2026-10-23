# Roadmap

**Vision:** a second brain for one person. You tell it things in plain language — by typing, by voice, from your phone — and it remembers them, reminds you at the right moment through the right channel, and answers "what did I say about…?" later.

It starts with events and reminders, then grows into a personal memory you can query.

Estimates are rough working hours.

## v0.1: Capture and remind (done)
- [x] Plain-language capture: "call mom tomorrow at 5pm", "pay rent on Nov 1", "in 2 hours"
- [x] Notes without a time ("remember that my passport is in the blue drawer")
- [x] SQLite store, CLI (`add`, `list`, `search`, `done`, `rm`, `run`)
- [x] Scheduler that fires due reminders once, with console and macOS notifications
- [x] Test suite for parser, store and scheduler

## v0.2: Reliable daily driver (~10h)
- [ ] Timezone-aware storage (store UTC, display local)
- [ ] Recurring reminders: "every Monday at 9am", "every 1st of the month"
- [ ] Snooze and reschedule ("snooze 3 10m", `maestro edit`)
- [ ] Schema migrations
- [ ] Run as a background service (launchd on macOS, systemd on Linux)
- [ ] GitHub Actions CI with pytest and ruff; publish to PyPI

## v0.3: More ways in (~10h)
- [ ] REST API (FastAPI) over the same store
- [ ] Small web UI to capture, browse and complete items
- [ ] Telegram bot for capture and reminders from the phone
- [ ] Calendar feed: subscribe to your reminders as an iCalendar (`.ics`) URL

## v0.4: More ways out (~6h)
- [ ] Pluggable delivery channels with per-item choice ("text me", "email me")
- [ ] Email (SMTP) and push (ntfy)
- [ ] SMS and phone-call reminders **to your own number** (Twilio), opt-in
- [ ] Escalation: if a reminder isn't acknowledged, retry on a louder channel

## v0.5: Voice capture (~8h)
- [ ] Push-to-talk capture from the desktop
- [ ] Pluggable speech-to-text provider, with transcripts fed to the existing parser
- [ ] Spoken confirmation ("Got it, I'll remind you Saturday at 5pm")

## v0.6: Second brain (~10h)
- [ ] Full-text search with SQLite FTS5
- [ ] Tags and collections (#home, #health, #travel)
- [ ] Ask questions over your notes: "where is my passport?"
- [ ] Optional fallback parser for phrases the rules can't handle, behind the same interface
- [ ] Export and import (JSON, Markdown)

## Later
- Delegated reminders (asking someone you trust to nudge you). This involves other people's contact details, so it will be strictly opt-in and designed with privacy first.

## Principles
- **Local-first.** Your data lives in a SQLite file you own. No account needed.
- **Plain language in, plain language out.**
- **Every channel optional.** The core works offline with no API keys.
