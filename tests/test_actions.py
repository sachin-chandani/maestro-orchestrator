"""#113 / ADR 0015: typed actions, the rules interpreter, and the one place actions are applied."""

from datetime import datetime

import pytest
from conftest import TZ, dt

from maestro import actions
from maestro.actions import (
    Applied,
    CreateNote,
    CreateReminder,
    Done,
    Snooze,
    Stop,
    apply,
    from_json,
)
from maestro.interpret import Context, Interpreter, RulesInterpreter, interpret
from maestro.models import Kind, Status
from maestro.parser import parse
from maestro.store import Store

NOW = dt(2026, 10, 23, 18, 0)  # a Friday evening


@pytest.fixture
def store():
    with Store(":memory:", tz=TZ) as s:
        yield s


# --- the JSON form -------------------------------------------------------------------------

ROUND_TRIP = [
    CreateReminder(text="Call mom", due_at=dt(2026, 10, 24, 17, 0)),
    CreateReminder(text="Pay rent", due_at=dt(2026, 11, 1, 9, 0), recurrence="monthly:1",
                   source_text="!pay rent on the 1st of every month, text me",
                   channels=["twilio_sms"], urgent=True, created_at=NOW),
    CreateNote(text="The wifi password is on the router", source_text="remember the wifi password"),
    CreateNote(text="Flat 4, blue door", channels=["email"], urgent=False, created_at=NOW),
    Done(),
    Done(item_id=3),
    Snooze(),
    Snooze(item_id=7, duration="2h"),
    Stop(item_id=12),
]


@pytest.mark.parametrize("action", ROUND_TRIP, ids=lambda a: str(a.to_json()))
def test_action_json_round_trip(action):
    data = action.to_json()
    assert data["type"] == action.TYPE
    assert from_json(data) == action
    # The JSON form is JSON: every value survives a real encode/decode unchanged.
    import json

    assert from_json(json.loads(json.dumps(data))) == action


def test_json_omits_what_was_never_decided():
    assert CreateNote(text="Blue drawer").to_json() == {"type": "create_note", "text": "Blue drawer"}
    assert Done().to_json() == {"type": "done"}


def test_actions_from_json_reads_a_list():
    data = [CreateNote(text="Blue drawer").to_json(), Done(item_id=2).to_json()]
    assert actions.actions_from_json(data) == [CreateNote(text="Blue drawer"), Done(item_id=2)]


@pytest.mark.parametrize("data, message", [
    ("done", "JSON object"),
    ({}, "Unknown action type"),
    ({"type": "explode"}, "Unknown action type"),
    ({"type": "create_note", "text": "x", "colour": "red"}, "no field colour"),
    ({"type": "create_note", "text": ""}, "non-empty string"),
    ({"type": "create_note", "text": 7}, "non-empty string"),
    ({"type": "create_reminder", "text": "x"}, "needs a due_at"),
    ({"type": "create_reminder", "text": "x", "due_at": "2026-10-24T17:00:00"}, "which timezone"),
    ({"type": "create_reminder", "text": "x", "due_at": "soon"}, "ISO 8601"),
    ({"type": "create_note", "text": "x", "source_text": 5}, "must be a string"),
    ({"type": "create_reminder", "text": "x", "due_at": 1761328800}, "ISO 8601"),
    ({"type": "create_note", "text": "x", "urgent": "yes"}, "true or false"),
    ({"type": "create_note", "text": "x", "channels": "email"}, "list of channel names"),
    ({"type": "create_note", "text": "x", "channels": [""]}, "list of channel names"),
    ({"type": "done", "item_id": 0}, "positive item number"),
    ({"type": "done", "item_id": "3"}, "positive item number"),
    ({"type": "done", "item_id": True}, "positive item number"),
    ({"type": "snooze", "duration": "soon"}, "Can't read duration"),
])
def test_invalid_json_is_rejected(data, message):
    with pytest.raises(ValueError, match=message):
        from_json(data)


def test_actions_from_json_needs_a_list():
    with pytest.raises(ValueError, match="JSON list"):
        actions.actions_from_json({"type": "done"})


def test_every_action_field_has_a_declared_json_type():
    from dataclasses import fields

    for action in actions.ACTIONS:
        for f in fields(action):
            assert f.name in actions.FIELD_TYPES, f"{action.__name__}.{f.name} has no JSON type"


# --- the rules interpreter -------------------------------------------------------------------


def test_rules_interpreter_is_the_default():
    assert isinstance(actions_for("call mom tomorrow at 5pm")[0], CreateReminder)
    assert interpret("remember the bins go out on sunday", Context(tz=TZ, now=NOW)) \
        == RulesInterpreter().interpret("remember the bins go out on sunday", Context(tz=TZ, now=NOW))


def actions_for(text):
    return interpret(text, Context(tz=TZ, now=NOW))


@pytest.mark.parametrize("text", [
    "remind me to call mom tomorrow at 5pm",
    "pay rent on the 1st of every month",
    "remember that my passport is in the blue drawer",
    "!take the medicine at 8am, text me and call me",
    "dentist in 2 hours",
])
def test_interpreting_keeps_exactly_what_the_parser_found(text):
    (action,) = actions_for(text)
    assert action.to_item() == parse(text, now=NOW, tz=TZ)


def test_the_whole_corpus_survives_the_round_trip():
    """Every corpus phrase becomes an action, through JSON and back, with the same item (#113)."""
    from test_corpus import NOW as CORPUS_NOW
    from test_corpus import TZ as CORPUS_TZ
    from test_corpus import load

    for case in load():
        (action,) = interpret(case.source, Context(tz=CORPUS_TZ, now=CORPUS_NOW))
        assert from_json(action.to_json()).to_item() == parse(case.source, now=CORPUS_NOW, tz=CORPUS_TZ), \
            f"line {case.line}: {case.source}"


def test_another_interpreter_can_be_used():
    class Fixed:
        def interpret(self, text: str, context: Context) -> list:
            return [CreateNote(text=text.upper())]

    fixed: Interpreter = Fixed()
    assert interpret("hello", Context(tz=TZ), interpreter=fixed) == [CreateNote(text="HELLO")]


# --- apply ------------------------------------------------------------------------------------


def test_apply_saves_a_reminder(store):
    (result,) = apply(store, actions_for("call mom tomorrow at 5pm"), source="cli")
    assert result == Applied(result.action, result.item, created=True, dropped=[], message="")
    assert result.item.id == 1
    assert store.get(1).due_at == dt(2026, 10, 24, 17, 0)
    assert store.get(1).kind == Kind.REMINDER


def test_apply_saves_a_note(store):
    (result,) = apply(store, actions_for("remember the wifi code is 4471"), source="api")
    assert store.get(result.item.id).kind == Kind.NOTE
    assert store.get(result.item.id).due_at is None


def test_apply_fills_in_when_the_capture_happened(store):
    """An action with no created_at (as a model would send) is stamped, not rejected."""
    before = datetime.now(TZ).replace(microsecond=0)
    (result,) = apply(store, [CreateNote(text="Blue drawer")], source="api")
    assert before <= store.get(result.item.id).created_at <= datetime.now(TZ)


def test_apply_keeps_only_configured_channels(store):
    (result,) = apply(store, actions_for("take the medicine at 8am, text me and call me"), source="cli",
                      requestable=frozenset({"twilio_sms"}))
    assert result.dropped == ["twilio_call"]
    assert store.get(result.item.id).channels == ["twilio_sms"]


def test_apply_skips_the_channel_check_when_the_deployment_has_not_said(store):
    (result,) = apply(store, actions_for("take the medicine at 8am, text me"), source="cli")
    assert result.dropped == []
    assert store.get(result.item.id).channels == ["twilio_sms"]


def test_apply_captures_an_inbound_message_once(store):
    captured = actions_for("dentist at 9am")
    first, = apply(store, captured, source="twilio_sms", dedupe_key="SM1")
    again, = apply(store, captured, source="twilio_sms", dedupe_key="SM1")
    assert (first.created, again.created) == (True, False)
    assert again.item.id == first.item.id
    assert len(store.list()) == 1


def test_apply_counts_the_same_id_from_another_source_separately(store):
    captured = actions_for("dentist at 9am")
    apply(store, captured, source="twilio_sms", dedupe_key="X1")
    other, = apply(store, captured, source="email", dedupe_key="X1")
    assert other.created
    assert len(store.list()) == 2


def test_apply_runs_every_action_in_order(store):
    results = apply(store, [CreateNote(text="One"), CreateNote(text="Two")], source="api")
    assert [r.item.text for r in results] == ["One", "Two"]


def test_apply_marks_done(store):
    (saved,) = apply(store, actions_for("call mom tomorrow at 5pm"), source="cli")
    (result,) = apply(store, [Done(item_id=saved.item.id)], source="cli")
    assert result.message == "✅ Done: Call mom"
    assert store.get(saved.item.id).status == Status.DONE


def test_apply_snoozes_and_stops(store):
    (saved,) = apply(store, actions_for("pay rent on the 1st of every month"), source="cli")
    (snoozed,) = apply(store, [Snooze(item_id=saved.item.id, duration="2h")], source="cli")
    assert snoozed.message.startswith("😴 Snoozed:")
    assert store.get(saved.item.id).snoozed_until is not None
    (stopped,) = apply(store, [Stop(item_id=saved.item.id)], source="cli")
    assert stopped.message == "⏹ Stopped: Pay rent"


def test_an_action_that_saves_nothing_has_no_item(store):
    (saved,) = apply(store, actions_for("call mom tomorrow at 5pm"), source="cli")
    (result,) = apply(store, [Done(item_id=saved.item.id)], source="cli")
    assert result.item is None
    with pytest.raises(ValueError, match="done saved no item"):
        assert result.saved


def test_apply_without_an_item_number_asks_which_one(store):
    (result,) = apply(store, [Done()], source="telegram", prefix="/")
    assert result.message == "Which one? Add its number, e.g. /done 3 (see /list)."


def test_apply_rejects_an_action_it_cannot_carry_out(store):
    class Unknown(actions.Action):
        TYPE = "unknown"

    with pytest.raises(ValueError, match="No way to apply"):
        apply(store, [Unknown()], source="cli")


def test_an_iso_string_is_read_into_a_datetime_whoever_passes_it():
    """One gate: a JSON-shaped value handed straight to the dataclass is read, not stored as text."""
    action = CreateReminder(text="Call mom", due_at="2026-10-24T17:00:00-04:00")  # type: ignore[arg-type]
    assert action.due_at == dt(2026, 10, 24, 17, 0)
    assert action == from_json({"type": "create_reminder", "text": "Call mom",
                                "due_at": "2026-10-24T17:00:00-04:00"})
    with pytest.raises(ValueError, match="which timezone"):
        CreateNote(text="x", created_at="2026-10-24T17:00:00")  # type: ignore[arg-type]


# --- what the fields hold, whoever set them (#113 review) -------------------------------------


def test_what_a_validator_reads_is_what_the_action_keeps(store):
    """The validators normalise as well as check, so the value they return has to be the one kept."""
    action = CreateReminder(text="Call mom", due_at="2026-10-24T17:00:00-04:00",  # type: ignore[arg-type]
                            created_at="2026-10-23T18:00:00-04:00")  # type: ignore[arg-type]
    assert isinstance(action.due_at, datetime) and isinstance(action.created_at, datetime)
    # …all the way into the store, which would reject a string.
    (result,) = apply(store, [action], source="api")
    assert store.get(result.saved.id).due_at == dt(2026, 10, 24, 17, 0)
    assert store.get(result.saved.id).created_at == dt(2026, 10, 23, 18, 0)


def test_a_subclass_check_runs_after_the_fields_are_read():
    """CreateReminder's "needs a due_at" must judge the read value, not the raw one."""
    assert CreateReminder(text="x", due_at="2026-10-24T17:00:00-04:00").due_at  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="needs a due_at"):
        CreateReminder(text="x")
    with pytest.raises(ValueError, match="Can't read duration"):
        Snooze(item_id=1, duration="ages")


def test_an_action_does_not_share_a_list_with_its_caller_or_its_json():
    asked = ["twilio_sms", "email"]
    action = CreateNote(text="Blue drawer", channels=asked)
    asked.append("telegram")  # the caller reuses its list
    assert action.channels == ["twilio_sms", "email"]
    data = action.to_json()
    data["channels"].append("telegram")  # a caller edits the JSON before sending it
    assert action.channels == ["twilio_sms", "email"]


def test_no_channel_choice_has_one_spelling():
    """[] and None both mean "wherever it normally goes", so an action only ever holds None."""
    assert CreateNote(text="x", channels=[]).channels is None
    assert from_json({"type": "create_note", "text": "x", "channels": []}) == CreateNote(text="x")
    assert "channels" not in CreateNote(text="x", channels=[]).to_json()


# --- dedupe keys (#113 review) ----------------------------------------------------------------


def test_a_capture_with_no_dedupe_key_is_saved_every_time(store):
    """No key means no claim to dedupe on: each arrival is a separate capture, as before ADR 0015."""
    for _ in range(2):
        apply(store, actions_for("dentist at 9am"), source="twilio_sms", dedupe_key=None)
    assert len(store.list()) == 2
    assert store.is_claimed("twilio_sms", "") is False  # nothing was claimed under an empty key


def test_the_dedupe_namespace_is_the_source_it_was_given(store):
    """The namespace is part of the stored claim, so it has to be exactly what the channel passes."""
    captured = actions_for("dentist at 9am")
    apply(store, captured, source="twilio_sms", dedupe_key="ID1")
    apply(store, captured, source="twilio_voice", dedupe_key="ID1")
    apply(store, captured, source="email", dedupe_key="ID1")
    assert len(store.list()) == 3  # the same id from three providers is three different messages
    assert all(store.is_claimed(s, "ID1") for s in ("twilio_sms", "twilio_voice", "email"))
    assert not store.is_claimed("telegram", "ID1")


# --- the schema (#115) -----------------------------------------------------------------------


def test_every_json_type_has_a_schema_and_every_schema_a_type():
    """The two tables describe the same fields: what a model is told and what is enforced."""
    assert set(actions.FIELD_SCHEMAS) == set(actions.FIELD_TYPES)


def test_the_schema_is_derived_from_the_actions():
    from dataclasses import fields

    schema = actions.json_schema()
    by_type = {entry["properties"]["type"]["const"]: entry for entry in schema["items"]["oneOf"]}
    assert set(by_type) == set(actions.BY_TYPE)
    for cls in actions.ACTIONS:
        entry = by_type[cls.TYPE]
        assert set(entry["properties"]) == {"type", *(f.name for f in fields(cls))}
        assert entry["additionalProperties"] is False
        assert entry["description"]  # the docstring's first line


def test_required_fields_are_what_the_gate_insists_on():
    required = {entry["properties"]["type"]["const"]: set(entry["required"])
                for entry in actions.json_schema()["items"]["oneOf"]}
    assert required["create_reminder"] == {"type", "text", "due_at"}  # due_at via REQUIRED
    assert required["create_task"] == {"type", "text"}
    assert required["create_note"] == {"type", "text"}
    assert required["done"] == {"type"}
    assert required["snooze"] == {"type"}


def test_the_schema_can_be_limited_to_the_actions_a_caller_accepts():
    schema = actions.json_schema(actions.CREATE_ACTIONS)
    kinds = [entry["properties"]["type"]["const"] for entry in schema["items"]["oneOf"]]
    assert kinds == ["create_reminder", "create_task", "create_note"]


@pytest.mark.parametrize("action", ROUND_TRIP, ids=lambda a: str(a.to_json()))
def test_every_json_form_fits_its_own_schema(action):
    """A structural check without a schema library: the keys the JSON form carries are properties,
    nothing required is missing, and the type tag is the constant."""
    entry = actions.action_schema(type(action))
    data = action.to_json()
    assert set(data) <= set(entry["properties"])
    assert set(entry["required"]) <= set(data)
    assert data["type"] == entry["properties"]["type"]["const"]


@pytest.mark.parametrize("rule", ["daily", "weekdays", "weekly:0", "weekly:6", "interval:120",
                                  "days:3", "weeks:2:4", "monthly:1", "monthly:31"])
def test_a_stored_recurrence_rule_is_accepted(rule):
    assert CreateReminder(text="x", due_at=NOW, recurrence=rule).recurrence == rule


@pytest.mark.parametrize("rule", ["every monday", "weekly:7", "weekly", "monthly:0", "monthly:32",
                                  "interval:0", "days:0", "weeks:2", "hourly", ""])
def test_a_recurrence_that_is_not_a_rule_is_rejected(rule):
    """A model that writes the phrase instead of the rule can't get it past the gate (#115)."""
    with pytest.raises(ValueError, match="recurrence rule|non-empty"):
        from_json({"type": "create_reminder", "text": "x", "due_at": NOW.isoformat(), "recurrence": rule})


def test_the_recurrence_pattern_in_the_schema_agrees_with_the_validator():
    import re

    from maestro import recurrence

    pattern = re.compile(actions.FIELD_SCHEMAS["recurrence"]["pattern"])
    for rule in ["daily", "weekly:3", "interval:90", "days:2", "weeks:3:0", "monthly:15"]:
        assert pattern.match(rule) and recurrence.validate(rule) == rule
    for bad in ["every monday", "weekly:7", "monthly:32"]:
        assert not pattern.match(bad)
        with pytest.raises(ValueError):
            recurrence.validate(bad)
