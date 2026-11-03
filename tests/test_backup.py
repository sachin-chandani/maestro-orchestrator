"""#59: the portable backup file — written whole, read back exactly, refused when it can't be
honoured, and kept in step with the real schema."""

import json
from datetime import timedelta

import pytest
from conftest import TZ, dt

from maestro import backup
from maestro.models import Delivery, Item, Kind, Status
from maestro.storage import BACKUP_TABLES, COLUMN_DEFAULTS, SQLiteStore
from maestro.storage.base import RestoreBlockedError

NOW = dt(2026, 10, 26, 9, 0)
LATER = dt(2026, 10, 26, 17, 0)


@pytest.fixture
def store():
    with SQLiteStore(":memory:", tz=TZ) as s:
        yield s


def populate(store):
    pills = store.add(Item(text="Take pills", kind=Kind.REMINDER, created_at=NOW, due_at=NOW,
                           recurrence="daily", source_text="take pills daily at 9am"))
    store.add(Item(text="Insulin", kind=Kind.REMINDER, created_at=NOW, due_at=LATER,
                   channels=["twilio_sms", "email"], urgent=True))
    store.add(Item(text="Gate code 4321", kind=Kind.NOTE, created_at=NOW))
    store.record_delivery(pills.id, NOW, Delivery(channel="console", attempts=1, delivered_at=NOW))
    store.complete(pills.id, via="cli", now=NOW + timedelta(minutes=1))
    store.claim_inbound("twilio_sms", "SM123", pills.id)
    return pills


def write_and_load(store, tmp_path):
    path = tmp_path / "maestro-backup.json"
    backup.write(store, path)
    return backup.load(path)


# --- the file ----------------------------------------------------------------------------------


def test_a_backup_round_trips_through_a_file_into_another_database(store, tmp_path):
    pills = populate(store)
    document = write_and_load(store, tmp_path)

    with SQLiteStore(":memory:", tz=TZ) as other:
        report = backup.restore(other, document, now=NOW)
        assert other.list(include_done=True) == store.list(include_done=True)
        assert other.deliveries(pills.id, NOW) == store.deliveries(pills.id, NOW)
        assert other.acknowledgement(pills.id, NOW) == store.acknowledgement(pills.id, NOW)
        assert other.is_claimed("twilio_sms", "SM123")
    assert report.items == 3 and report.counts["deliveries"] == 1


def test_the_file_appears_whole_or_not_at_all(store, tmp_path):
    path = tmp_path / "backups" / "maestro-backup.json"  # the directory is created too
    backup.write(store, path)
    assert json.loads(path.read_text())["format"] == backup.FORMAT
    assert list(path.parent.iterdir()) == [path]  # no .part scratch file left behind

    populate(store)
    backup.write(store, path)  # overwriting goes through the same rename
    assert len(json.loads(path.read_text())["tables"]["items"]["rows"]) == 3


def test_timestamps_are_stored_as_utc_to_the_second(store, tmp_path):
    populate(store)
    document = write_and_load(store, tmp_path)
    (due,) = {row[4] for row in document["tables"]["items"]["rows"] if row[1] == "Insulin"}
    assert due == "2026-10-26T21:00:00+00:00"  # 5pm New York, said in UTC


def test_the_header_says_what_made_the_backup(store, tmp_path):
    document = write_and_load(store, tmp_path)
    assert document["backend"] == "sqlite" and document["timezone"] == TZ.key
    assert document["maestro"] and document["created_at"].endswith("+00:00")


def test_the_scheduler_lease_is_not_backed_up(store, tmp_path):
    assert store.acquire_lease("serve-1", NOW, NOW + timedelta(days=365))[0]
    document = write_and_load(store, tmp_path)
    assert "scheduler_lease" not in document["tables"]


# --- what restore reports ----------------------------------------------------------------------


def test_restore_counts_reminders_that_came_due_while_the_backup_sat_on_disk(store, tmp_path):
    store.add(Item(text="Due during the gap", kind=Kind.REMINDER, created_at=NOW, due_at=LATER))
    store.add(Item(text="Still in the future", kind=Kind.REMINDER, created_at=NOW,
                   due_at=NOW + timedelta(days=30)))
    document = write_and_load(store, tmp_path)

    with SQLiteStore(":memory:", tz=TZ) as other:
        report = backup.restore(other, document, now=LATER + timedelta(days=2))
        assert report.overdue == 1
        # Preserved, not skipped: the next tick sends it (the delivery guarantee survives a restore).
        assert [i.text for i in other.due(LATER + timedelta(days=2))] == ["Due during the gap"]


def test_restore_is_refused_while_a_scheduler_runs(store, tmp_path):
    document = write_and_load(store, tmp_path)
    populate(store)
    assert store.acquire_lease("serve-1", NOW, NOW + timedelta(seconds=90))[0]
    with pytest.raises(RestoreBlockedError, match="serve-1"):
        backup.restore(store, document, now=NOW)
    assert len(store.list(include_done=True)) == 3  # nothing was touched


# --- backups from another version --------------------------------------------------------------


def older_backup(document, table, *dropped):
    """The same backup as a Maestro without `dropped` columns would have written it."""
    data = document["tables"][table]
    keep = [i for i, column in enumerate(data["columns"]) if column not in dropped]
    data["columns"] = [data["columns"][i] for i in keep]
    data["rows"] = [[row[i] for i in keep] for row in data["rows"]]
    return document


def test_missing_columns_get_the_exact_defaults_a_migration_would_give(store, tmp_path):
    populate(store)
    document = write_and_load(store, tmp_path)
    older_backup(document, "items", "source_text", "recurrence", "snoozed_until", "channels", "urgent")
    older_backup(document, "deliveries", "attempts", "delivered_at", "next_attempt_at", "last_error")

    with SQLiteStore(":memory:", tz=TZ) as other:
        backup.restore(other, document, now=NOW)
        insulin = next(i for i in other.list() if i.text == "Insulin")
        # urgent=None means "judge by the text", channels=None means MAESTRO_CHANNELS — exactly
        # what migration 9 gives rows that predate the flags (ADR 0011).
        assert insulin.urgent is None and insulin.channels is None
        assert insulin.source_text == "" and insulin.recurrence is None
        (delivery,) = other.deliveries(1, NOW).values()
        assert delivery == Delivery(channel="console")  # attempts=0 and nothing else, as migration 3


def test_a_table_this_maestro_added_later_starts_empty(store, tmp_path):
    populate(store)
    document = write_and_load(store, tmp_path)
    del document["tables"]["acknowledgements"]

    with SQLiteStore(":memory:", tz=TZ) as other:
        report = backup.restore(other, document, now=NOW)
        assert report.items == 3 and report.counts["acknowledgements"] == 0


def test_a_newer_format_is_refused(store, tmp_path):
    document = write_and_load(store, tmp_path)
    document["format"] = backup.FORMAT + 1
    with pytest.raises(backup.BackupError, match="newer than this Maestro supports"):
        _reload(document, tmp_path)


def test_an_unknown_table_is_refused(store, tmp_path):
    document = write_and_load(store, tmp_path)
    document["tables"]["attachments"] = {"columns": [], "rows": []}
    with pytest.raises(backup.BackupError, match="attachments.*newer Maestro"):
        _reload(document, tmp_path)


def test_an_unknown_column_is_refused(store, tmp_path):
    populate(store)
    document = write_and_load(store, tmp_path)
    document["tables"]["items"]["columns"].append("priority")
    for row in document["tables"]["items"]["rows"]:
        row.append(5)
    with pytest.raises(backup.BackupError, match="priority.*newer Maestro"):
        backup.restore(store, _reload(document, tmp_path), now=NOW)


def test_a_backup_missing_a_required_column_is_refused(store, tmp_path):
    populate(store)
    document = write_and_load(store, tmp_path)
    older_backup(document, "items", "created_at")
    with pytest.raises(backup.BackupError, match="items is missing required columns .*created_at"):
        backup.restore(store, document, now=NOW)


@pytest.mark.parametrize("mangle, says", [
    (lambda p: p.write_text("not json"), "not valid JSON"),
    (lambda p: p.write_text("[]"), "no format number"),
    (lambda p: p.write_text('{"format": 1}'), "no tables"),
])
def test_a_file_that_is_not_a_backup_is_refused_with_a_sentence(tmp_path, mangle, says):
    path = tmp_path / "maestro.db"  # e.g. someone points restore at the database itself
    mangle(path)
    with pytest.raises(backup.BackupError, match=says):
        backup.load(path)


def test_a_mangled_row_or_time_is_refused_not_half_loaded(store, tmp_path):
    populate(store)
    document = write_and_load(store, tmp_path)
    document["tables"]["items"]["rows"][0] = ["too", "short"]
    with pytest.raises(backup.BackupError, match="items has a row"):
        backup.restore(store, document, now=NOW)

    document = write_and_load(store, tmp_path)
    document["tables"]["items"]["rows"][0][5] = "last tuesday-ish"
    with pytest.raises(backup.BackupError, match="unreadable created_at"):
        backup.restore(store, document, now=NOW)
    assert len(store.list(include_done=True)) == 3  # both refusals left the store alone


# --- keeping the spec honest against the real schema -------------------------------------------


def sqlite_schema(store):
    tables = {}
    for (name,) in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT IN ('sqlite_sequence', 'scheduler_lease')"
    ).fetchall():
        tables[name] = store.conn.execute(f"PRAGMA table_info({name})").fetchall()
    return tables


def test_the_spec_covers_every_table_and_column_of_the_real_schema(store):
    """A migration that adds a table or column must extend BACKUP_TABLES, or backups would
    silently lose it. (The PostgreSQL schema is held equal to SQLite's in test_storage_postgres.)"""
    schema = sqlite_schema(store)
    assert {t.name for t in BACKUP_TABLES} == set(schema)
    for table in BACKUP_TABLES:
        assert set(table.columns) == {c["name"] for c in schema[table.name]}, table.name


def test_the_restore_defaults_are_exactly_the_schema_defaults(store):
    """What restore fills in for an older backup must match what the migrations declare, column
    by column: NULL for a nullable column without a default, the declared default otherwise."""
    schema = sqlite_schema(store)
    for table in BACKUP_TABLES:
        declared = {c["name"]: c for c in schema[table.name]}
        for column, filled in COLUMN_DEFAULTS.get(table.name, {}).items():
            says = declared[column]["dflt_value"]
            expected = None if says is None else json.loads(says.replace("'", '"'))
            assert filled == expected, f"{table.name}.{column}"
            if filled is None:
                assert not declared[column]["notnull"], f"{table.name}.{column} can't default to NULL"


def test_every_default_free_required_column_really_is_required(store):
    """If a required column gained a schema default, COLUMN_DEFAULTS should say so too."""
    schema = sqlite_schema(store)
    for table in BACKUP_TABLES:
        defaults = COLUMN_DEFAULTS.get(table.name, {})
        for column in schema[table.name]:
            if column["name"] in defaults or column["pk"]:
                continue
            assert column["dflt_value"] is None, (
                f"{table.name}.{column['name']} has a schema default; add it to COLUMN_DEFAULTS"
            )


def _reload(document, tmp_path):
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(document))
    return backup.load(path)


def test_sqlite_cannot_be_opened_as_a_backup(tmp_path):
    path = tmp_path / "real.db"
    with SQLiteStore(path, tz=TZ):
        pass
    with pytest.raises(backup.BackupError, match="not a Maestro backup"):
        backup.load(path)


def test_statuses_survive(store, tmp_path):
    pills = populate(store)
    done = store.add(Item(text="One-off, handled", kind=Kind.REMINDER, created_at=NOW, due_at=NOW))
    store.complete(done.id, via="cli", now=NOW)
    # Completing recurring pills skipped it to the next occurrence and left it pending (ADR 0009).
    assert store.get(pills.id).status == Status.PENDING and store.get(done.id).status == Status.DONE
    document = write_and_load(store, tmp_path)
    with SQLiteStore(":memory:", tz=TZ) as other:
        backup.restore(other, document, now=NOW)
        assert other.get(pills.id) == store.get(pills.id)
        assert other.get(done.id).status == Status.DONE


# --- the CLI -------------------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)  # keep load_dotenv away from any real .env
    return str(tmp_path / "m.db"), tmp_path / "backup.json"


def cli(db, *args):
    from maestro.cli import main

    return main(["--db", db, *args])


def test_cli_backup_and_restore_round_trip(paths, capsys):
    db, file = paths
    assert cli(db, "add", "call", "mom", "tomorrow", "at", "5pm") == 0
    assert cli(db, "backup", str(file)) == 0
    assert f"Backed up 1 item to {file}." in capsys.readouterr().out

    assert cli(db, "add", "accidental", "extra", "note") == 0
    assert cli(db, "restore", str(file)) == 0
    out = capsys.readouterr().out
    assert f"Restored 1 item from {file}." in out
    assert cli(db, "list") == 0
    assert "Accidental" not in capsys.readouterr().out


def test_cli_restore_saves_what_it_replaces_and_that_file_restores_too(paths, capsys):
    db, file = paths
    assert cli(db, "add", "the", "original", "reminder", "tomorrow", "at", "9am") == 0
    assert cli(db, "backup", str(file)) == 0
    assert cli(db, "rm", "1") == 0
    assert cli(db, "add", "note", "to", "self:", "the", "wifi", "password", "is", "hunter2") == 0
    capsys.readouterr()

    assert cli(db, "restore", str(file)) == 0
    out = capsys.readouterr().out
    assert "Saved what the database held to" in out and "if this was a mistake" in out
    (kept,) = file.parent.glob("maestro-pre-restore-*.json")

    assert cli(db, "restore", str(kept)) == 0  # the undo really is a restore like any other
    assert cli(db, "search", "hunter2") == 0
    assert "hunter2" in capsys.readouterr().out


def test_cli_restore_into_an_empty_database_skips_the_safety_backup(paths, capsys):
    db, file = paths
    assert cli(db, "add", "water", "the", "plants", "every", "sunday") == 0
    assert cli(db, "backup", str(file)) == 0
    fresh = str(file.parent / "fresh.db")
    assert cli(fresh, "restore", str(file)) == 0
    out = capsys.readouterr().out
    assert "Saved what the database held" not in out
    assert list(file.parent.glob("maestro-pre-restore-*")) == []


def test_cli_restore_reports_reminders_that_came_due_in_the_gap(paths, capsys):
    db, file = paths
    with SQLiteStore(db, tz=TZ) as store:
        store.add(Item(text="Due long ago", kind=Kind.REMINDER, created_at=NOW, due_at=NOW))
        store.add(Item(text="Due long ago too", kind=Kind.REMINDER, created_at=NOW, due_at=LATER))
    assert cli(db, "backup", str(file)) == 0
    assert cli(db, "restore", str(file)) == 0
    assert "2 reminders came due while the backup sat on disk; the next scheduler tick will send" \
        in capsys.readouterr().out


def test_cli_restore_refuses_while_a_server_runs(paths, capsys):
    db, file = paths
    assert cli(db, "add", "irreplaceable", "note") == 0
    assert cli(db, "backup", str(file)) == 0
    from datetime import UTC as utc
    from datetime import datetime

    with SQLiteStore(db, tz=TZ) as store:
        assert store.acquire_lease("serve-1", datetime.now(utc),
                                   datetime.now(utc) + timedelta(seconds=90))[0]
    assert cli(db, "restore", str(file)) == 2
    err = capsys.readouterr().err
    assert "Restore refused: a scheduler is running (serve-1)" in err and "stop it first" in err
    assert cli(db, "search", "irreplaceable") == 0  # nothing was touched
    assert list(file.parent.glob("maestro-pre-restore-*")) == []  # not even a stray safety file


def test_cli_restore_of_a_file_that_is_not_a_backup_is_a_sentence(paths, capsys):
    db, file = paths
    file.write_text("definitely not json")
    assert cli(db, "restore", str(file)) == 2
    assert "Restore error:" in capsys.readouterr().err


def test_cli_backup_into_an_unwritable_place_is_a_sentence(paths, capsys):
    db, _ = paths
    assert cli(db, "backup", "/dev/null/nope.json") == 2
    assert "Backup error:" in capsys.readouterr().err


# --- scheduled backups (MAESTRO_BACKUP_DIR) --------------------------------------------------------


from maestro.backup import ScheduledBackups, scheduled_backups  # noqa: E402
from maestro.config import Config  # noqa: E402
from maestro.observability import METRICS  # noqa: E402


def utc(*args):
    from datetime import datetime

    from maestro.tz import UTC as zone

    return datetime(*args, tzinfo=zone)


T0 = utc(2026, 10, 26, 9, 0)


def test_scheduled_backups_are_off_until_a_directory_is_set(tmp_path):
    assert scheduled_backups(Config()) is None
    built = scheduled_backups(Config(backup_dir=str(tmp_path), backup_every="12h", backup_keep=3))
    assert built == ScheduledBackups(tmp_path, timedelta(hours=12), 3)
    assert built.describe() == f"every 12h into {tmp_path}, keeping 3"


@pytest.mark.parametrize("bad, says", [
    (Config(backup_dir="/b", backup_every="often"), "Can't read duration"),
    (Config(backup_dir="/b", backup_every="5m"), "at least 15m"),
    (Config(backup_dir="/b", backup_keep=0), "at least 1 backup"),
])
def test_half_configured_scheduled_backups_refuse_to_start(bad, says):
    with pytest.raises(ValueError, match=says):
        scheduled_backups(bad)


@pytest.fixture
def schedule(tmp_path):
    return ScheduledBackups(tmp_path / "backups", timedelta(hours=24), keep=2)


def test_the_first_tick_writes_a_backup_and_the_next_ones_wait(store, schedule):
    populate(store)
    path = schedule.maybe_run(store, now=T0)
    assert path == schedule.directory / "maestro-backup-20261026T090000Z.json"  # T0, said in UTC
    assert backup.load(path)["format"] == backup.FORMAT  # restorable like any other backup
    assert METRICS.get("maestro_backup_last_success_timestamp_seconds") == T0.timestamp()

    assert schedule.maybe_run(store, now=T0 + timedelta(hours=23)) is None
    assert schedule.maybe_run(store, now=T0 + timedelta(hours=24)) is not None


def test_the_clock_is_the_newest_file_so_a_restart_does_not_reset_it(store, schedule):
    schedule.maybe_run(store, now=T0)
    reborn = ScheduledBackups(schedule.directory, schedule.every, schedule.keep)  # a new process
    assert reborn.maybe_run(store, now=T0 + timedelta(hours=1)) is None
    assert reborn.maybe_run(store, now=T0 + timedelta(hours=25)) is not None


def test_only_the_newest_keep_files_survive_and_only_maestro_named_ones_are_touched(store, schedule):
    schedule.directory.mkdir(parents=True)
    stranger = schedule.directory / "maestro-backup-notes.json"  # doesn't match the timestamp name
    stranger.write_text("mine")
    manual = schedule.directory / "before-the-upgrade.json"
    backup.write(store, manual)

    for day in range(3):
        assert schedule.maybe_run(store, now=T0 + timedelta(days=day)) is not None
    names = sorted(p.name for p in schedule.directory.iterdir())
    assert names == ["before-the-upgrade.json", "maestro-backup-20261027T090000Z.json",
                     "maestro-backup-20261028T090000Z.json", "maestro-backup-notes.json"]


def test_a_failing_backup_is_counted_and_not_retried_every_tick(store, tmp_path, caplog):
    file_in_the_way = tmp_path / "not-a-directory"
    file_in_the_way.write_text("")
    schedule = ScheduledBackups(file_in_the_way, timedelta(hours=24), keep=2)
    import logging

    with caplog.at_level(logging.ERROR):
        assert schedule.maybe_run(store, now=T0) is None  # logged, counted, never raised
    assert METRICS.get("maestro_backup_errors_total") == 1
    assert "Scheduled backup" in caplog.text

    assert schedule.maybe_run(store, now=T0 + timedelta(minutes=5)) is None  # within the retry hold-off
    assert METRICS.get("maestro_backup_errors_total") == 1
    assert schedule.maybe_run(store, now=T0 + timedelta(minutes=16)) is None  # retried, fails again
    assert METRICS.get("maestro_backup_errors_total") == 2


def test_only_the_leader_writes_backups(schedule):
    from maestro.scheduler import Lease, run_forever

    with SQLiteStore(":memory:", tz=TZ) as store:
        assert store.acquire_lease("the-real-leader", T0, T0 + timedelta(days=365))[0]

        def one_tick(seconds):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_forever(store, _no_channels(), interval=30, wait=one_tick,
                        lease=Lease(owner="standby"), backups=schedule)
        assert not schedule.directory.exists()  # a standby never writes

    with SQLiteStore(":memory:", tz=TZ) as store:
        with pytest.raises(KeyboardInterrupt):
            run_forever(store, _no_channels(), interval=30, wait=one_tick, backups=schedule)
        assert len(schedule.written()) == 1


def _no_channels():
    from maestro.notifier import MultiNotifier

    return MultiNotifier({})


def test_doctor_reports_the_backup_schedule(tmp_path, monkeypatch, capsys):
    from maestro import doctor

    monkeypatch.chdir(tmp_path)
    config = Config(db_path=str(tmp_path / "m.db"), backup_dir=str(tmp_path / "backups"),
                    timezone=TZ.key)
    assert doctor.run(config)
    out = capsys.readouterr().out
    assert f"backups: every 24h into {tmp_path / 'backups'}, keeping 7; none written yet" in out

    ScheduledBackups(tmp_path / "backups", timedelta(hours=24), 7).maybe_run(
        SQLiteStore(":memory:", tz=TZ))
    assert doctor.run(config)
    assert "keeping 7; newest is 0m old" in capsys.readouterr().out


def test_doctor_fails_on_a_bad_backup_setting(tmp_path, capsys):
    from maestro import doctor

    config = Config(db_path=str(tmp_path / "m.db"), backup_dir="/b", backup_every="5m",
                    timezone=TZ.key)
    assert not doctor.run(config)
    assert "backups: MAESTRO_BACKUP_EVERY must be at least 15m" in capsys.readouterr().out
