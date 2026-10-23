# Roadmap

**Vision:** an open-source, self-hosted second brain. You tell it things in plain language (typed, spoken, texted or from a phone call), and it remembers them, reminds you through the channels you choose, and answers "what did I say about…?" later.

It runs on your machine, a Raspberry Pi or a $5 VPS. You bring your own keys (Twilio, Telegram, SMTP, ntfy), and your data stays in a database you own.

Estimates are rough working hours. Status is tracked in GitHub issues.

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

## v0.2: Self-hostable server (in progress)
- [x] Recurring reminders: daily, weekdays, weekly, monthly, every N hours
- [x] Snooze (`maestro snooze 3 10m`, `POST /items/{id}/snooze`)
- [x] Versioned schema migrations, safe under concurrent startup
- [x] REST API with bearer-token auth; refuses to bind publicly without a token
- [x] Env-based config with `.env` support; channels: ntfy, Twilio SMS
- [x] Dockerfile, docker-compose, CI (ruff, pytest on 3.11–3.13, Docker build)
- [ ] Timezone-aware storage (UTC in the DB, user timezone in config) (~3h)
- [x] Per-channel delivery tracking with retry backoff, so a failing channel doesn't re-send on the others
- [ ] Structured JSON logs and a `/metrics` endpoint (Prometheus) (~2h)
- [ ] First PyPI release and GHCR image on tag (~2h)

## v0.3: Capture from anywhere (~16h)
- [ ] **Telegram bot**: capture, list, snooze and done from chat (bring your own bot token)
- [ ] **Inbound SMS**: text your Twilio number "dentist next tue 3pm" (webhook + signature validation)
- [ ] **Inbound voice**: call your Twilio number and say the reminder (Twilio speech gather → parser)
- [ ] **Email-in**: forward an email to capture it (IMAP poller)
- [ ] **Apple Shortcuts / Android Tasker** recipes against the REST API, documented
- [ ] Reply-to-act: reply "done" or "snooze 1h" to an SMS or Telegram reminder

## v0.4: More ways out (~12h)
- [ ] **Voice-call reminders** to your own phone for things that must not be missed (Twilio)
- [ ] Email (SMTP), Telegram, Matrix, generic webhook (Slack, Discord, Home Assistant)
- [ ] Per-item channel choice in plain language: "text me", "call me", "email me"
- [ ] Escalation policies: push first, SMS after 10 minutes unacknowledged, call after 30
- [ ] Quiet hours and do-not-disturb windows
- [ ] Channel plugin API via entry points, with a cookiecutter template for new channels

## v0.5: Web UI and companion apps (~20h)
- [ ] Web UI: capture box, timeline, notes, search, settings (HTMX, works on mobile)
- [ ] Installable PWA with web push notifications
- [ ] Calendar feed: subscribe to your reminders as an iCalendar (`.ics`) URL from any calendar app
- [ ] CalDAV sync (two-way) for people who live in their calendar
- [ ] Companion mobile app (Expo / React Native, iOS + Android) that points at your own server: quick capture, widgets, native notifications

## v0.6: Second brain (~16h)
- [ ] Full-text search with SQLite FTS5 (and `tsvector` on Postgres)
- [ ] Tags, people and places as first-class fields (#home, @alex, at the pharmacy)
- [ ] Questions over your notes: "where is my passport?" with an optional, pluggable language-model provider (off by default; rules-only mode keeps working offline)
- [ ] Daily and weekly digest ("here's what's coming up")
- [ ] Import from Apple Reminders, Google Tasks, Todoist exports; export to JSON and Markdown

## v0.7: Production-grade self-hosting (~14h)
- [ ] PostgreSQL backend behind the store interface, with migration tooling
- [ ] Multi-user per instance (household accounts), per-user API tokens, OIDC login for the web UI
- [ ] Encrypted backups to S3-compatible storage; `maestro backup` / `maestro restore`
- [ ] Deployment guides: Docker Compose, Raspberry Pi, Fly.io, Railway, systemd, Kubernetes (Helm chart)
- [ ] Rate limiting and audit log on the API

## v1.0: Stable (~8h)
- [ ] Stable library and REST API with a deprecation policy
- [ ] Docs site: quickstart, self-hosting, channel setup per provider, API reference, plugin guide
- [ ] Security review, threat model and `SECURITY.md` disclosure process

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
