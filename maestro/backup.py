"""One portable backup format for every storage backend (#59, ADR 0013).

`maestro backup` writes a single JSON file; `maestro restore` loads one. The file is the same
whatever the backend, so it also moves an install between SQLite and PostgreSQL. Timestamps are
UTC ISO-8601 to the second, exactly as they cross the storage interface.

The format is versioned separately from the schema: a backup made by a newer Maestro (a higher
format, an unknown table or column) is refused with a sentence, and one made by an older Maestro
gets its missing columns filled with the defaults the schema migrations would have used
(`COLUMN_DEFAULTS`). Restoring is `Store.replace_all`: one transaction, refused while a scheduler
holds the lease, old data intact if anything fails.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from maestro import __version__
from maestro.observability import METRICS
from maestro.storage.base import BACKUP_TABLES, COLUMN_DEFAULTS, Row, Store, TableSpec
from maestro.tz import UTC

if TYPE_CHECKING:
    from maestro.config import Config

log = logging.getLogger(__name__)

FORMAT = 1


class BackupError(ValueError):
    """The file is not a Maestro backup this version can read."""


def dump(store: Store) -> dict[str, Any]:
    """The whole database as one JSON-ready document."""
    return {
        "format": FORMAT,
        "maestro": __version__,
        "backend": store.backend,
        "created_at": _text(datetime.now(UTC)),
        "timezone": str(store.tz),
        "tables": {
            table.name: {
                "columns": list(table.columns),
                "rows": [[_portable(table, column, row[column]) for column in table.columns]
                         for row in store.export_rows(table)],
            }
            for table in BACKUP_TABLES
        },
    }


def write(store: Store, path: Path) -> dict[str, Any]:
    """Write a backup to `path` and return the document. The file appears whole or not at all:
    it is written next to its destination and moved into place, so a crash can't leave half a
    backup where a good one was."""
    document = dump(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".part")
    scratch.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    scratch.replace(path)
    return document


def load(path: Path) -> dict[str, Any]:
    """Read and check a backup file. Everything wrong with it is a BackupError sentence."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BackupError(f"{path} is not a Maestro backup (not valid JSON: {e})") from e
    if not isinstance(document, dict) or not isinstance(document.get("format"), int):
        raise BackupError(f"{path} is not a Maestro backup (no format number)")
    if document["format"] > FORMAT:
        raise BackupError(
            f"{path} uses backup format {document['format']}, newer than this Maestro supports "
            f"({FORMAT}); upgrade Maestro to restore it"
        )
    tables = document.get("tables")
    if not isinstance(tables, dict):
        raise BackupError(f"{path} is not a Maestro backup (no tables)")
    known = {table.name for table in BACKUP_TABLES}
    if unknown := sorted(set(tables) - known):
        raise BackupError(f"{path} holds tables this Maestro doesn't know ({', '.join(unknown)}); "
                          "it was made by a newer Maestro")
    return document


@dataclass(frozen=True)
class RestoreReport:
    """What a restore loaded: row counts per table, and how many reminders came due while the
    backup sat on disk — they are preserved and go out on the next tick, never dropped."""

    counts: dict[str, int]
    overdue: int

    @property
    def items(self) -> int:
        return self.counts.get("items", 0)


def restore(store: Store, document: dict[str, Any], now: datetime) -> RestoreReport:
    """Replace the database's contents with `document` (from `load`). One transaction; refused
    with RestoreBlockedError while a scheduler holds the lease."""
    rows = {table.name: _table_rows(table, document["tables"].get(table.name))
            for table in BACKUP_TABLES}
    store.replace_all(rows, now)
    return RestoreReport(counts={name: len(table_rows) for name, table_rows in rows.items()},
                         overdue=len(store.due(now)))


def _table_rows(table: TableSpec, data: object) -> list[Row]:
    """One table's portable rows out of the document, with an older backup's missing columns
    filled exactly as the schema migrations fill them for existing rows."""
    if data is None:
        return []  # a table this Maestro added after the backup was made starts empty
    if (not isinstance(data, dict) or not isinstance(data.get("columns"), list)
            or not isinstance(data.get("rows"), list)):
        raise BackupError(f"table {table.name} is malformed")
    columns: list[str] = data["columns"]
    if unknown := sorted(set(columns) - set(table.columns)):
        raise BackupError(f"table {table.name} has columns this Maestro doesn't know "
                          f"({', '.join(unknown)}); it was made by a newer Maestro")
    defaults = COLUMN_DEFAULTS.get(table.name, {})
    if required := sorted(set(table.columns) - set(columns) - set(defaults)):
        raise BackupError(f"table {table.name} is missing required columns ({', '.join(required)})")
    out = []
    for values in data["rows"]:
        if not isinstance(values, list) or len(values) != len(columns):
            raise BackupError(f"table {table.name} has a row that doesn't match its columns")
        row = dict(defaults)
        row.update(zip(columns, values, strict=True))
        for column in table.times:
            row[column] = _time(table, column, row[column])
        out.append(row)
    return out


def _portable(table: TableSpec, column: str, value: object) -> object:
    if column in table.times:
        assert value is None or isinstance(value, datetime)  # export_rows hands over portable rows
        return _text(value) if value else None
    return value


def _text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _time(table: TableSpec, column: str, value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as e:
        raise BackupError(f"table {table.name} holds an unreadable {column} time: {value!r}") from e


# --- scheduled backups (opt-in: MAESTRO_BACKUP_DIR) ----------------------------------------------

#: Scheduled backup files, named by when they were written. The name is what retention trusts:
#: only files matching it are ever deleted, and age comes from the name, not the filesystem.
SCHEDULED_NAME = re.compile(r"maestro-backup-(\d{8}T\d{6})Z\.json$")
#: A failed backup is retried sooner than the schedule, but not on every 30 s tick.
RETRY_EVERY = timedelta(minutes=15)


def scheduled_backups(config: Config) -> ScheduledBackups | None:
    """Scheduled backups when MAESTRO_BACKUP_DIR is set, else None. Raises ValueError when the
    settings make no sense, so `maestro run`/`serve` refuse to start half-configured."""
    from maestro.parser import parse_duration

    if not config.backup_dir:
        return None
    every = parse_duration(config.backup_every)  # a bad MAESTRO_BACKUP_EVERY raises ValueError
    if every < timedelta(minutes=15):
        raise ValueError("MAESTRO_BACKUP_EVERY must be at least 15m (unset MAESTRO_BACKUP_DIR to "
                         "turn scheduled backups off)")
    if config.backup_keep < 1:
        raise ValueError("MAESTRO_BACKUP_KEEP must keep at least 1 backup")
    return ScheduledBackups(Path(config.backup_dir).expanduser(), every, config.backup_keep)


@dataclass
class ScheduledBackups:
    """Writes a backup into `directory` every `every`, keeping the newest `keep` (#59).

    Driven by the scheduler after a tick, and only while it holds the lease, so several processes
    on one database produce one backup series. Age is read from the newest file's name, not from
    process state: a restart doesn't reset the clock, and a standby that takes over carries on
    where the old leader left off. A failure is logged and counted, never raised — a full disk
    must not stop reminders — and retried sooner than the schedule.
    """

    directory: Path
    every: timedelta
    keep: int
    _attempted_at: datetime | None = field(default=None, repr=False)

    def describe(self) -> str:
        hours = self.every.total_seconds() / 3600
        every = f"{hours:g}h" if hours >= 1 else f"{int(self.every.total_seconds() // 60)}m"
        return f"every {every} into {self.directory}, keeping {self.keep}"

    def written(self) -> list[tuple[datetime, Path]]:
        """The scheduled backups on disk, oldest first, dated by their names."""
        found = []
        for path in self.directory.glob("maestro-backup-*.json"):
            if match := SCHEDULED_NAME.fullmatch(path.name):
                found.append((datetime.strptime(match[1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC), path))
        return sorted(found)

    def due(self, now: datetime) -> bool:
        if self._attempted_at and now - self._attempted_at < RETRY_EVERY:
            return False  # a failure was just logged; don't repeat it every tick
        try:
            newest = max((at for at, _ in self.written()), default=None)
        except OSError:
            return True  # can't read the directory: trying will say why, once per RETRY_EVERY
        return newest is None or now - newest >= self.every

    def maybe_run(self, store: Store, now: datetime | None = None) -> Path | None:
        """Write a backup if one is due. Called by the scheduler that holds the lease."""
        now = now or datetime.now(UTC)
        if not self.due(now):
            return None
        self._attempted_at = now
        path = self.directory / f"maestro-backup-{now.astimezone(UTC):%Y%m%dT%H%M%S}Z.json"
        try:
            write(store, path)
            self._prune()
        except Exception:  # a full disk — or anything else — must not stop the scheduler
            METRICS.inc("maestro_backup_errors_total")
            log.exception("Scheduled backup to %s failed; retrying in %s", path, RETRY_EVERY)
            return None
        METRICS.set("maestro_backup_last_success_timestamp_seconds", now.timestamp())
        log.info("Backed up the database to %s", path)
        return path

    def _prune(self) -> None:
        for _, path in self.written()[: -self.keep]:
            path.unlink(missing_ok=True)  # only names that match SCHEDULED_NAME, never other files
