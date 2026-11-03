# 0013: One portable backup format, restored transactionally

**Status:** Accepted (#59)

## Context
Issue #59 was written when SQLite was the only backend and proposed its online backup API. Since then PostgreSQL landed (ADR 0010), which that API does nothing for, and ADR 0010 deferred "moving an install between backends" to this issue. Off-site backups (#37) will encrypt and upload whatever this issue produces. ADR 0006 already warns that copying `maestro.db` while it runs can miss transactions, so people need a supported way out.

## Decision
- **One versioned JSON file for every backend**, not a native file per backend. `maestro backup <file>` writes it, `maestro restore <file>` loads it. A header carries the format number, Maestro version, backend, timezone and creation time; each table carries its column list and rows, timestamps as UTC ISO-8601 to the second. The same file restores into SQLite or PostgreSQL, which is also the migration path between them.
- **The table list lives next to the storage interface** (`BACKUP_TABLES` in `maestro.storage.base`), with which columns are timestamps and booleans. A migration that adds a column must extend it; tests compare the spec against the real schema so forgetting fails CI, not a future restore.
- **The scheduler lease is not backed up.** It names a process on a machine, not data.
- **Restore is one transaction and refuses while a scheduler runs.** `Store.replace_all` checks the lease *inside* the same transaction (`BEGIN IMMEDIATE` on SQLite, the lease advisory lock on PostgreSQL), so a scheduler can't start between check and load; a crash mid-restore leaves the old data untouched. There is no `--force`: a crashed holder's lease expires within its TTL anyway.
- **Ids are preserved and never reused.** They live on in Telegram buttons, reply flows and the web UI. After a restore, new items get ids above anything the database ever handed out, even ids the backup no longer contains (SQLite's AUTOINCREMENT does this by itself; PostgreSQL's sequence is moved forward, never back).
- **Restore first saves what it replaces** to `maestro-pre-restore-<utc-time>.json` next to the backup — a full backup in the same format, so the undo is just another restore. Skipped when the database is empty.
- **A backup from another version is handled, not guessed at.** A newer format, table or column is refused with a sentence. An older backup gets missing columns filled with exactly the defaults the schema migrations give existing rows (`COLUMN_DEFAULTS`, test-checked against the schema), and a table this Maestro added later starts empty.
- **Reminders that came due while the backup sat on disk are reported and kept.** Restore prints how many; the next tick sends them. Dropping them silently would break the delivery guarantee.
- **Scheduled backups are opt-in and run on the leader.** `MAESTRO_BACKUP_DIR` turns them on; `MAESTRO_BACKUP_EVERY` (default 24h) and `MAESTRO_BACKUP_KEEP` (default 7) shape them. The scheduler holding the lease writes one after a tick when the newest file — dated by its name, `maestro-backup-<utc-time>.json`, not by filesystem metadata — is older than the interval, so restarts don't reset the clock and several processes produce one series. Retention deletes only files matching that exact name. A failure is logged and counted (`maestro_backup_errors_total`), retried after 15 minutes rather than every tick, and `maestro_backup_last_success_timestamp_seconds` lets an alert catch backups that silently stop.

## Consequences
- The backup is not a byte-exact copy and is slower to write than SQLite's backup API; at this product's data sizes (thousands of rows) that is milliseconds, and in exchange the file is one format to test, encrypt (#37) and restore anywhere.
- Restoring moves data, not settings: `.env` is still the operator's to carry.
- `maestro run --once` never writes scheduled backups; cron setups can run `maestro backup` on their own schedule.
- The issue's original `sqlite3 .backup` suggestion in ADR 0006's consequences is superseded by `maestro backup`.
- Restore wipes and reloads: anything captured after the backup was written is gone (into the pre-restore file). The overdue report and the pre-restore file are the guard rails.
