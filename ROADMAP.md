# Roadmap

**Vision:** an open-source, self-hosted second brain. You tell it things in plain language (typed, spoken, texted or from a phone call), and it remembers them, reminds you through the channels you choose, and answers "what did I say about…?" later.

It runs on your machine, a Raspberry Pi or a $5 VPS. You bring your own keys (Twilio, Telegram, SMTP, ntfy), and your data stays in a database you own.

Dates are targets, not promises. Work is tracked in [GitHub issues](https://github.com/sachin-chandani/maestro-orchestrator/issues) and [milestones](https://github.com/sachin-chandani/maestro-orchestrator/milestones).

---

## Architecture (target)

```
            ┌───────────── capture ─────────────┐
 CLI · REST · Web UI · Telegram · SMS · Voice call · Email-in · Shortcuts
            └────────────────┬──────────────────┘
                             ▼
                 maestro core (Python library)
          parser → recurrence → store → scheduler
                             │
            ┌────────────────┴──────────────────┐
            ▼                                   ▼
   storage backends                    delivery channels (plugins)
   SQLite (default)                    console · macOS · ntfy · SMS · voice call
   PostgreSQL                          email · Telegram · webhooks · Matrix
```

- **Core library** (`maestro`): pure Python, no network, importable by other projects.
- **Server** (`maestro serve`): FastAPI app plus an in-process scheduler; one container, one volume.
- **Channels and capture sources** are plugins discovered through Python entry points, so third parties can ship `maestro-<channel>` packages.

| Area | Choice |
| --- | --- |
| Language | Python 3.11+ |
| API | FastAPI, OpenAPI schema published with each release |
| Storage | SQLite (default), PostgreSQL via the same store interface |
| Scheduling | In-process loop; optional external worker for multi-instance setups |
| Web UI | Server-rendered (Jinja + HTMX), no build step |
| Packaging | PyPI wheel, multi-arch Docker image on GHCR (amd64, arm64 for Raspberry Pi) |
| Docs | MkDocs Material on GitHub Pages |
| CI/CD | GitHub Actions: lint, test matrix, Docker build, release on tag |

---

## v0.1: Core (done)
- [x] Plain-language capture: `tomorrow at 5pm`, `in 2 hours`, `next friday`, `on Nov 1`
- [x] Notes without a time, with text search
- [x] SQLite store and CLI (`add`, `list`, `search`, `done`, `rm`, `run`)
- [x] Scheduler with console and macOS notifications

## v0.2: Self-hostable server (released Oct 24, 2026)
- [x] Recurring reminders: daily, weekdays, weekly, monthly, every N hours
- [x] Snooze (`maestro snooze 3 10m`, `POST /items/{id}/snooze`)
- [x] Versioned schema migrations, safe under concurrent startup
- [x] REST API with bearer-token auth; refuses to bind publicly without a token
- [x] Env-based config with `.env` support; channels: ntfy, Twilio SMS
- [x] Dockerfile, docker-compose, CI (ruff, pytest on 3.11–3.13, Docker build)
- [x] Timezone-aware storage: UTC in the DB, `MAESTRO_TIMEZONE` for parsing and display, DST-safe recurrences (#1)
- [x] Per-channel delivery tracking with retry backoff (#2)
- [x] Structured JSON logs and a `/metrics` endpoint (#3)
- [x] `maestro doctor` to validate config and test each channel (#15)
- [x] Release workflow on tags: GitHub release from the changelog, multi-arch GHCR image, PyPI once enabled (#4)

## v0.3: Capture from anywhere, hardened (released Oct 26, 2026)
- [x] Telegram bot: capture, list, snooze and done from chat, plus a delivery channel with action buttons (#5)
- [x] Inbound SMS to your Twilio number, with signature validation (#6)
- [x] Inbound voice: call your number and say the reminder (#7)
- [x] Reply-to-act: "done" or "snooze 1h" in reply to a reminder (#8)
- [x] Email-in via IMAP (#9)
- [x] Apple Shortcuts and Android Tasker recipes (#10)
- [x] Exactly-once scheduling when more than one process runs (#52)
- [x] SQLite durability: WAL and busy timeout (#53)
- [x] Rate limits on public endpoints (#54)
- [x] Parser accuracy corpus, target >= 95% (#55); 201/201 at introduction
- [x] Property-based tests for recurrence and timezones (#56)
- [x] mypy clean in CI (#57); engineering standards, ADRs, coverage and audit gates (#58)

Exit criteria: capture from every source above, parser corpus >= 95%, exactly-once scheduling across processes, mypy and coverage >= 90% enforced in CI, no known vulnerabilities.
- [x] More phrases: parts of the day, end of day/week, weekends, `on the 5th`, every N days/weeks (#16)

## v0.4: More ways out (target Nov 22, 2026)
- [ ] Voice-call reminders to your own phone (#11)
- [x] Email (SMTP) channel, with reply-to-act through email-in (#17)
- [ ] Generic webhook channel for Slack, Discord, Home Assistant (#18)
- [x] Acknowledgement tracking (#19)
- [x] Escalation policies: push, then SMS, then more (#12); voice-call reminders (#11) will plug in as a channel
- [ ] Quiet hours (#13)
- [ ] Per-item channel choice: "text me", "call me", "email me" (#20)
- [ ] Channel plugins via entry points (#14)
- [ ] Local backup and restore (#59)
- [ ] Graceful shutdown and documented at-least-once delivery (#60)
- [ ] Telegram: act on the reminder you replied to (#61)

Exit criteria: escalation proven end to end with fakes, acknowledgements on every channel, backups restorable.

## v0.5: Web UI and companion apps (target Dec 13, 2026)
- [x] Minimal web UI at `/`: capture, reminders and notes, search, done/snooze/delete, auto-refresh
- [ ] Edit items (#21) and a timeline view with keyboard shortcuts (#22)
- [ ] End-to-end tests for the web UI (#23)
- [ ] Installable PWA with web push (#24)
- [ ] iCalendar (`.ics`) feed (#25) and two-way CalDAV sync (#26)
- [ ] Companion mobile app spike (#27)

## v0.6: Second brain (target Jan 17, 2027)
- [ ] Full-text search with SQLite FTS5 (#28)
- [ ] Tags, people and places (#29)
- [ ] Daily and weekly digest (#30)
- [ ] Import from Apple Reminders, Google Tasks, Todoist (#31); export to JSON and Markdown (#32)
- [ ] Ask questions over your notes, offline-first (#33)

## v0.7: Production-grade self-hosting (target Feb 7, 2027)
- [ ] Storage interface and PostgreSQL backend (#34)
- [ ] Multi-user per instance (#35) and OIDC login for the web UI (#36)
- [ ] Encrypted off-site backups to S3-compatible storage (#37)
- [ ] Deployment guides: Raspberry Pi, Fly.io, Railway, systemd, Caddy (#38) and a Helm chart (#39)
- [ ] Per-token API rate limits and audit log (#40)

## v1.0: Stable (target Feb 28, 2027)
- [ ] Documentation site (#41)
- [ ] API stability and deprecation policy (#42)
- [ ] Security review and threat model (#43)
- [ ] Performance at 10k items (#44)

## Later / ideas
- Delegated reminders: ask someone you trust to nudge you. This involves other people's contact details, so it would be strictly opt-in with explicit consent from the other person.
- Location-based reminders via the companion app ("when I'm at the pharmacy").
- Home Assistant integration (announce reminders on smart speakers).

---

## Principles
- **Self-hosted and local-first.** Your data lives in a database you run. No telemetry.
- **Bring your own keys.** Every external service is optional and configured by you.
- **Works offline.** The core parser and scheduler need no network and no API keys.
- **Plain language in, plain language out.**
- **Small and boring to operate.** One container, one volume, SQLite by default.
