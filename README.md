# Maestro Orchestrator

A personal second brain. Tell it what to remember in plain language and it reminds you at the right time.

```console
$ maestro add remind me to call mom tomorrow at 5pm
Saved: 1  ⏰ Sat 24 Oct 17:00  Call mom

$ maestro add remember that my passport is in the blue drawer
Saved: 2  📝 —                 My passport is in the blue drawer

$ maestro search passport
   2  📝 —                 My passport is in the blue drawer
```

**Who it's for:** anyone who forgets things and wants a lightweight, local-first place to dump them, without signing up for another app.

**Status:** early. The CLI, parser, store and scheduler work today; voice, phone and calendar channels are on the [roadmap](ROADMAP.md).

## Features
- Understands phrases like `tomorrow at 5pm`, `in 2 hours`, `next friday at 10am`, `on Nov 1`, `tonight`, `at noon`
- Anything without a time is kept as a note you can search later
- Reminders fire once when due, as macOS notifications (console elsewhere)
- All data in a single SQLite file (`~/.maestro/maestro.db` by default)

## Built with
Python 3.11+, SQLite, [dateparser](https://github.com/scrapinghub/dateparser), pytest.

## Getting started
```bash
git clone https://github.com/sachin-chandani/maestro-orchestrator.git
cd maestro-orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

## Usage
```bash
maestro add pay rent on Nov 1     # capture a reminder or note
maestro list                      # pending reminders (soonest first), then notes
maestro list --all                # include completed items
maestro search rent               # find by text
maestro done 3                    # mark as done
maestro rm 3                      # delete
maestro run                       # keep running and fire reminders as they come due
maestro run --once                # check once (useful from cron)
```

Use `--db PATH` or the `MAESTRO_DB` environment variable to point at a different database.

## Development
```bash
pytest
```

Code layout:

| Module | Role |
| --- | --- |
| `maestro/parser.py` | Plain language → reminder or note |
| `maestro/store.py` | SQLite persistence |
| `maestro/scheduler.py` | Fires due reminders |
| `maestro/notifier.py` | Delivery channels |
| `maestro/cli.py` | Command-line interface |

## License
[MIT](LICENSE)
