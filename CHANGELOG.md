# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `webhook` channel: POST reminders as JSON, or in Slack or Discord format; optional HMAC-SHA256 signature over timestamp and body; HTTPS required except on your own network; redirects are refused, mentions and markup in reminder text are neutralised, and the URL is redacted in errors (#18)
- `email` channel: reminder emails over SMTP (STARTTLS by default; refuses to send a password without TLS). With email-in on, replying `done` or `snooze 1h` acts on exactly that reminder and occurrence (the reply quotes its Message-ID; replies to an older occurrence are ignored); other replies are ignored instead of captured; HTML-only replies work (#17)
- `twilio_call` channel: Maestro calls your own number and reads the reminder out; press 1 for done or 2 to snooze 10 minutes (needs `MAESTRO_PUBLIC_URL`; signed webhook `/webhooks/twilio/voice/reminder`, acts only on that call's occurrence and once per call); works as an escalation stage (#11)
- Quiet hours: `MAESTRO_QUIET_HOURS=22:00-07:00` holds non-urgent reminders and their escalation until the window ends, then sends each once, with escalation stages timed again from the window's end; reminders starting with `!` or containing "urgent" (but not "not urgent") go through; `maestro doctor` shows the window; metric `maestro_reminders_held` (#13)
- Escalation: `MAESTRO_ESCALATION=10m=twilio_sms, 30m=telegram+twilio_sms` re-sends unacknowledged reminders on louder channels, once per stage, timed from when the first notice went out, until someone says done or snooze; only within 12 hours of the due time; works with `run`, `run --once` and `serve`; replies to escalation messages act on the reminder; metric `maestro_escalations_total{stage}` (#12)

### Fixed
- The scheduler no longer overwrites a done or snooze that lands between delivering a reminder and finishing it (e.g. from another process)

### Added
- Acknowledgements: marking a recently delivered reminder done or snoozing it (from any interface, including `web`) is recorded per occurrence; deliveries from before the upgrade count as acknowledged; `GET /items?unacknowledged=true`, `awaiting_acknowledgement` on items, a web UI badge, and metrics `maestro_acknowledgements_total{via}` / `maestro_unacknowledged`. Groundwork for escalation (#19)

## [0.3.0] - 2026-10-26
Capture from anywhere, hardened. Reminders can now come in by SMS, phone call, email and phone shortcuts, and the server behind them is built to run unattended: one scheduler fires at a time, bad data never stops it, and public endpoints are authenticated and rate limited.

**Upgrading:** stop every running Maestro process (server, `run`, cron jobs) before starting 0.3. Older versions don't know about the scheduler lease and would keep delivering in parallel. From 0.3 on, an older Maestro refuses to open a newer database. The database migrates automatically (schema 4 → 6).

### Capture from anywhere
- **SMS:** text your Twilio number to capture; `list`, `done 3`, `snooze 3 1h`, `rm 3` (#6)
- **Voice:** call your Twilio number, say the reminder, hear it read back (#7)
- **Email:** send or forward mail to a dedicated inbox polled over IMAP (#9)
- **Phone shortcuts:** Apple Shortcuts and Android Tasker guide, with its examples tested against the API (#10)
- **Reply to act:** reply `done` or `snooze 1h` to a reminder by SMS or Telegram, no number needed; `rm` always needs one (#8)
- API items include `when`, a ready-to-show description of the due time

### Understands more
- Parts of the day, `eod`/`eow`, `this weekend`, `on the 5th`, `every other friday`, `every 3 days`, `biweekly on tuesday`, `every night` (#16)
- Hours without am/pm the way people say them (`at 6` → 18:00, `at 8 tonight`, `6:30`), dictated `a.m.`/`p.m.`, `around 5pm`, `monthly on the 5th` (#55)
- Measured against a 225-phrase corpus: 98.7%, with 3 documented gaps (#55)
- New one-off reminders are never overdue: a time today that has passed means tomorrow (#56)

### Reliable
- **Exactly-once delivery** when several schedulers share a database (`serve` + `run`, cron, replicas), via a lease in the database (#52)
- **WAL mode** with a busy timeout: other processes no longer hit "database is locked" (#53)
- One corrupt reminder, failed tick or malformed message never stops the scheduler, the bot or the email worker
- Every write is atomic; inbound SMS, calls and emails are captured once even when the provider retries
- `maestro run` exits cleanly on SIGTERM and hands over at once
- Metrics: `maestro_scheduler_leader`, `maestro_scheduler_errors_total`, `maestro_rate_limited_total`, `maestro_email_given_up_total`

### Secure by default
- Webhooks are authenticated by provider signatures and accept only the owner; calls whose caller ID failed STIR/SHAKEN are rejected
- Email senders must be confirmed by DMARC in your own provider's header, so forged senders and headers are ignored
- Rate limits: 10 wrong API tokens per minute per client, then a lockout (safe against parallel guessing); forged webhooks limited; genuine callbacks never (#54)

### Engineering
- `ENGINEERING.md` (service objectives, definition of done), 8 architecture decision records, PR template (#58)
- CI gates: ruff, mypy (#57), tests on Python 3.11–3.13, coverage ≥ 96%, dependency audit, Docker build
- Property-based tests (#56) and a parser accuracy corpus (#55); every pull request self-reviewed before merge

### Fixed
- "this weekend", "tonight" and "today at 9am" could produce a time in the past that fired at once
- "movie night", "the weekly report", "the 5th element", "form 1040" and similar no longer misread as times or schedules
- Text with tabs or form feeds near a date no longer crashes the parser
- Database connections are closed; a failed write can no longer leave the shared connection unusable
- API and CLI no longer crash if an item disappears between an update and the read-back
- Telegram `/done` without a number acts on the latest reminder; `/snooze 3` defaults to 10 minutes

## [0.2.0] - 2026-10-24
Self-hosting essentials.

### Added
- Telegram bot: capture by messaging your own bot; `/list`, `/done`, `/snooze`, `/rm`; reminders delivered with ✅ Done / +10m / +1h / +1d buttons; only your chat is answered (#5)
- Web UI at `/`: capture, reminders and notes, search, done/snooze/delete, auto-refresh
- `MAESTRO_TIMEZONE`: parsing and display in your timezone; recurring reminders keep their wall-clock time across DST (#1)
- `GET /metrics` in Prometheus format and `MAESTRO_LOG_FORMAT=json` (#3)
- `maestro doctor [--send-test]` to check configuration and test every channel (#15)
- Release workflow: GitHub release, multi-arch image at `ghcr.io/sachin-chandani/maestro-orchestrator` (#4)

### Changed
- Timestamps are stored in UTC. Existing databases are migrated automatically (schema 4), interpreting old values in the configured timezone.
- Delivery is tracked per channel: a failing channel is retried with backoff (up to 5 attempts) without re-sending on channels that already succeeded (#2)
- The version is defined only in `maestro/__init__.py`

### Fixed
- A failure on one channel no longer causes duplicate notifications on the others (#2)

## [0.1.0] - 2026-10-23
First release.

### Added
- Plain-language capture: `tomorrow at 5pm`, `in 2 hours`, `next friday at 10am`, `on Nov 1`, `tonight`, `at noon`
- Recurring reminders: daily, weekdays, weekly, monthly, every N minutes or hours
- Notes for anything without a time, with text search
- CLI: `add`, `list`, `search`, `snooze`, `done`, `rm`, `run`, `serve`
- REST API (FastAPI) with bearer-token auth
- Channels: console, macOS notifications, ntfy push, Twilio SMS
- Dockerfile and docker-compose
- Versioned SQLite migrations, safe under concurrent startup

[Unreleased]: https://github.com/sachin-chandani/maestro-orchestrator/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/sachin-chandani/maestro-orchestrator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/sachin-chandani/maestro-orchestrator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sachin-chandani/maestro-orchestrator/releases/tag/v0.1.0
