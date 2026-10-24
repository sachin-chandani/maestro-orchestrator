# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

**Upgrading:** stop every running Maestro process (server, `run`, cron jobs) before starting the new version. Older versions don't know about the scheduler lease (#52) and would keep delivering in parallel. From this version on, an older Maestro refuses to open a newer database.

### Added
- Phrases: `tomorrow morning` / `afternoon` / `evening` / `night`, `by end of day` / `eod`, `end of week` / `eow`, `this weekend`, `on the 5th` (#16)
- Recurrences: `every 3 days`, `every other day`, `every other friday`, `every 2 weeks on monday`, `biweekly` / `fortnightly` (with `on <weekday>` anywhere in the phrase), `every night` / `every morning` (#16)
- Every-N-days/weeks reminders step from their previous occurrence, so they keep their rhythm after downtime
- Inbound SMS: text your Twilio number to capture, or `list` / `done 3` / `snooze 3 1h` / `rm 3`; verified with Twilio's signature and limited to your own number (#6)
- `MAESTRO_PUBLIC_URL` for verifying webhooks behind a reverse proxy
- Parser understands bare hours and times without am/pm the way people say them (`at 6` and `6:30` → evening, `at 9` → morning, `at 8 tonight` → 20:00, `at 6 tomorrow morning` → 06:00), `at 3 o'clock`, `around`/`after 5pm`, and `monthly on the 5th` (#55)
- Rate limits per client (#54): wrong API tokens (10/min, then the client is locked out of the API, so the token can't be brute-forced, even with parallel requests) and webhook requests with invalid signatures (60/min). Genuine Twilio callbacks and requests without a token are never limited; the web UI stops polling after a 401/429. `MAESTRO_TRUST_PROXY_HEADERS` for reverse proxies; metric `maestro_rate_limited_total`
- Reply-to-act: `done`, `snooze 1h` or just `snooze` (10 minutes) without a number act on the reminder last delivered on that channel, by SMS or Telegram; `rm` always needs a number (#8)

### Changed
- Engineering standards (`ENGINEERING.md`), architecture decision records (`docs/adr/`) and a PR template; CI now enforces a coverage floor (93%) and a dependency vulnerability audit (#58)
- Telegram and SMS share one command implementation (`maestro/commands.py`)
- The package type-checks cleanly with mypy, enforced in CI (#57)
- The database runs in WAL mode (with `synchronous=NORMAL` once WAL is confirmed) and a 5 s busy timeout, so separate processes (`maestro add`, backup tools, a second instance) no longer hit "database is locked" against a running server (#53). Keep `MAESTRO_DB` on a local disk or Docker volume, not a network share; `maestro doctor` warns otherwise
- Telegram `/done` without a number now acts on the latest reminder instead of printing usage; `/snooze 3` defaults to 10 minutes
- Reminder texts end with "(reply done or snooze 1h)"

### Fixed
- Parser (found by the new 200-phrase corpus, #55): "movie night saturday" no longer loses "night" (parts of the day now need "this", "in the", "at" or a day word); "remind me in 15 minutes to check the oven" no longer leaves "To"; "form 1040 by april 15" is no longer dated the year 1040 and "form 1999 by april 15" keeps its date; "movie night every friday" and "the morning standup every monday" keep their names; "remind me about that email" keeps "that"; "the dentist is at 5 main street" stays a note; text with unusual whitespace (tabs, form feeds) around a date no longer crashes the parser (found by property tests)
- New one-off reminders are never overdue: "this weekend" said on a Saturday or Sunday from 10:00, and "tonight", "today" or "today at 9am" after that time, used to produce a time in the past that fired at once. A time today that has passed now means tomorrow; "this weekend" moves to the next unpassed weekend slot (found by property tests, #56)
- Running more than one scheduler on a database (`serve` + `run`, cron `run --once`, two replicas) no longer delivers reminders twice: a lease in the database lets one fire while the others stand by and take over if it stops (#52). New metric `maestro_scheduler_leader`
- Database connections are now closed (CLI, `maestro doctor`); `Store` is a context manager
- Every write runs in a transaction that commits or rolls back as a whole; a write that failed half way can no longer leave the shared connection unusable
- `maestro run` exits cleanly on SIGTERM (`docker stop`, `systemctl stop`) and hands the scheduler lease over immediately
- API and CLI no longer crash if an item disappears between an update and the read-back (404 / exit 1 / "No item"); found by #57
- One corrupt reminder or a failed tick no longer stops the scheduler, and one malformed Telegram update no longer stops the bot; new metric `maestro_scheduler_errors_total`
- "the weekly report…", "my daily standup notes…" and similar are no longer turned into recurring reminders
- "the 5th element…" is no longer read as a date

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

[Unreleased]: https://github.com/sachin-chandani/maestro-orchestrator/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sachin-chandani/maestro-orchestrator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sachin-chandani/maestro-orchestrator/releases/tag/v0.1.0
