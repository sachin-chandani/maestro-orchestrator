# 0006: SQLite in WAL mode with a busy timeout

**Status:** Accepted (#53)

## Context
`maestro serve` writes from the scheduler while the API and web UI read, and `maestro add` or a second process may write at the same time. In SQLite's default rollback-journal mode a writer blocks readers while it commits, and contention surfaces as "database is locked".

## Decision
- File databases use `journal_mode=WAL`: readers see a consistent snapshot and never block the writer, or each other.
- `synchronous=NORMAL`, but only once WAL is confirmed: SQLite reports the resulting mode instead of raising, so the store checks it. If WAL can't engage (e.g. a network filesystem) it logs a warning and keeps `synchronous=FULL`, and `maestro doctor` warns.
- Every connection waits up to 5 s (`BUSY_TIMEOUT_SECONDS`) for a lock instead of failing immediately. Switching a fresh file to WAL retries within that same budget.
- In-memory databases (tests) are left as they are.

## Consequences
- The database must live on a local filesystem. WAL relies on shared memory, so NFS/SMB mounts are not supported; Docker volumes and local disks are fine. Documented in the README.
- Two extra files (`maestro.db-wal`, `maestro.db-shm`) sit next to the database. A plain `cp` of `maestro.db` while it runs can miss recent transactions: stop the server first, or use `sqlite3 maestro.db ".backup copy.db"` until `maestro backup` (#59) exists.
- WAL is persistent: once set, the file stays in WAL mode even if opened by another tool.
- Inside one `maestro serve` process everything shares a single connection (ADR 0003), so WAL helps across processes (`maestro add`, a second instance, backup tools), not between the API and scheduler of one server.
