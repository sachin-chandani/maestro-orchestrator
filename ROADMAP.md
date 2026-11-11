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
   interpret ──► actions ──► apply ──► store ──► scheduler
   (rules by default;        (reminder · task ·       (delivery, escalation,
    optional model with       note · list · done ·     quiet hours: always
    your own key, ADR 0015)   snooze · ask)            deterministic)
                             │
            ┌────────────────┴──────────────────┐
            ▼                                   ▼
   storage backends                    delivery channels (plugins)
   SQLite (default)                    console · macOS · ntfy · SMS · voice call
   PostgreSQL                          email · Telegram · webhooks · Matrix
```

- **Core library** (`maestro`): pure Python, no network, importable by other projects.
- **Understanding** (ADR 0015): every capture becomes typed actions. The rule-based interpreter is the offline default; an optional language-model interpreter (your own key, or a local model) reads messy messages and forwarded emails, and only ever proposes actions that Maestro validates.
- **Server** (`maestro serve`): FastAPI app plus an in-process scheduler; one container, one volume.
- **Channels and capture sources** are plugins discovered through Python entry points, so third parties can ship `maestro-<channel>` packages.

| Area | Choice |
| --- | --- |
| Language | Python 3.11+ |
| API | FastAPI, OpenAPI schema published with each release |
| Storage | SQLite (default), PostgreSQL via the same store interface |
| Scheduling | In-process loop; optional external worker for multi-instance setups |
| Web UI | React + TypeScript app (Vite), built in CI and served by `maestro serve` (ADR 0014); design in `docs/design/mobile/` |
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

## v0.4: More ways out (done; release pending)
- [x] Voice-call reminders to your own phone (#11)
- [x] Email (SMTP) channel, with reply-to-act through email-in (#17)
- [x] Generic webhook channel for Slack, Discord, Home Assistant (#18)
- [x] Acknowledgement tracking (#19)
- [x] Escalation policies: push, then SMS, then more (#12); voice calls work as a stage (`30m=twilio_call`)
- [x] Quiet hours (#13)
- [x] Per-item channel choice: "text me", "call me", "email me" (#20)

The rest of the original v0.4 moved on 2026-11-11: #61 and #84 to v0.6, #14, #59 and #60 to v0.8, #70 to Later.

## v0.5: The loop (target Dec 13, 2026)
**Say it on your phone, it's in your list, it reaches your phone on time, you tap Done.** Nothing else goes in this release. Design: `docs/design/mobile/README.md`; decision: ADR 0017 (save first, ask later); tracking: #109.

- [x] Minimal web UI at `/`: capture, reminders and notes, search, done/snooze/delete, auto-refresh
- [x] Capture splits per reminder: sure ones save at once, unsure ones wait with a gap, one undo per capture (#126)
- [ ] The React app ships in the release and talks to the server (#111, #108, #112)
- [ ] Rules split one sentence into several requests (#130)
- [ ] One list: Needs a look, Next up, Today, Tomorrow, This week, Anytime (#22)
- [ ] Say or type, reminders land in place, with a 10-second Undo (#104)
- [ ] Fill a gap in one tap, and an evening wrap-up (#128)
- [ ] Mobile layout, keyboard and screen-reader accessibility (#87)
- [ ] Live check: the reminder reaches your phone on Telegram (#132)

Exit criteria: one sentence with three requests puts the sure ones in the list within a second and the unsure one in Needs a look; filling the gap takes one tap; the reminder arrives on a real phone through Telegram and Done there shows in the app; the React app ships in the wheel and the image.

## v0.6: Live with it (target Jan 10, 2027)
Use the loop every day for a week and fix what gets in the way (#133). The friction log decides the order; the items below are the starting list.

- [ ] A week of real use, with a friction log (#133)
- [ ] Reminder screen: what you said, what will happen, and edit (#21)
- [ ] Hold-to-talk mic with live words (#127)
- [ ] Search across reminders, tasks and notes (#105)
- [ ] Reach your server over HTTPS (#96), install to the home screen with web push (#24)
- [ ] Pair a phone with a QR code (#95, #106), Settings (#107), share into Maestro (#97)
- [ ] Edit by replying: "make it 4pm" (#85); Telegram acts on the message you replied to (#61)
- [ ] Lead-time alerts: "remind me an hour before" (#84)
- [ ] End-to-end tests in CI (#23)

Exit criteria: the friction log exists and its top items are fixed; a phone installs the app and gets web push over HTTPS; everything doable from the CLI is doable from the phone; e2e tests run in CI.

## v0.7: Second brain (target Feb 7, 2027)
**Understands what you mean** (#120, ADR 0015):
- [x] Capture produces actions: one interpret step and one apply step for every channel (#113)
- [x] Tasks: a third kind between reminders and notes (#114)
- [x] Optional language-model interpreter with your own key, off by default (#115)
- [x] Ask the model only when the rules are unsure (#116)
- [x] Confirm unsure interpretations, with undo (#117)
- [ ] Propose reminders from forwarded emails (#118), and confirmations by email (#124)
- [ ] One way to group things: lists (#88), tags (#29) and tracks (#119)

- [ ] Full-text search with SQLite FTS5 (#28)
- [ ] Daily and weekly digest (#30)
- [ ] Import from Apple Reminders, Google Tasks, Todoist (#31); export to JSON and Markdown (#32)
- [ ] Ask questions over your notes: offline by default, the optional model when configured (#33)
- [ ] Streaks and adherence for recurring reminders (#89)

Exit criteria: one grouping model decided and documented; search behaves the same on SQLite and Postgres; import → export → import round-trips without loss; with the model interpreter off, behaviour is identical to the rules alone.

## v0.8: Easy to run (target Feb 28, 2027)
- [x] Storage interface and PostgreSQL backend (#34)
- [ ] Local backup and restore (#59) and graceful shutdown (#60)
- [ ] Guided first-run setup: `maestro init` (#91)
- [ ] Deployment guides: Raspberry Pi, Fly.io, Railway, systemd, Caddy (#38)
- [ ] Move existing data between SQLite and PostgreSQL (#90)
- [ ] Encrypted off-site backups to S3-compatible storage (#37)
- [ ] iCalendar (`.ics`) feed (#25)
- [ ] Channel plugins via entry points (#14)
- [ ] Performance at 10k items (#44)

Exit criteria: a new user reaches the first test message in under 15 minutes with `maestro init`; restores tested on both backends; a third-party channel installs without core changes.

## v1.0: Stable (target Mar 21, 2027)
- [ ] Documentation site (#41)
- [ ] API stability and deprecation policy (#42)
- [ ] Security review and threat model (#43)

Exit criteria: public API, CLI, settings and plugin API frozen with a deprecation policy; threat model published.

## Later / ideas
Deferred until a real user asks for them (label `later`): Helm chart (#39), OIDC login (#36), multi-user (#35), two-way CalDAV (#26), Matrix channel (#86), voice PIN (#70), per-token limits and audit log (#40), native mobile app (#27).

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
