# 0012: SIGTERM finishes the work in flight, then stops

**Status:** Accepted (#60)

## Context
`docker stop`, `systemctl stop` and a Kubernetes rollout send SIGTERM and then kill the process after a grace period. Since #65, `maestro run` exited on SIGTERM and released the scheduler lease, but it exited *immediately*: a reminder whose send had gone out and whose delivery had not yet been recorded was cut off, and the next scheduler sent it again. `maestro serve` cancelled its background tasks without waiting for them, so the lease release in the scheduler task's `finally` might never run, and the Telegram long poll in the default executor could hold the process open for its full 35 s.

The delivery guarantee in ENGINEERING.md is at-least-once per channel, never silently dropped. A restart is the most common moment to break it.

## Decision
- **One `Shutdown` per process** (`maestro.shutdown`). The signal handler requests a stop; the loops check it between units of work. Stretches that must finish once started are marked `busy()`: a tick (a send and the record of it), handling a batch of chat updates, capturing fetched mail.
- **"Finish the current tick" means finish the reminder in flight, not every due reminder.** The scheduler stops *between* reminders, after the one being delivered is recorded and its occurrence advanced. Reminders still due stay due; the next leader takes them within one tick. Finishing the whole tick could run past the grace period with several slow channels and be killed mid-send, which is what this ADR is meant to prevent.
- **Then stop accepting new work and release the lease.** `maestro run` returns through its normal path, so `run_forever`'s `finally` releases the lease and the store is closed. `maestro serve` awaits its cancelled tasks so the same release happens before the event loop closes; uvicorn stops accepting connections on its own.
- **An idle wait is interrupted at once.** In `maestro run`, SIGTERM during `sleep` or a Telegram long poll raises `SystemExit(0)`, as before. In `maestro serve`, our handler runs first (to let a tick in progress stop between reminders) and then calls uvicorn's, which ends serving.
- **Telegram:** the long poll may be abandoned; updates it had not confirmed are re-sent by Telegram on the next start. A batch already fetched is handled to the end, because `getUpdates` with the next offset is what confirms it, and that offset is lost with the process. In `serve` the poll runs in a daemon thread rather than the default executor, so exit does not wait for it.
- **Email:** `process()` once started is finished. The IMAP thread is not waited on; mail it fetched but did not hand over stays unread and is captured on the next start, deduplicated by Message-ID.
- **Docker:** `stop_grace_period: 45s` in `docker-compose.yml`, enough for one reminder across every channel at the 10 s send timeout plus the shutdown itself. The default 10 s is not.

## Consequences
- A stop takes as long as the longest single send, bounded by the channel timeout; it never takes a whole tick.
- The guarantee is unchanged and now documented in the README: at-least-once per channel. A `kill -9` or power loss between a send and its record can still repeat one message; nothing is dropped.
- `run_forever`, `tick` and `fire_due` take an optional `shutdown`; callers that pass none behave as before.
- Ctrl+C (SIGINT) is still immediate: it is a person at a terminal, not an orchestrator.
