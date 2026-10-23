# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/sachin-chandani/maestro-orchestrator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sachin-chandani/maestro-orchestrator/releases/tag/v0.1.0
