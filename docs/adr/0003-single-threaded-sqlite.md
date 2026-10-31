# 0003: One SQLite connection, used from one thread

**Status:** Accepted

## Context
`maestro serve` runs the API, the scheduler and the Telegram poller in one process. A shared `sqlite3` connection is not safe to use from several threads at once.

## Decision
- API handlers are `async` and run on the event loop thread; the scheduler and bot-handling loops are tasks on the same loop. The store is only touched from that thread.
- Blocking network calls that don't touch the store (Telegram long polling) run in worker threads via `asyncio.to_thread`; their results are handled back on the loop.
- Separate processes (`maestro add` while `serve` runs) use their own connections; SQLite's locking handles that, and migrations take `BEGIN IMMEDIATE`.

## Consequences
- A slow channel send blocks the loop for its duration (bounded by a 10 s timeout). Acceptable for one user; revisit with a worker if channels get slower.
- Multiple schedulers on one database must coordinate (#52).
- PostgreSQL (#34) keeps this model: one connection, used from the event-loop thread. See ADR 0010;
  a connection pool is what changes it, not the backend.
