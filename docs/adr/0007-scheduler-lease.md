# 0007: One scheduler fires at a time, chosen by a lease in the database

**Status:** Accepted (#52)

## Context
`maestro serve` and `maestro run` both run a scheduler, and nothing stopped them from running against the same database, along with a cron `maestro run --once` or a second replica. Each delivered every due reminder, so users got duplicates.

## Decision
- A single-row `scheduler_lease(owner, expires_at)` table (migration 5). A scheduler takes the lease when it is free, expired or already its own. The check-and-set runs under `BEGIN IMMEDIATE`, so two processes can't both see the lease as free.
- The leader checks the lease at the start of every tick and before every reminder, and renews it (at most every ttl/3) against the **real current time**, so a long tick with slow channels keeps the lease. Standbys check with a plain read and only take the write lock when the lease looks free.
- Owners are `host:pid:random`. The TTL is three tick intervals (at least 30 s): a crashed leader is replaced within that time.
- Schedulers that don't hold the lease skip the tick and log once that they are standing by. `maestro_scheduler_leader` exports the state.
- On shutdown (`run` exit including SIGTERM, `serve` lifespan end) the lease is released so a standby takes over immediately. `maestro run --once` takes the lease for its single pass and releases it straight after, so cron can't double-send while a server runs.

## Consequences
- Exactly-once in normal operation, including restarts and crashes. A leader that freezes for longer than the TTL in the middle of a tick (e.g. a suspended VM) can overlap with its successor for one reminder; the guarantee there is still at-least-once (ENGINEERING.md).
- Cheap: the leader writes the lease at most once per ttl/3, standbys only read until it looks free.
- Expiry compares wall clocks across processes. On one host that's exact; across hosts, clocks must be NTP-synced to well within the TTL.
- An older Maestro that doesn't know the lease would ignore it, so the store refuses to open a database whose schema is newer than it supports (`SchemaTooNewError`). Releases before 0.3 lack this guard: stop every old process before upgrading.
- Moving to Postgres (#34) can keep the same design with `SELECT ... FOR UPDATE` or an advisory lock.
