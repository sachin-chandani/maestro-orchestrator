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

**Status:** early but usable. CLI, REST API, recurring reminders, ntfy and Twilio SMS work today. See the [roadmap](ROADMAP.md) for Telegram, voice calls, web UI and more.

## Features
- **Plain language:** `tomorrow at 5pm`, `in 2 hours`, `next friday at 10am`, `on Nov 1`, `tonight`, `at noon`
- **Recurring:** `every day at 8am`, `every weekday at 9:30am`, `every monday`, `on the 1st of every month`, `every 2 hours`
- **Timezone-aware:** stored in UTC, shown in your timezone; "8am every day" stays 8am across DST changes
- **Notes:** anything without a time is kept and searchable
- **Channels:** console, macOS notifications, [ntfy](https://ntfy.sh) push, Twilio SMS
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
cp .env.example .env     # set MAESTRO_API_TOKEN and any channel keys
docker compose up -d
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
| `MAESTRO_DB` | SQLite path (default `~/.maestro/maestro.db`) |
| `MAESTRO_TIMEZONE` | Your IANA timezone, e.g. `Europe/London`. Defaults to the system zone, which is UTC in Docker, so set it when self-hosting |
| `MAESTRO_API_TOKEN` | Bearer token for the API; required unless bound to localhost |
| `MAESTRO_CHANNELS` | Comma-separated: `console`, `macos`, `ntfy`, `twilio_sms` |
| `NTFY_URL`, `NTFY_TOPIC` | ntfy server and topic |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `MAESTRO_SMS_TO` | SMS to your own phone |

## CLI reference
```bash
maestro add <text>            # capture a reminder or note
maestro list [--all]          # pending reminders (soonest first), then notes
maestro search <text>         # find by text
maestro snooze <id> <10m|2h|1d>
maestro done <id>
maestro rm <id>
maestro run [--once]          # scheduler only
maestro serve [--host --port] # API + scheduler (needs the [server] extra)
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
pytest && ruff check .
```
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License
[MIT](LICENSE)
