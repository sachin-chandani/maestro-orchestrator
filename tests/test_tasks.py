"""#114: tasks, the third kind of item between reminders and notes.

A task is something to do with no time: it is never delivered, done works on it, and giving it a
time turns it into a reminder. The rules that decide task from note are the corpus's job
(`tests/corpus/phrases.txt`); what is here is the behaviour around them.
"""

from datetime import datetime

import pytest
from conftest import TZ, dt

from maestro import actions, commands
from maestro.actions import CreateNote, CreateReminder, CreateTask, apply
from maestro.interpret import Context, interpret
from maestro.models import Item, Kind, Status
from maestro.store import MIGRATIONS, Store

NOW = dt(2026, 10, 27, 8, 0)


@pytest.fixture
def store():
    with Store(":memory:", tz=TZ) as store:
        yield store


def kind_of(text: str) -> Kind:
    from maestro.parser import parse

    return parse(text, now=NOW, tz=TZ).kind


# --- the rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "todo: sort out the garage",
    "to do: ask jo about the invoice",
    "TODO: renew the car tax",
    "remind me to renew the passport",
    "remember to ask jo about the invoice",
    "don't forget to book the mot",
    "renew the passport",
    "buy milk",
    "call the plumber about the leak",
    "pick up the dry cleaning",
    "pay the gas bill",
    "check that the oven is off",
])
def test_an_untimed_instruction_is_a_task(text):
    assert kind_of(text) is Kind.TASK


@pytest.mark.parametrize("text", [
    "call centre number is 0800 123 456",
    "order number is 12345",
    "check engine light means low oil",
    "book club meets at the library",
    "book recommendation: project hail mary",
    "text from mom: the spare charger is in my bag",
    "pay scale for band 6 is on the intranet",
    "email signature template is in drafts",
    "fix for the printer jam is to open tray 2",
    "car park barrier code is 7788",
    "the gate code is 4321",
    "remember that my passport is in the blue drawer",
    "don't forget that sam is allergic to peanuts",
    "the best time to call dad is after dinner",
    "idea: build a birdhouse",
])
def test_a_statement_stays_a_note(text):
    assert kind_of(text) is Kind.NOTE


@pytest.mark.parametrize("text", [
    "buy milk at 6am",
    "call mom tomorrow at 5pm",
    "remind me to renew the passport on friday",
    "pay rent on the 1st of every month",
    "todo: call the plumber at 3pm",
])
def test_a_time_still_wins_and_makes_a_reminder(text):
    assert kind_of(text) is Kind.REMINDER


def test_the_todo_marker_is_dropped_from_the_text_but_kept_in_the_source():
    from maestro.parser import parse

    item = parse("todo: sort out the garage", now=NOW, tz=TZ)
    assert item.text == "Sort out the garage"
    assert item.source_text == "todo: sort out the garage"


def test_a_task_can_be_urgent_and_carry_channels():
    from maestro.parser import parse

    item = parse("!pay the parking fine", now=NOW, tz=TZ)
    assert item.kind is Kind.TASK and item.urgent is True and item.due_at is None


# --- actions and apply -------------------------------------------------------------------------


def test_the_rules_interpreter_asks_for_a_task():
    (action,) = interpret("renew the passport", Context(tz=TZ, now=NOW))
    assert isinstance(action, CreateTask)
    assert action.text == "Renew the passport"


def test_create_task_round_trips_through_its_json_form():
    action = CreateTask(text="Renew the passport", source_text="renew the passport", urgent=True,
                        created_at=NOW)
    data = action.to_json()
    assert data["type"] == "create_task"
    assert actions.from_json(data) == action


def test_create_task_has_no_time_fields():
    with pytest.raises(ValueError, match="no field due_at"):
        actions.from_json({"type": "create_task", "text": "Renew the passport",
                           "due_at": "2026-10-28T09:00:00-04:00"})


def test_from_item_maps_each_kind_to_its_action():
    common = dict(created_at=NOW, source_text="x")
    assert isinstance(actions.from_item(Item(text="A", kind=Kind.NOTE, **common)), CreateNote)
    assert isinstance(actions.from_item(Item(text="A", kind=Kind.TASK, **common)), CreateTask)
    assert isinstance(actions.from_item(
        Item(text="A", kind=Kind.REMINDER, due_at=NOW, **common)), CreateReminder)


def test_apply_saves_a_task_through_the_same_path_as_a_note(store):
    (result,) = apply(store, interpret("renew the passport", Context(tz=TZ, now=NOW)), source="cli")
    saved = result.saved
    assert saved.kind is Kind.TASK and saved.due_at is None and saved.id is not None
    assert store.get(saved.id).kind is Kind.TASK


def test_a_repeated_capture_of_a_task_is_stored_once(store):
    captured = interpret("renew the passport", Context(tz=TZ, now=NOW))
    first = apply(store, captured, source="twilio_sms", dedupe_key="SM1")[0]
    again = apply(store, captured, source="twilio_sms", dedupe_key="SM1")[0]
    assert again.created is False and again.saved.id == first.saved.id
    assert len(store.list()) == 1


# --- behaviour ----------------------------------------------------------------------------------


def test_a_task_never_becomes_due(store):
    store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW))
    assert store.due(datetime(2030, 1, 1, tzinfo=TZ)) == []


def test_done_works_on_a_task(store):
    task = store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW))
    assert store.complete(task.id, via="cli") is True
    assert store.get(task.id).status is Status.DONE
    assert store.list() == []  # done items are out of the default list


def test_giving_a_task_a_time_turns_it_into_a_reminder(store):
    task = store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW))
    when = dt(2026, 10, 28, 9, 0)
    assert store.snooze(task.id, when, via="cli") is True
    after = store.get(task.id)
    assert after.kind is Kind.REMINDER and after.due_at == when
    assert [i.id for i in store.due(when)] == [task.id]


def test_counts_report_tasks_separately(store):
    store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW))
    store.add(Item(text="The gate code is 4321", kind=Kind.NOTE, created_at=NOW))
    counts = store.counts()
    assert counts[(Kind.TASK, Status.PENDING)] == 1
    assert counts[(Kind.NOTE, Status.PENDING)] == 1


def test_a_task_is_listed_with_its_own_marker(store):
    task = store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW))
    assert commands.line(task) == f"☑️ #{task.id} Renew the passport (task)"
    assert commands.when(task) == "task"


# --- storage ------------------------------------------------------------------------------------


def test_tasks_survive_a_restart(tmp_path):
    path = tmp_path / "tasks.db"
    with Store(path, tz=TZ) as store:
        store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW,
                       source_text="renew the passport"))
    with Store(path, tz=TZ) as store:
        (item,) = store.list()
        assert item.kind is Kind.TASK


def test_migrating_a_database_from_before_tasks_keeps_every_note_a_note(tmp_path):
    """Schema 9 is 0.4: it has no idea tasks exist. Its notes are notes, not tasks, however much
    they read like something to do — nobody reclassifies data behind the user's back."""
    path = tmp_path / "v9.db"
    with Store(path, tz=TZ) as store:
        store.add(Item(text="Renew the passport", kind=Kind.NOTE, created_at=NOW,
                       source_text="renew the passport"))
        store.add(Item(text="The gate code is 4321", kind=Kind.NOTE, created_at=NOW,
                       source_text="the gate code is 4321"))
        store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=NOW,
                       due_at=dt(2026, 10, 28, 17, 0), source_text="call mom tomorrow at 5pm"))
        store.conn.execute("DROP INDEX IF EXISTS idx_items_kind")
        store.conn.execute("ALTER TABLE items DROP COLUMN interpreter")  # added later, by #116
        store.conn.execute("ALTER TABLE items DROP COLUMN capture_id")  # added later, by #117
        store.conn.execute("PRAGMA user_version = 9")  # as a 0.4 database
        store.conn.commit()

    with Store(path, tz=TZ) as store:
        assert store.schema_version == len(MIGRATIONS)
        assert [(i.text, i.kind) for i in store.list()] == [
            ("Call mom", Kind.REMINDER),
            ("Renew the passport", Kind.NOTE),
            ("The gate code is 4321", Kind.NOTE),
        ]
        # and the new kind works in the migrated database
        task = store.add(Item(text="Buy milk", kind=Kind.TASK, created_at=NOW))
        assert store.get(task.id).kind is Kind.TASK


def test_the_list_puts_tasks_after_reminders_and_before_notes(store):
    store.add(Item(text="The gate code is 4321", kind=Kind.NOTE, created_at=NOW))
    store.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW))
    store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=NOW, due_at=dt(2026, 10, 28, 17, 0)))
    assert [i.text for i in store.list()] == ["Call mom", "Renew the passport", "The gate code is 4321"]


# --- the CLI and the API -------------------------------------------------------------------------


def test_the_cli_lists_tasks_under_their_own_heading(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", "renew", "the", "passport"])
    main(["--db", db, "add", "remember", "the", "gate", "code", "is", "4321"])
    main(["--db", db, "add", "call", "mom", "tomorrow", "at", "5pm"])
    capsys.readouterr()
    assert main(["--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "Tasks" in out and "Notes" in out and "Reminders" in out
    assert out.index("Reminders") < out.index("Tasks") < out.index("Notes")
    assert "Renew the passport" in out.split("Tasks")[1].split("Notes")[0]


def test_the_api_can_ask_for_tasks_only():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with TestClient(create_app(Store(), api_token="t0ken")) as client:
        auth = {"Authorization": "Bearer t0ken"}
        client.post("/items", json={"text": "renew the passport"}, headers=auth)
        client.post("/items", json={"text": "remember the gate code is 4321"}, headers=auth)
        client.post("/items", json={"text": "call mom tomorrow at 5pm"}, headers=auth)

        tasks = client.get("/items?kind=task", headers=auth).json()
        assert [(i["text"], i["kind"], i["when"]) for i in tasks] == \
            [("Renew the passport", "task", "task")]
        assert [i["kind"] for i in client.get("/items?kind=note", headers=auth).json()] == ["note"]
        assert len(client.get("/items", headers=auth).json()) == 3
        assert client.get("/items?kind=nonsense", headers=auth).status_code == 422
