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

**Status:** usable (v0.3). Capture from the CLI, web UI, API, Telegram, SMS, phone calls, email and phone shortcuts; recurring reminders and timezones; delivery via desktop, ntfy, Twilio SMS and Telegram; exactly-once scheduling. See the [roadmap](ROADMAP.md) for escalation, calendar feeds, a PWA and more.

## Features
- **Plain language:** `tomorrow at 5pm`, `in 2 hours`, `next friday at 10am`, `on Nov 1`, `tonight`, `at noon`, `tomorrow morning`, `by end of day`, `eow`, `this weekend`, `on the 5th`, `around 5pm`, and hours without am/pm the way people say them (`at 6` is evening, `at 9` morning, `at 8 tonight` 20:00). `next friday` means the coming Friday. Measured against a 200+ phrase corpus (`tests/corpus/phrases.txt`)
- **Recurring:** `every day at 8am`, `every weekday at 9:30am`, `every monday`, `every other friday`, `every 3 days`, `biweekly on tuesday`, `every night`, `on the 1st of every month`, `every 2 hours`
- **Timezone-aware:** stored in UTC, shown in your timezone; "8am every day" stays 8am across DST changes
- **Notes:** anything without a time is kept and searchable
- **Channels:** console, macOS notifications, [ntfy](https://ntfy.sh) push, Twilio SMS, Telegram (with Done/snooze buttons), email (reply to act), webhooks (Slack, Discord, Home Assistant, anything)
- **Email-in:** forward or send an email to a dedicated inbox to capture it
- **Voice:** call your Twilio number and say the reminder; it's read back to you. Or have Maestro call you: press 1 for done, 2 to snooze
- **Telegram bot:** text your own bot "dentist next tue 3pm" to capture; `/list`, `/done`, `/snooze`, `/rm`
- **Self-hosted:** one Docker container, one SQLite file, token-protected REST API and a built-in web UI
- **Escalation:** unacknowledged reminders move to louder channels on a schedule you set (push, then SMS, then more)
- **Quiet hours:** non-urgent reminders wait until morning; start one with `!` to let it through
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
Open `http://localhost:8000` for the web UI (capture, browse, search, done/snooze/delete). Interactive API docs are at `http://localhost:8000/docs`. To capture from your phone with Apple Shortcuts or Android Tasker, see [docs/integrations/phone-shortcuts.md](docs/integrations/phone-shortcuts.md).

### Configuration
All settings come from environment variables or a `.env` file. See [.env.example](.env.example).

| Variable | Purpose |
| --- | --- |
| `MAESTRO_DB` | SQLite path (default `~/.maestro/maestro.db`). Must be on a local disk or Docker volume, not NFS/SMB (WAL mode) |
| `MAESTRO_TIMEZONE` | Your IANA timezone, e.g. `Europe/London`. Defaults to the system zone, which is UTC in Docker, so set it when self-hosting |
| `MAESTRO_API_TOKEN` | Bearer token for the API; required unless bound to localhost |
| `MAESTRO_RATE_LIMIT_WEBHOOK` | Webhook requests with an invalid signature allowed per minute per client (default 60, 0 = off). Genuine Twilio callbacks are never limited |
| `MAESTRO_RATE_LIMIT_AUTH_FAILURES` | Wrong API tokens allowed per minute per client before it is locked out of the API (default 10, 0 = off). A missing token doesn't count |
| `MAESTRO_TRUST_PROXY_HEADERS` | `true` behind a reverse proxy, so limits apply per real client (last `X-Forwarded-For` entry) |
| `MAESTRO_LOG_FORMAT`, `MAESTRO_LOG_LEVEL` | `text` (default) or `json` (one object per line, for log shippers); `INFO` by default |
| `MAESTRO_CHANNELS` | Comma-separated: `console`, `macos`, `ntfy`, `twilio_sms`, `twilio_call`, `telegram`, `email`, `webhook` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Your bot and the only chat it will talk to (see below) |
| `NTFY_URL`, `NTFY_TOPIC` | ntfy server and topic |
| `MAESTRO_WEBHOOK_URL`, `MAESTRO_WEBHOOK_FORMAT`, `MAESTRO_WEBHOOK_SECRET` | `webhook` channel: POST each reminder as `json` (default), or in `slack` / `discord` incoming-webhook format. HTTPS required except on your own network (e.g. Home Assistant). With a secret, requests carry `X-Maestro-Timestamp` and `X-Maestro-Signature: sha256=HMAC(secret, "<timestamp>.<body>")` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `MAESTRO_EMAIL_TO` | Reminder emails (`email` channel). With email-in on, reply `done` or `snooze 1h` to act on that exact reminder |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `MAESTRO_SMS_TO` | SMS to and from your own phone |
| `MAESTRO_PUBLIC_URL` | How the internet reaches your server (e.g. `https://maestro.example.com`); needed for inbound webhooks behind a proxy |

### Escalation
If a reminder isn't acknowledged (marked done or snoozed, from anywhere), Maestro can try louder channels:

```bash
MAESTRO_ESCALATION="10m=twilio_sms, 30m=telegram+twilio_sms"
```

That sends an SMS 10 minutes after the first notice went out, then Telegram and another SMS at 30 minutes, each once, prefixed "Still waiting:". Replying `done` to any of them stops the rest. Only reminders from the last 12 hours escalate, so turning this on never pages you about old ones. `maestro doctor` prints the plan.

### Quiet hours
```bash
MAESTRO_QUIET_HOURS=22:00-07:00   # local time (MAESTRO_TIMEZONE); may wrap past midnight
```

During the window, reminders that come due wait until it ends, on every channel, and so does escalation. Each held reminder is sent once at the end of the window, so an hourly reminder sends a single notice at 07:00, not eight. Escalation stages are timed again from the end of the window, so they stay spaced out (with `10m=twilio_sms`, the SMS comes at 07:10). A reminder is urgent, and goes through anyway, if its text starts with `!` or contains the word "urgent" ("not urgent" doesn't count): `maestro add "!take insulin at 11pm"`. The metric `maestro_reminders_held` shows how many are waiting.

### Checking your setup
```bash
maestro doctor              # timezone, database, API token, every configured channel
maestro doctor --send-test  # also delivers a test message on each channel
```
It exits non-zero if anything fails, so you can run it before a deploy (`docker compose run --rm maestro maestro doctor`).

### Monitoring
`GET /metrics` serves Prometheus metrics (same bearer token as the API): captures by source, deliveries by channel and result, reminders fired, acknowledgements and reminders still awaiting one, scheduler ticks and lag, and item counts by kind and status. `GET /items?unacknowledged=true` lists reminders that were sent but not yet marked done or snoozed. Alert on `time() - maestro_scheduler_last_tick_timestamp_seconds > 120` to catch a stuck scheduler.

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

### Call your reminders in (inbound voice)
With the same Twilio settings, point the number's "A call comes in" webhook at `https://<your-server>/webhooks/twilio/voice`. Call it from your phone, say "remind me to call mom tomorrow at 5 p.m.", and Maestro reads back what it saved. Calls from other numbers are rejected without being answered, and so are calls whose caller ID failed the carrier's STIR/SHAKEN check. Caller ID can still be spoofed where carriers don't check it, so treat voice capture as convenient rather than strongly authenticated; a PIN option is tracked in the issues.

### Reminders as phone calls
Add `twilio_call` to `MAESTRO_CHANNELS` (or use it as an escalation stage: `MAESTRO_ESCALATION="10m=twilio_sms, 30m=twilio_call"`). With the same Twilio settings, Maestro calls `MAESTRO_SMS_TO` and reads the reminder out. With `MAESTRO_PUBLIC_URL` set (pointing at `maestro serve`, which handles the key press), press 1 to mark it done or 2 to snooze it for 10 minutes; without it, the reminder is read twice and the call ends. The key press only acts on the reminder that call was about, and only if it hasn't been delivered again since. A placed call counts as delivered even if you don't answer, so pair calls with escalation for things that must not be missed. Calls only ever go to your own number.

### Email your reminders (email-in)
Create a dedicated inbox for Maestro, set `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `MAESTRO_EMAIL_FROM` (your own addresses) and `MAESTRO_EMAIL_AUTHSERV_ID` (the first word of the `Authentication-Results` header your provider adds to mail you receive, e.g. `mx.google.com`), and send or forward mail to it. The subject is captured ("Re:"/"Fwd:" removed), or the first line of the body if there's no subject. Only your addresses count, and only when your provider confirms the sender with DMARC, so a forged `From` (or a forged authentication header) is ignored ([ADR 0008](docs/adr/0008-email-sender-trust.md)). `maestro doctor --send-test` checks the IMAP login.

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
