# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Device tokens (ADR 0018, for #108): `POST /devices {name}` with `MAESTRO_API_TOKEN` answers once with a token of the device's own (`mdt_…`), accepted everywhere the master token is except on `/devices` itself, so a phone never stores the master token and a lost phone is one `DELETE /devices/{id}` (or `maestro devices revoke <id>`) rather than a rotation. `GET /devices` lists what is signed in; `maestro devices list|add|revoke` does the same from a terminal. Only the token's SHA-256 is stored; a wrong device token counts towards the same lockout as a wrong master token. SQLite migrates to schema 14 and PostgreSQL to 7; metric `maestro_devices_total{action}`
- A capture now splits per reading: the parts Maestro can act on are saved at once and only the rest wait as **needs a look** (#126, ADR 0017). Each proposed action is judged on its own (`maestro.confidence.gap`) and an unsure one names what it lacks — `when`, `what` or `channel` — as a nullable `items.gap`; a gapped item is saved, listed and searchable but never scheduled, never delivered and never expires. **A reading that already has a usable time is never held back**, whatever the message was doubted for, so nothing that fired before stops firing: only something to do with no time at all, in a message naming one the rules can't read ("pick up the kids before", "call mom morning"), becomes a question, and notes are never held back. Answer with a bare time ("5pm") to fill the one question asked in the last 30 minutes (`MAESTRO_GAP_REPLY_MINUTES`), or name it (`#12 5pm`) at any time; with two open questions Maestro asks which and saves nothing, and anything that says what to do ("call dad at 5pm") is captured as usual. A reply can only ever complete a question, never change a scheduled reminder. `dismiss` drops one. Every capture carries one 10-minute undo token covering everything it added, on every channel. New: `maestro list --needs-look`, `maestro fill <id> <phrase>`, `maestro dismiss <id>`, a **Needs a look** section in `maestro list`, `GET /needs-look`, `POST /needs-look/{id}`, `DELETE /needs-look/{id}` and `POST /capture` answering `{added, needs_look, undo: {id, expires_at}}`. SQLite migrates to schema 13 and PostgreSQL to 6; metric `maestro_capture_split_total{result}`
- Confirm before acting on an unsure interpretation, with undo (#117, ADR 0016). `MAESTRO_CONFIRM=unsure` (off by default: `never`) holds a capture the rules are unsure of (#116), or one message asking for several things, as a proposal — stored in its own table, never visible to the scheduler — and asks in the channel used: Telegram Yes / Change / Cancel buttons, SMS "Reply YES or NO", `maestro add` on a terminal, or a `202` from `POST /items` with `POST /captures/{id}/confirm|cancel|undo` and `GET /captures` to answer. A confirmation acts exactly once (claim-then-act with a per-capture dedupe key: double taps, webhook retries and simultaneous replies save one set of items), every confirmation can be undone for 10 minutes (removing exactly the items it created), and unanswered proposals expire — default 24 h (`MAESTRO_CONFIRM_HOURS`), or at the proposed due time if sooner — staying visible in `maestro list --proposals` without ever being applied. "yes"/"no"/"undo" only answer your own question on the channel you asked on; otherwise they are captured as any text. Voice and email-in captures still save directly. SQLite migrates to schema 12 and PostgreSQL to 5 (a `captures` table and `items.capture_id`); metric `maestro_captures_confirmation_total{outcome}`
- `MAESTRO_INTERPRETER=auto`: the rules read every capture, and the model is asked only when they are unsure (#116). "Unsure" means evidence the reading is wrong or incomplete, not a length or score: a time still in the saved text, a time word left without its time ("before"), a time the rules can't read ("sometime next week", "after work", shorthand such as "tmrw", "b4" or "tues"), a second request, several sentences or a forwarded email, or dictation filler. On the parser corpus every misread phrase is flagged and plain captures never leave the server. A model that fails, times out or answers something invalid costs nothing: the rules' answer is used, as with `rules`. `model` and `rules` mean what they did. Each saved item records which interpreter read it (`rules` or `model`), shown by `maestro list --verbose` and as `interpreter` in the API; existing databases migrate in place (SQLite schema 11, PostgreSQL 4) and existing items show it as unknown rather than guessed. An interpreter can't be named in an action's JSON, so neither a model nor an API body can claim to be the rules. Metrics `maestro_interpret_routed_total{route}` and `maestro_interpret_doubts_total{doubt}`
- Optional language-model interpreter, off by default (#115, ADR 0015). `MAESTRO_INTERPRETER=model` with `MAESTRO_MODEL_PROVIDER` (`anthropic`, `openai` or `openai-compatible` for Ollama, LM Studio and vLLM), `MAESTRO_MODEL`, a key (`MAESTRO_MODEL_API_KEY`, else `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) and `MAESTRO_MODEL_TIMEOUT_SECONDS` (default 8). A key on its own never turns it on, and there is no default model name. The model only proposes actions, which are validated exactly like any other action before anything is saved: unknown kinds, times out of range, invented channels and anything beyond ten actions in one message are refused, and the whole reply is dropped in favour of the rules — as it is on a timeout, a provider error or a malformed answer — so a capture is never lost. It is never offered done, snooze or stop, so a forwarded email can't talk it into one. `maestro doctor` names the interpreter and warns plainly when captures leave the server; `maestro doctor --check-model` sends one fixed phrase and shows what came back. Metrics `maestro_interpret_total{interpreter,result}` and `maestro_interpret_seconds_total`; keys and capture text are never logged
- Tasks, a third kind of item between reminders and notes (#114, ADR 0015). An untimed capture that asks for something — a leading "todo:"/"to do:", a "remind me to…" with no time, or a to-do verb such as buy, call, renew, book, send, fix, pay or email — is kept as a task instead of being lost among the notes. A task is never delivered and never escalated; `done` works on it, and giving it a time (`maestro snooze <id> …`) turns it into a reminder. Statements stay notes, so "call centre number is 0800 123 456" and "book recommendation: project hail mary" are unchanged. `maestro list` shows reminders, tasks and notes under a heading each, `GET /items?kind=task` lists one kind, and the capture APIs answer with `"kind": "task"`. Existing databases migrate in place and every note stays a note; the web app's `Item` type and mock understand the new kind (screens come later)
- The new React web app (ADR 0014), served by `maestro serve` at `/app` while it's built; `/` keeps the current page. The wheel and the Docker image include it (Node is only needed to build it); a source checkout without a build gets a page at `/app` explaining how to build. For now it's a placeholder on sample data; see `web-app/README.md` (#100)
- The web app's first screen: today's, tomorrow's and later reminders with Done and +1 hour, and a capture bar, in the design's tokens and components with a dark theme that follows the system (#102). It runs on one API client with a mock and a real implementation; every build uses sample data until signing in to the server arrives (#108), and `npm run dev:api` or `VITE_MAESTRO_MOCK=0` uses the real API. Development builds add a panel to reset the sample data and simulate slow or failing calls, and a component gallery at `/app/?gallery`; neither is in production builds (#101)
- Per-item channels: say `text me`, `call me`, `email me` or `on telegram` in a capture and that one reminder is delivered there instead of `MAESTRO_CHANNELS`. Only clear instructions count ("call me a cab" stays text); several can be combined (`text me and email me`). The chosen channel needs its settings but not a `MAESTRO_CHANNELS` entry; asking for an unconfigured one says so at capture (CLI, API, Telegram; SMS/voice and email captures log it) and keeps the rest of the choice, and a stored choice that loses its configuration falls back at delivery — the still-configured channels, else the defaults — logged once per occurrence (metric `maestro_channel_fallbacks_total`). Shown in `maestro list`, the web UI and the API (`channels`) (#20)
- An `urgent` flag, set at capture from a leading `!` (now dropped from the reminder text) or the word "urgent"; quiet hours read the flag. Items stored by an older Maestro keep being judged by their text. Shown in `maestro list`, the web UI and the API (`urgent`) (#20)

### Fixed
- Web app robustness from the first review (#112). A 429 no longer locks the app until a reload: the client waits out `Retry-After` and carries on. Each card is busy on its own, so acting on two at once can't re-enable one early or skip an extra occurrence of a recurring reminder. The capture box clears as soon as a capture is sent and puts the text back only if the save fails and nothing new was typed, so words typed during a slow save aren't lost. A slow older response can no longer overwrite a newer one. The list is fetched again every minute while visible and whenever the tab is returned to, so groups, the date and "N left today" roll over at midnight and a sent reminder shows as awaiting. The same confirmation said twice no longer vanishes early. The sample-data client now locks after a 401 or 429 exactly as the real one does (shared `Connection`), and its "in N minutes" capture keeps the day, time and recurrence words it wasn't using
- The released wheel now actually ships the React app (#111). `python -m build` builds the wheel from the sdist, and the sdist's hatch target didn't list the gitignored `maestro/web/app/`, so a pip install served the "hasn't been built" page at `/app`. Both hatch targets now carry the app, `scripts/check_wheel_app.py` fails any wheel without it, and CI builds the wheel the way the release does and runs that check

### Changed
- **`MAESTRO_CONFIRM` now defaults to `split`** instead of `never` (#126, ADR 0017), so an upgrade changes behaviour without any configuration change. What this does and does not alter, deliberately: every reading with a usable time is still saved and still fires at the same time, so no reminder that went out before stops going out; captures that asked for several things no longer hold each other back; and something to do with no readable time at all is now saved as a visible question instead of being filed untimed. Every capture also now writes one `captures` row as its undo token. Set `MAESTRO_CONFIRM=never` to keep the old behaviour exactly, or `unsure` for ADR 0016's hold-the-whole-message confirmation
- The model interpreter now falls back to the rules on any unexpected error from a provider, not only on the errors it anticipated, so a bug in a provider or its response handling can't lose a capture. Only the error's type is logged, never its message (#116)

### Security
- The model interpreter's fallback log no longer quotes what the model wrote at length: a model-chosen action type, channel, field name, timestamp or recurrence rule is cut to 40 characters and stripped of line breaks, and a whole fallback reason to 300, so a model or a forwarded email steering it can't put a message or a forged line into the log. The model API key is left out of `repr()` of `Config` and `ModelSettings` (#123)

### Changed
- An action's `recurrence` is now validated against the stored rule grammar (`daily`, `weekdays`, `weekly:<0-6>`, `interval:<minutes>`, `days:<n>`, `weeks:<n>:<0-6>`, `monthly:<1-31>`) instead of being accepted as any text, so a rule the scheduler couldn't follow can't reach the store (#115). Nothing changes for captures — every rule the parser writes passes — but code building actions directly, e.g. `CreateReminder(recurrence="every monday")`, must now pass the rule (`weekly:0`) rather than the phrase
- Capture now has one shape everywhere: `maestro.interpret` reads text into typed actions (`maestro.actions`: `CreateReminder`, `CreateNote`, `Done`, `Snooze`, `Stop`, each with a validated JSON form) and `maestro.actions.apply` is the only code that turns them into store calls. CLI, API, SMS, voice, Telegram and email-in all go through it. `RulesInterpreter` is today's parser, so nothing changes for users: the same items are saved, each channel answers in the same words, provider retries still capture once, and the parser corpus is untouched. It is the step ADR 0015 needs before an optional model interpreter (#113)
- Storage is now an interface (`maestro.storage.Store`) with SQLite as the only backend, in preparation for PostgreSQL (#34). What done and snooze mean for a repeating reminder moved to `maestro.storage.logic`, so every backend decides it the same way. Nothing changes for users: the database, the CLI and the API are untouched, and `from maestro.store import Store` still opens SQLite
- `--db` and `maestro doctor` accept a database URL as well as a path, and `Store.acknowledgement(item_id, occurrence)` reads how an occurrence was acknowledged (#34)

### Added
- PostgreSQL backend: set `MAESTRO_DB_URL=postgresql://user:password@host/maestro` and install `maestro-orchestrator[postgres]`. SQLite stays the default and is untouched; psycopg is only imported when a URL asks for it. The scheduler lease, inbound-message claims and migrations are safe across processes on PostgreSQL too, and both backends are held to the same conformance suite (#34)
- `maestro doctor` names the database it reached (`postgresql 16.2 at db:5432/maestro`), `GET /health` reports the `backend`, and `/metrics` exposes `maestro_store_info{backend="…"}` and `maestro_store_reconnects_total`. Existing metrics keep the labels they had (#34)
- An unreachable, locked or too-new database is reported as `Database error: …` with exit code 2 instead of a traceback (#34)
- `docker compose --profile postgres up -d` starts a PostgreSQL alongside Maestro; without the profile nothing changes and SQLite stays the default (#34)
- `MAESTRO_DB_URL`: name the database as a URL (`sqlite:///data/maestro.db`, `postgresql://…`). `MAESTRO_DB` keeps working as the SQLite path; when both are set the URL wins and `maestro doctor` says so. Passwords in a URL are redacted wherever Maestro prints it (#34)

### Added
- `webhook` channel: POST reminders as JSON, or in Slack or Discord format; optional HMAC-SHA256 signature over timestamp and body; HTTPS required except on your own network; redirects are refused, mentions and markup in reminder text are neutralised, and the URL is redacted in errors (#18)
- `email` channel: reminder emails over SMTP (STARTTLS by default; refuses to send a password without TLS). With email-in on, replying `done` or `snooze 1h` acts on exactly that reminder and occurrence (the reply quotes its Message-ID; replies to an older occurrence are ignored); other replies are ignored instead of captured; HTML-only replies work (#17)
- `twilio_call` channel: Maestro calls your own number and reads the reminder out; press 1 for done or 2 to snooze 10 minutes (needs `MAESTRO_PUBLIC_URL`; signed webhook `/webhooks/twilio/voice/reminder`, acts only on that call's occurrence and once per call); works as an escalation stage (#11)
- Quiet hours: `MAESTRO_QUIET_HOURS=22:00-07:00` holds non-urgent reminders and their escalation until the window ends, then sends each once, with escalation stages timed again from the window's end; reminders starting with `!` or containing "urgent" (but not "not urgent") go through; `maestro doctor` shows the window; metric `maestro_reminders_held` (#13)
- Escalation: `MAESTRO_ESCALATION=10m=twilio_sms, 30m=telegram+twilio_sms` re-sends unacknowledged reminders on louder channels, once per stage, timed from when the first notice went out, until someone says done or snooze; only within 12 hours of the due time; works with `run`, `run --once` and `serve`; replies to escalation messages act on the reminder; metric `maestro_escalations_total{stage}` (#12)

### Fixed
- "every 0 minutes" (and "every 0 hours") no longer becomes a reminder that repeats on the same moment for ever and is re-delivered on every tick; a repeat of zero is not a repeat, as it already wasn't for days, weeks and months, so the capture is kept as ordinary text (#115)
- Done and snooze on a repeating reminder apply to that occurrence: done no longer ends the series and snooze no longer shifts its time; snoozed occurrences are re-sent at the snoozed time (migration 8, ADR 0009). New `stop` command / `POST /items/{id}/stop` / web UI button ends a series (#80)
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
