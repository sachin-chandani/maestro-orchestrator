# 0006: SQLite in WAL mode with a busy timeout

**Status:** Accepted (#53)

## Context
`maestro serve` writes from the scheduler while the API and web UI read, and `maestro add` or a second process may write at the same time. In SQLite's default rollback-journal mode a writer blocks readers while it commits, and contention surfaces as "database is locked".

## Decision
- File databases use `journal_mode=WAL`: readers see a consistent snapshot and never block the writer, or each other.
- `synchronous=NORMAL`: durable across application crashes; with WAL, a power loss can lose at most the last transactions, never corrupt the database.
- Every connection waits up to 5 s (`BUSY_TIMEOUT_SECONDS`) for a lock instead of failing immediately.
- In-memory databases (tests) are left as they are.

## Consequences
- The database must live on a local filesystem. WAL relies on shared memory, so NFS/SMB mounts are not supported; Docker volumes and local disks are fine. Documented in the README.
- Two extra files (`maestro.db-wal`, `maestro.db-shm`) sit next to the database. Copy the database with `maestro backup` (#59), not `cp`, while it's running.
- WAL is persistent: once set, the file stays in WAL mode even if opened by another tool.
