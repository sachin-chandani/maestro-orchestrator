# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
