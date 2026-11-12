"""#116: ask the model only when the rules are unsure, and record who read each capture.

Every test uses a fake provider. Nothing reaches a network or a real key.
"""

import logging
from datetime import datetime, timedelta

import pytest
from conftest import TZ, dt
from test_corpus import KNOWN_GAPS, check, load
from test_corpus import NOW as CORPUS_NOW
from test_corpus import TZ as CORPUS_TZ
from test_model_interpreter import FakeProvider, reminder

from maestro import actions, doctor
from maestro.actions import CreateNote, CreateReminder, apply, from_json
from maestro.confidence import DOUBTS
from maestro.config import Config
from maestro.interpret import (
    AutoInterpreter,
    Context,
    ModelInterpreter,
    RulesInterpreter,
    build_interpreter,
)
from maestro.model import ProviderError
from maestro.models import Item, Kind
from maestro.observability import METRICS
from maestro.store import MIGRATIONS, Store

NOW = dt(2026, 10, 27, 8, 0)  # a Tuesday morning
CONTEXT = Context(tz=TZ, now=NOW)
DENTIST = [reminder()]  # what the fake model says, whatever it is asked


def auto(reply=DENTIST) -> tuple[AutoInterpreter, FakeProvider]:
    provider = FakeProvider(reply)
    return AutoInterpreter(ModelInterpreter(provider)), provider


def doubts(text: str) -> tuple[str, ...]:
    _, confidence = RulesInterpreter().read(text, CONTEXT)
    return confidence.doubts


@pytest.fixture
def store():
    with Store(":memory:", tz=TZ) as s:
        yield s


# --- how sure the rules are -------------------------------------------------------------------

# The ten phrases agreed on #116: five the rules read correctly, five they don't.
SURE = [
    "milk",
    "call mom tomorrow at 5pm",
    "pay rent on the 1st of every month",
    "remember that my passport is in the blue drawer",
    "dentist friday at 3pm",
    # Two clear requests now split and read in full (#130), so there is nothing left to doubt.
    "book flights and remind me to pack sunscreen on friday",
    "buy milk, call mom",
]
UNSURE = {
    "pick up the kids before 3pm": "dangling_time_word",
    "work from 9am to 5pm": "time_left",
    "call the plumber sometime next week": "unread_time",
    # A join the rules can't split (#130): the first part states a fact, not a request.
    "the invoice is overdue and remind me to chase it": "several_requests",
    "so um tomorrow i need to like call the bank about the card and then also maybe pick up the "
    "dry cleaning after work": "several_requests",
}


@pytest.mark.parametrize("text", SURE)
def test_the_rules_are_sure_of_what_they_read_cleanly(text):
    assert doubts(text) == ()


@pytest.mark.parametrize("text,doubt", UNSURE.items())
def test_the_rules_say_why_they_are_unsure(text, doubt):
    assert doubt in doubts(text)


@pytest.mark.parametrize("text,doubt", [
    ("Fwd: your booking is confirmed", "forwarded"),
    ("check in at 15:00\non 14 Nov", "several_sentences"),
    ("Dentist friday at 3pm. Bring the forms.", "several_sentences"),
    ("um call mom tomorrow at 5pm", "filler"),
    ("call mom morning", "unread_time"),
    ("milk and also remind me to call mom", "several_requests"),  # "milk" alone can't split off
    # a dangling time word mid-text: "before 10am" lost its 10am and the grammar shows it
    ("meet john before 10am in the lobby", "dangling_time_word"),
    # "next weekend" is read as this weekend, leaving "next" behind
    ("dentist next weekend", "dangling_time_word"),
    # texting shorthand the rules don't read, left in the saved text
    ("renew passport b4 friday", "unread_time"),
    ("dr appointment 3pm tmrw", "unread_time"),
    ("team sync tues 10am", "unread_time"),
])
def test_other_reasons_to_doubt(text, doubt):
    assert doubt in doubts(text)


@pytest.mark.parametrize("text", [
    "salt and pepper",                       # "and" without a second request
    "call mom and dad tomorrow at 5pm",
    "Dr. Smith appointment friday at 3pm",   # a title isn't a sentence end
    "renew the passport",                    # a task with no time: nothing for a model to add
    "!take the medicine at 8am, text me and call me",
    "movie night saturday",                  # a part of the day that names something, with a time
    "take the pills after 9pm with food",    # "after 9pm" read; "after" is gone from the text
    "pick up grandma from the airport at 6pm",
    "look after the cat",                    # "after" as part of a verb, not a lost time
    "ask about the invoice",
    "the cat sat on the mat",                # "sat" and "sun" are words, not weekday shorthand
    "remember the sun lotion",
    "wedding on saturday at 2pm",
    "friday drinks at 6pm",
    "b4b meeting tomorrow at 3pm",           # shorthand only counts as a whole word
])
def test_ordinary_captures_are_not_doubted(text):
    assert doubts(text) == ()


def test_a_saved_text_that_trips_the_parser_is_a_doubt_not_a_crash():
    """The confidence check parses the saved text a second time. That parse must never cost a
    capture the first one saved: here it overflows, which only means the rules are unsure."""
    text = "check the oven in 2 hours and again in 99999999999 hours"
    read, confidence = RulesInterpreter().read(text, CONTEXT)
    assert not confidence.sure
    (action,) = read  # the capture itself is unharmed, in rules mode too
    assert isinstance(action, CreateReminder) and action.due_at == NOW.replace(hour=10)
    interpreter, _ = auto(ProviderError("timeout", "no answer"))  # and auto still never loses it
    assert interpreter.interpret(text, CONTEXT) == read


def test_confidence_means_correct_across_the_whole_corpus():
    """The point of #116: whenever the rules say they are sure, they are right. Every phrase in
    the corpus the rules misread — the known gaps — is one they are unsure of, so `auto` sends
    it to the model. This is measured on correctness, not on whether the words were used up."""
    misread_but_sure = []
    for case in load():
        _, confidence = RulesInterpreter().read(case.source, Context(tz=CORPUS_TZ, now=CORPUS_NOW))
        if confidence.sure and check(case) is not None:
            misread_but_sure.append(case.source)
    assert misread_but_sure == []
    for gap in KNOWN_GAPS:
        _, confidence = RulesInterpreter().read(gap, Context(tz=CORPUS_TZ, now=CORPUS_NOW))
        assert not confidence.sure, gap


def test_most_of_the_corpus_never_needs_a_model():
    """Not a threshold to tune — a guard that the doubts stay specific. If a change made the rules
    doubt ordinary captures, `auto` would quietly become `model` and send everything away."""
    cases = load()
    unsure = [c.source for c in cases
              if not RulesInterpreter().read(c.source, Context(tz=CORPUS_TZ, now=CORPUS_NOW))[1].sure]
    assert len(unsure) <= len(cases) // 20, unsure


def test_every_doubt_has_a_name_from_the_fixed_set():
    for text in [*UNSURE, "Fwd: x", "um milk", "a. b"]:
        assert set(doubts(text)) <= set(DOUBTS)


# --- auto: rules first, the model only when unsure ---------------------------------------------


def test_confident_captures_never_reach_the_provider():
    interpreter, provider = auto()
    for case in load():
        context = Context(tz=CORPUS_TZ, now=CORPUS_NOW)
        if RulesInterpreter().read(case.source, context)[1].sure:
            read = interpreter.interpret(case.source, context)
            assert read == RulesInterpreter().interpret(case.source, context)
    for text in SURE:
        interpreter.interpret(text, CONTEXT)
    assert provider.prompts == []
    assert METRICS.get("maestro_interpret_routed_total", route="model") == 0
    assert METRICS.get("maestro_interpret_routed_total", route="rules") > len(SURE)


@pytest.mark.parametrize("text", UNSURE)
def test_an_unsure_capture_is_read_by_the_model(text):
    interpreter, provider = auto()
    (action,) = interpreter.interpret(text, CONTEXT)
    assert len(provider.prompts) == 1 and provider.prompts[0][0].text == text
    assert isinstance(action, CreateReminder) and action.text == "Dentist"
    assert action.interpreter == "model"
    assert METRICS.get("maestro_interpret_routed_total", route="model") == 1


@pytest.mark.parametrize("failure,result", [
    (ProviderError("timeout", "the model provider didn't answer in 8s"), "fallback_timeout"),
    (ProviderError("http", "HTTP 503 from the model provider"), "fallback_http"),
    ([{"type": "done", "item_id": 3}], "fallback_invalid"),  # an action it wasn't offered
    ([], "fallback_invalid"),
    ([reminder(interpreter="rules")], "fallback_invalid"),  # can't claim to be the rules
    (RuntimeError("a bug in a provider"), "fallback_error"),
    (KeyError("content"), "fallback_error"),
], ids=["timeout", "http", "unoffered", "empty", "claims-rules", "runtime-error", "key-error"])
def test_when_the_model_fails_the_rules_answer_exactly_as_they_would_have(failure, result):
    text = "pick up the kids before 3pm"
    interpreter, provider = auto(failure)
    read = interpreter.interpret(text, CONTEXT)
    assert len(provider.prompts) == 1
    assert read == RulesInterpreter().interpret(text, CONTEXT)
    assert all(a.interpreter == "rules" for a in read)  # recorded as what actually read it
    assert METRICS.get("maestro_interpret_total", interpreter="model", result=result) == 1
    # The rules' answer was used, so the rules are counted — exactly as a fallback under
    # MAESTRO_INTERPRETER=model counts them (the RulesInterpreter() comparison above adds one).
    assert METRICS.get("maestro_interpret_total", interpreter="rules", result="ok") == 2


def test_an_unexpected_model_error_never_loses_a_capture_in_model_mode_either(caplog):
    with caplog.at_level(logging.DEBUG):
        read = ModelInterpreter(FakeProvider(RuntimeError("leaked: my bank pin is 1234"))) \
            .interpret("my bank pin reminder tomorrow at 9am", CONTEXT)
    assert read == RulesInterpreter().interpret("my bank pin reminder tomorrow at 9am", CONTEXT)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "unexpected RuntimeError" in logged
    assert "1234" not in logged and "bank pin" not in logged  # neither the error's text nor the capture


def test_routing_logs_why_and_never_what(caplog):
    interpreter, _ = auto()
    with caplog.at_level(logging.DEBUG):
        interpreter.interpret("call the plumber sometime next week", CONTEXT)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "unread_time" in logged and "plumber" not in logged
    assert METRICS.get("maestro_interpret_doubts_total", doubt="unread_time") == 1


# --- configuration ------------------------------------------------------------------------------

AUTO_ENV = {"MAESTRO_INTERPRETER": "auto", "MAESTRO_MODEL_PROVIDER": "openai",
            "MAESTRO_MODEL": "m", "MAESTRO_MODEL_API_KEY": "sk-fake-key-for-tests-only-0000"}


def test_auto_builds_rules_first_with_the_configured_model():
    interpreter = build_interpreter(Config.from_env(AUTO_ENV),
                                    transport=lambda *a: pytest.fail("no network in tests"))
    assert isinstance(interpreter, AutoInterpreter)
    assert interpreter.model.provider.name == "openai"


def test_model_and_rules_keep_meaning_what_they_did():
    assert isinstance(build_interpreter(Config.from_env({**AUTO_ENV, "MAESTRO_INTERPRETER": "model"})),
                      ModelInterpreter)
    assert isinstance(build_interpreter(Config.from_env({**AUTO_ENV, "MAESTRO_INTERPRETER": "rules"})),
                      RulesInterpreter)


def test_a_half_configured_auto_says_which_setting_is_missing():
    with pytest.raises(ValueError, match="MAESTRO_INTERPRETER=auto needs MAESTRO_MODEL_PROVIDER"):
        build_interpreter(Config.from_env({"MAESTRO_INTERPRETER": "auto"}))


def test_doctor_says_auto_only_sends_what_the_rules_are_unsure_of(tmp_path):
    config = Config.from_env({**AUTO_ENV, "MAESTRO_MODEL_PROVIDER": "anthropic"})
    config.db_path, config.channels, config.api_token = str(tmp_path / "m.db"), ["console"], "t"
    lines: list[str] = []
    doctor.run(config, out=lines.append)
    text = "\n".join(lines)
    assert "auto: the rules, and when they are unsure model m via anthropic" in text
    assert "the text of every capture the rules are unsure of is sent to anthropic" in text
    assert "sk-fake-key" not in text
    assert "--check-model" in lines[-1]


# --- recording who read it ----------------------------------------------------------------------


def test_the_interpreter_is_not_part_of_an_actions_json_form():
    """Only an interpreter can say who it is. A model reply or an API body naming one is refused,
    the schema a model is shown doesn't mention it, and the JSON form leaves it out."""
    with pytest.raises(ValueError, match="no field interpreter"):
        from_json({"type": "create_note", "text": "x", "interpreter": "rules"})
    assert "interpreter" not in str(actions.json_schema())
    assert "interpreter" not in CreateNote(text="x", interpreter="model").to_json()
    with pytest.raises(ValueError, match="interpreter must be one of rules, model"):
        CreateNote(text="x", interpreter="me")


def test_each_saved_item_records_who_read_it(store):
    interpreter, _ = auto()
    for text in ["call mom tomorrow at 5pm", "call the plumber sometime next week"]:
        apply(store, interpreter.interpret(text, CONTEXT), "cli")
    assert {(i.text, i.interpreter) for i in store.list()} == {("Call mom", "rules"), ("Dentist", "model")}


def test_the_cli_shows_who_read_each_item_when_asked(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", "call", "mom", "tomorrow", "at", "5pm"])
    with Store(db, tz=TZ) as s:  # an item saved directly, as if from before #116
        s.add(Item(text="Old note", kind=Kind.NOTE, created_at=NOW))
    capsys.readouterr()
    main(["--db", db, "list"])
    assert "read by" not in capsys.readouterr().out
    main(["--db", db, "list", "--verbose"])
    out = capsys.readouterr().out
    assert "read by rules" in out
    assert "read by unknown (saved before this was recorded)" in out


def test_the_api_reports_who_read_each_item():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    # The API reads captures at the real "now", so the fake model answers with a time after it.
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(microsecond=0).isoformat()
    interpreter, _ = auto([reminder(due=tomorrow)])
    with TestClient(create_app(Store(), api_token="t0ken", interpreter=interpreter)) as client:
        auth = {"Authorization": "Bearer t0ken"}
        client.post("/items", json={"text": "call mom tomorrow at 5pm"}, headers=auth)
        client.post("/items", json={"text": "call the plumber sometime next week"}, headers=auth)
        items = client.get("/items", headers=auth).json()
        assert {(i["text"], i["interpreter"]) for i in items} == {("Call mom", "rules"),
                                                                  ("Dentist", "model")}


def test_migrating_from_the_schema_before_it_was_recorded(tmp_path):
    """Schema 10 is the release before #116. Its rows come through untouched with no interpreter
    — not guessed as "rules" — and rows saved afterwards record theirs."""
    path = tmp_path / "v10.db"
    with Store(path, tz=TZ) as s:
        s.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=NOW, due_at=dt(2026, 10, 28, 17, 0),
                   source_text="call mom tomorrow at 5pm", channels=["twilio_sms"], urgent=True))
        s.add(Item(text="Renew the passport", kind=Kind.TASK, created_at=NOW,
                   source_text="renew the passport"))
        s.conn.execute("ALTER TABLE items DROP COLUMN interpreter")
        s.conn.execute("ALTER TABLE items DROP COLUMN capture_id")  # added later, by #117
        s.conn.execute("ALTER TABLE items DROP COLUMN gap")  # added later, by #126
        s.conn.execute("PRAGMA user_version = 10")
        s.conn.commit()

    with Store(path, tz=TZ) as s:
        assert s.schema_version == len(MIGRATIONS) == 14
        reminder_, task = s.list()
        assert (reminder_.text, reminder_.channels, reminder_.urgent, reminder_.interpreter) == \
            ("Call mom", ["twilio_sms"], True, None)
        assert (task.text, task.kind, task.interpreter) == ("Renew the passport", Kind.TASK, None)
        (applied,) = apply(s, RulesInterpreter().interpret("milk", CONTEXT), "cli")
        assert s.get(applied.saved.id).interpreter == "rules"
