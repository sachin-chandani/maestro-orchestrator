# Roadmap

**Vision:** an open-source, self-hosted second brain. You tell it things in plain language (typed, spoken, texted or from a phone call), and it remembers them, reminds you through the channels you choose, and answers "what did I say about…?" later.

It runs on your machine, a Raspberry Pi or a $5 VPS. You bring your own keys (Twilio, Telegram, SMTP, ntfy), and your data stays in a database you own.

## Who it's for
- **The self-hoster with a busy life.** Runs a Pi or a small VPS, wants reminders that reach them wherever they are (text, call, chat, email), and doesn't want their life in someone else's cloud.
- **The "I'll forget otherwise" person.** Bills, plants, school forms, bin day. Recurring reminders that escalate until acknowledged, and a record of whether they kept up.
- **The note-taker.** Captures thoughts from wherever they are and wants to find them again, in lists, by tag, or by asking.

**What success looks like:** capture takes under 5 seconds from any device; no reminder is silently missed; setup takes under 15 minutes from `pip install` to the first test message.

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
- [x] Voice-call reminders to your own phone (#11)
- [x] Email (SMTP) channel, with reply-to-act through email-in (#17)
- [x] Generic webhook channel for Slack, Discord, Home Assistant (#18)
- [x] Acknowledgement tracking (#19)
- [x] Escalation policies: push, then SMS, then more (#12); voice calls work as a stage (`30m=twilio_call`)
- [x] Quiet hours (#13)
- [x] Per-item channel choice: "text me", "call me", "email me" (#20)
- [ ] Channel plugins via entry points (#14)
- [ ] Local backup and restore (#59)
- [ ] Graceful shutdown and documented at-least-once delivery (#60)
- [ ] Telegram: act on the reminder you replied to (#61)
- [ ] Lead-time alerts: "remind me an hour before" (#84)

Exit criteria: escalation proven end to end with fakes, acknowledgements on every channel, backups restorable, a third-party channel installable without core changes.

## v0.5: Web UI and companion apps (target Dec 13, 2026)
- [x] Minimal web UI at `/`: capture, reminders and notes, search, done/snooze/delete, auto-refresh
- [ ] Edit items (#21) and a timeline view with keyboard shortcuts (#22)
- [ ] End-to-end tests for the web UI (#23)
- [ ] Installable PWA with web push (#24)
- [ ] iCalendar (`.ics`) feed (#25) and two-way CalDAV sync (#26)
- [ ] Companion mobile app spike (#27)
- [ ] Edit a reminder by replying: "make it 4pm" (#85)
- [ ] Matrix channel, built as the first entry-point plugin (#86)
- [ ] Mobile layout, keyboard and screen-reader accessibility (#87)

**Phone-first experience.** Most capture and nearly every reminder happens on a phone, so the web UI becomes an installable app reached securely from anywhere:
- [ ] Reach your server from your phone over HTTPS: Tailscale, Cloudflare Tunnel or Caddy, with doctor checks (#96)
- [ ] Pair a phone with a QR code, with a key per device (#95)
- [ ] App shell: Today, Lists and Notes tabs with an always-on capture bar (#93)
- [ ] Show what Maestro understood while you type (#94)
- [ ] Settings for channels, quiet hours and escalation without a restart (#98)
- [ ] Share to Maestro from any app, plus home-screen shortcuts (#97)

Exit criteria: a new phone goes from scanning a pairing code to its first push notification in under 2 minutes; everything doable from the CLI is doable from the web UI on a phone; e2e tests and an accessibility check run in CI; installable as an app with push.

## v0.6: Second brain (target Jan 17, 2027)
- [ ] Full-text search with SQLite FTS5 (#28)
- [ ] Tags, people and places (#29)
- [ ] Daily and weekly digest (#30)
- [ ] Import from Apple Reminders, Google Tasks, Todoist (#31); export to JSON and Markdown (#32)
- [ ] Ask questions over your notes, offline-first (#33)
- [ ] Lists: "add milk to the shopping list" (#88)
- [ ] Streaks and adherence for recurring reminders (#89)

Exit criteria: search behaves the same on SQLite and Postgres; import → export → import round-trips without loss; one grouping model (lists vs tags) decided and documented.

## v0.7: Production-grade self-hosting (target Feb 7, 2027)
- [x] Storage interface and PostgreSQL backend (#34)
- [ ] Multi-user per instance (#35) and OIDC login for the web UI (#36)
- [ ] Encrypted off-site backups to S3-compatible storage (#37)
- [ ] Deployment guides: Raspberry Pi, Fly.io, Railway, systemd, Caddy (#38) and a Helm chart (#39)
- [ ] Per-token API rate limits and audit log (#40)
- [ ] Move existing data between SQLite and PostgreSQL (#90)
- [ ] Guided first-run setup: `maestro init` (#91)

Exit criteria: a new user reaches the first test message in under 15 minutes with `maestro init`; an existing SQLite install moves to Postgres with one documented command; restores tested on both backends.

## v1.0: Stable (target Feb 28, 2027)
- [ ] Documentation site (#41)
- [ ] API stability and deprecation policy (#42)
- [ ] Security review and threat model (#43)
- [ ] Performance at 10k items (#44)

Exit criteria: public API, CLI, settings and plugin API frozen with a deprecation policy; threat model published; documented performance targets met in CI.

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
