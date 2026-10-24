# Maestro Orchestrator

[![CI](https://github.com/sachin-chandani/maestro-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/sachin-chandani/maestro-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An open-source, self-hosted second brain. Tell it what to remember in plain language; it reminds you through the channels you choose (desktop, push, SMS) with keys you own.

```console
$ maestro add remind me to call mom tomorrow at 5pm
Saved: 1  ⏰ Sat 24 Oct 17:00  Call mom

$ maestro add pay rent on the 1st of every month
Saved: 2  ⏰ Sun 01 Nov 09:00  Pay rent (monthly on day 1)

$ maestro add remember that my passport is in the blue drawer
Saved: 3  📝 —                 My passport is in the blue drawer
```

**Who it's for:** people who forget things and want a private, scriptable place to dump them, plus developers who want a reminder engine they can host or embed.

**Status:** early but usable (v0.2). CLI, web UI, REST API, recurring reminders, timezones, and delivery via desktop, ntfy, Twilio SMS and Telegram work today. See the [roadmap](ROADMAP.md) for SMS and voice capture, escalation, calendar feeds and more.

## Features
- **Plain language:** `tomorrow at 5pm`, `in 2 hours`, `next friday at 10am`, `on Nov 1`, `tonight`, `at noon`, `tomorrow morning`, `by end of day`, `eow`, `this weekend`, `on the 5th`
- **Recurring:** `every day at 8am`, `every weekday at 9:30am`, `every monday`, `every other friday`, `every 3 days`, `biweekly on tuesday`, `every night`, `on the 1st of every month`, `every 2 hours`
- **Timezone-aware:** stored in UTC, shown in your timezone; "8am every day" stays 8am across DST changes
- **Notes:** anything without a time is kept and searchable
- **Channels:** console, macOS notifications, [ntfy](https://ntfy.sh) push, Twilio SMS, Telegram (with Done/snooze buttons)
- **Telegram bot:** text your own bot "dentist next tue 3pm" to capture; `/list`, `/done`, `/snooze`, `/rm`
- **Self-hosted:** one Docker container, one SQLite file, token-protected REST API and a built-in web UI
- **Reliable:** delivery is tracked per channel; a failing channel is retried with backoff (up to 5 attempts) without re-sending on the ones that worked. After downtime, recurring reminders fire once and skip ahead instead of flooding you

## Quick start (CLI)
```bash
pip install git+https://github.com/sachin-chandani/maestro-orchestrator.git
maestro add check the oven in 1 minute
maestro run          # fires reminders as they come due
```

## Self-hosting
```bash
git clone https://github.com/sachin-chandani/maestro-orchestrator.git
cd maestro-orchestrator
cp .env.example .env     # set MAESTRO_API_TOKEN, MAESTRO_TIMEZONE and any channel keys
docker compose up -d
```

Or run the published image directly (amd64 and arm64, so it works on a Raspberry Pi):
```bash
docker run -d --name maestro --env-file .env -p 127.0.0.1:8000:8000 \
  -v maestro-data:/data ghcr.io/sachin-chandani/maestro-orchestrator:latest
```

Then capture from anything that can make an HTTP request (Apple Shortcuts, Tasker, curl):
```bash
curl -X POST localhost:8000/items \
  -H "Authorization: Bearer $MAESTRO_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "dentist next tuesday at 3pm"}'
```
Open `http://localhost:8000` for the web UI (capture, browse, search, done/snooze/delete). Interactive API docs are at `http://localhost:8000/docs`.

### Configuration
All settings come from environment variables or a `.env` file. See [.env.example](.env.example).

| Variable | Purpose |
| --- | --- |
| `MAESTRO_DB` | SQLite path (default `~/.maestro/maestro.db`). Must be on a local disk or Docker volume, not NFS/SMB (WAL mode) |
| `MAESTRO_TIMEZONE` | Your IANA timezone, e.g. `Europe/London`. Defaults to the system zone, which is UTC in Docker, so set it when self-hosting |
| `MAESTRO_API_TOKEN` | Bearer token for the API; required unless bound to localhost |
| `MAESTRO_RATE_LIMIT_WEBHOOK` | Requests per minute per client on public webhooks (default 60, 0 = off) |
| `MAESTRO_RATE_LIMIT_AUTH_FAILURES` | Failed authentications per minute per client before it gets 429s on everything (default 10, 0 = off) |
| `MAESTRO_TRUST_PROXY_HEADERS` | `true` behind a reverse proxy, so limits apply per real client (last `X-Forwarded-For` entry) |
| `MAESTRO_LOG_FORMAT`, `MAESTRO_LOG_LEVEL` | `text` (default) or `json` (one object per line, for log shippers); `INFO` by default |
| `MAESTRO_CHANNELS` | Comma-separated: `console`, `macos`, `ntfy`, `twilio_sms`, `telegram` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Your bot and the only chat it will talk to (see below) |
| `NTFY_URL`, `NTFY_TOPIC` | ntfy server and topic |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `MAESTRO_SMS_TO` | SMS to and from your own phone |
| `MAESTRO_PUBLIC_URL` | How the internet reaches your server (e.g. `https://maestro.example.com`); needed for inbound webhooks behind a proxy |

### Checking your setup
```bash
maestro doctor              # timezone, database, API token, every configured channel
maestro doctor --send-test  # also delivers a test message on each channel
```
It exits non-zero if anything fails, so you can run it before a deploy (`docker compose run --rm maestro maestro doctor`).

### Monitoring
`GET /metrics` serves Prometheus metrics (same bearer token as the API): captures by source, deliveries by channel and result, reminders fired, scheduler ticks and lag, and item counts by kind and status. Alert on `time() - maestro_scheduler_last_tick_timestamp_seconds > 120` to catch a stuck scheduler.

### Telegram
1. Create a bot with [@BotFather](https://t.me/BotFather) and set `TELEGRAM_BOT_TOKEN`.
2. Start Maestro (`maestro run` or `maestro serve`) and send `/start` to your bot. It replies with your chat id.
3. Set `TELEGRAM_CHAT_ID` to that id, add `telegram` to `MAESTRO_CHANNELS`, and restart.

Now anything you text the bot is captured, reminders arrive in the chat with ✅ Done / +10m / +1h / +1d buttons, and messages from any other chat are ignored. The bot long-polls Telegram, so it needs no public URL.

### Text your reminders (inbound SMS)
With the Twilio settings above, set `MAESTRO_PUBLIC_URL` to the address the internet reaches your server at, and point your Twilio number's "A message comes in" webhook at `https://<your-server>/webhooks/twilio/sms`. Then text it:

- `dentist next tue 3pm` → saved, with a reply confirming the time
- `list`, `done 3`, `snooze 3 1h`, `rm 3`, `help`
- Reply `done` or `snooze 1h` to a reminder text to act on it without its number (the same works in Telegram). `rm` always needs the number.

Requests are verified with Twilio's signature, and only texts from `MAESTRO_SMS_TO` (your number) are acted on. `maestro doctor` prints the exact webhook URL to use.

## CLI reference
```bash
maestro add <text>            # capture a reminder or note
maestro list [--all]          # pending reminders (soonest first), then notes
maestro search <text>         # find by text
maestro snooze <id> <10m|2h|1d>
maestro done <id>
maestro rm <id>
maestro run [--once]          # scheduler only (safe alongside serve or cron: one scheduler fires at a time)
maestro serve [--host --port] # API + scheduler (needs the [server] extra)
maestro doctor [--send-test]  # check configuration and channels
```

## Using it as a library
```python
from maestro.parser import parse
from maestro.store import Store

store = Store("reminders.db")
item = store.add(parse("water the plants every saturday at 10am"))
print(item.due_at, item.recurrence)   # 2026-10-24 10:00:00 weekly:5
```

## Architecture
| Module | Role |
| --- | --- |
| `maestro/parser.py` | Plain language → reminder or note |
| `maestro/recurrence.py` | Recurrence rules and next-occurrence math |
| `maestro/store.py` | SQLite persistence with versioned migrations |
| `maestro/scheduler.py` | Fires due reminders, reschedules recurring ones |
| `maestro/notifier.py` | Delivery channels and the channel registry |
| `maestro/api.py` | FastAPI server |
| `maestro/cli.py` | Command-line interface |

## Development
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
ruff check . && mypy && pytest --cov
```
See [CONTRIBUTING.md](CONTRIBUTING.md), [ENGINEERING.md](ENGINEERING.md) for the definition of done and service objectives, and [docs/adr/](docs/adr/) for design decisions.

## License
[MIT](LICENSE)
