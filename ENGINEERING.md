# Engineering standards

How Maestro is built: what "done" means, what we promise users, and how changes are reviewed.

## Service objectives
These are the promises a self-hosted instance should keep. Each one has a metric or test behind it.

| Objective | Target | How it is checked |
|---|---|---|
| Reminders fire on time | Delivered within 60 s of the due time (default tick is 30 s) | `maestro_scheduler_lag_seconds`; alert when `time() - maestro_scheduler_last_tick_timestamp_seconds > 120` |
| Delivery guarantee | At least once per channel; never silently dropped. A crash between sending and recording can repeat one message. | Delivery tests in `tests/test_delivery.py`; give-ups are logged and counted (`result="gave_up"`) |
| No double delivery | One scheduler fires at a time, even with several processes on one database | `maestro_scheduler_leader`; `tests/test_lease.py`; ADR 0007 |
| Durable data | Nothing acknowledged to the user is lost across restarts or upgrades | Migrations tested from every previous schema; WAL (#53); backups (#59); restart and rollback tests per backend (`tests/test_storage_postgres.py`) |
| Loops keep running | One corrupt item, failed tick or malformed chat update is logged, counted and skipped; it never stops the scheduler or the bot | `maestro_scheduler_errors_total`; `tests/test_resilience.py` |
| Correct times | Wall-clock times survive DST and server timezones | `tests/test_timezones.py`, property tests (#56) |
| Understands people | At least 95% of the parser corpus parses exactly as expected, and no miss goes unlisted | `tests/test_corpus.py` over `tests/corpus/phrases.txt`; add every real-world misparse to the corpus |
| Safe to expose | Every non-public route needs the API token; webhooks need a valid provider signature; wrong tokens and forged webhooks are rate limited per client, atomically, without ever throttling genuine provider callbacks | `tests/test_api.py`, `tests/test_sms.py`, `tests/test_ratelimit.py`; `maestro_rate_limited_total` |

## Definition of done
A change is done when all of these hold:

1. **Behaviour is tested.** New behaviour has tests, every bug fix has a regression test that fails without the fix, and nothing in the test suite touches the network.
2. **CI is green.** Lint (ruff), types (mypy with `check_untyped_defs`, no blanket ignores), tests on Python 3.11–3.13, coverage at or above the gate (96%, only ever raised), dependency audit, and Docker build.
3. **Docs match.** README for anything user-visible, `.env.example` for new settings, `CHANGELOG.md` under `[Unreleased]`, `ROADMAP.md` checkbox.
4. **Operable.** New background behaviour logs failures and has a metric if it can silently stop working. A problem an operator can fix — an unreachable database, a bad setting — is reported as a sentence with a non-zero exit code, not a traceback.
5. **Decisions are written down.** A choice that constrains future work gets an ADR in `docs/adr/`.
6. **Reviewed.** The PR description states what changed, how it was tested, and the risks; the diff gets a self-review against the checklist below before merge.

## Review checklist
- **Correctness:** edge cases (empty input, past times, month ends, DST), idempotency, ordering.
- **Failure modes:** what happens when a provider is down, slow, or returns garbage? When the process dies mid-operation?
- **Security:** anything from the network (API bodies, webhooks, chat messages) is untrusted. Auth checks are constant-time. No secrets in logs or URLs.
- **Data:** migrations are forward-only, run inside the migration transaction, and are tested from the previous schema with real-looking rows.
- **Time:** datetimes are timezone-aware; storage is UTC; user-facing times are in `MAESTRO_TIMEZONE`.
- **Compatibility:** CLI flags, REST routes, env vars and the Python API don't change without a CHANGELOG entry.
- **Scope:** unrelated cleanups go in their own PR; new ideas become issues.

## Testing strategy
- **Unit and integration tests** with fakes for every external service: each integration takes an injectable transport or opener.
- **Signed-request tests** for webhooks, with signatures cross-checked against the provider's official SDK.
- **Property-based tests** (Hypothesis, `tests/test_properties.py`) for invariants in recurrence, time handling, storage and the parser, across awkward zones (30-minute DST, 45-minute offsets, southern-hemisphere DST).
- **Parser corpus** (`tests/corpus/phrases.txt`, 200+ phrases with what a person means) with a 95% floor; any miss not listed in `KNOWN_GAPS` fails. When a real phrase is misparsed, add it to the corpus first.
- **One conformance suite per storage backend** (`tests/test_storage_conformance.py`): every backend runs the same tests through `maestro.storage.Store`, so "it works on SQLite" is never taken as evidence about PostgreSQL. What only a real server can show — connecting, migrating, restarts, locking between processes — is in `tests/test_storage_postgres.py`, which needs a disposable database (`MAESTRO_TEST_POSTGRES_URL`, started by CI) and skips without one.
- **No leaked connections:** a `Store` opened by product code and not closed fails the test that opened it.
- **Live checks** only through `maestro doctor --send-test`, run by a person with real credentials.

## Workflow
- One issue per branch (`issue-<N>-<slug>`), one PR per issue, `Fixes #N` in the description.
- Merge commits, not squash, so history shows the real steps.
- Releases follow [CONTRIBUTING.md](CONTRIBUTING.md#releasing); each milestone ends with a release and its exit criteria met.
