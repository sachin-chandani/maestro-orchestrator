# 0007: One scheduler fires at a time, chosen by a lease in the database

**Status:** Accepted (#52)

## Context
`maestro serve` and `maestro run` both run a scheduler, and nothing stopped them from running against the same database, along with a cron `maestro run --once` or a second replica. Each delivered every due reminder, so users got duplicates.

## Decision
- A single-row `scheduler_lease(owner, expires_at)` table (migration 5). A scheduler takes the lease when it is free, expired or already its own, and renews it on every tick and before every reminder. The check-and-set runs under `BEGIN IMMEDIATE`, so two processes can't both see the lease as free.
- Owners are `host:pid:random`. The TTL is three tick intervals (at least 30 s): a crashed leader is replaced within that time.
- Schedulers that don't hold the lease skip the tick and log once that they are standing by. `maestro_scheduler_leader` exports the state.
- On shutdown (`run` exit, `serve` lifespan end) the lease is released so a standby takes over immediately. `maestro run --once` takes the lease for its single pass and releases it straight after, so cron can't double-send while a server runs.

## Consequences
- Exactly-once in normal operation, including restarts and crashes. A leader that freezes for longer than the TTL in the middle of a tick (e.g. a suspended VM) can overlap with its successor for one reminder; the guarantee there is still at-least-once (ENGINEERING.md).
- Two lease writes per tick plus one per due reminder: negligible at personal scale.
- Moving to Postgres (#34) can keep the same design with `SELECT ... FOR UPDATE` or an advisory lock.
