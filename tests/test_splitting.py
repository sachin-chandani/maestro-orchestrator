"""#130: one sentence, several requests — the splitter, and what a split capture saves.

The expected splits themselves live in tests/corpus/splits.txt (test_split_corpus.py); here are the
splitter's own edges and the end-to-end behaviour through `gaps.split_apply` and the API.
"""


import pytest
from conftest import TZ, dt

from maestro import gaps
from maestro.interpret import Context, RulesInterpreter, interpret_assessed
from maestro.parser import MAX_REQUESTS, parse, split_requests
from maestro.storage import SQLiteStore

NOW = dt(2026, 10, 27, 8, 0)  # a Tuesday morning
CONTEXT = Context(tz=TZ, now=NOW)

ADR_EXAMPLE = ("book flights, remind me friday at nine to check in, "
               "and call the plumber sometime next week")


# --- the splitter ------------------------------------------------------------------------------


@pytest.mark.parametrize("source,parts", [
    (ADR_EXAMPLE,
     ["book flights", "remind me friday at nine to check in",
      "call the plumber sometime next week"]),
    ("buy milk and then call the dentist at 3pm", ["buy milk", "call the dentist at 3pm"]),
    ("buy milk\ncall the dentist at 3pm", ["buy milk", "call the dentist at 3pm"]),
    ("pay rent tomorrow; also submit the expense report",
     ["pay rent tomorrow", "submit the expense report"]),
    # hedges between the join and the request are consumed with the join
    ("book the table and then maybe pick up some flowers",
     ["book the table", "pick up some flowers"]),
    # a grocery list splits only at the real second request
    ("buy bread, milk and eggs and call the dentist",
     ["buy bread, milk and eggs", "call the dentist"]),
])
def test_clear_joins_split(source, parts):
    assert split_requests(source) == parts


@pytest.mark.parametrize("source", [
    "buy bread and milk",                         # "milk" asks for nothing
    "call mom and dad tomorrow at 5pm",
    "clean and wash the car",                     # one bare verb is a description, not a request
    "work from 9am to 5pm",                       # no join at all
    "pay the bills friday and then call me",      # a channel choice (#20), not a second request
    "submit the report by eod and email me",
    "water the plants tomorrow, text me and email me",
    "Pay rent Friday. Text me.",                  # a channel choice after a sentence end too
    "the invoice is overdue and remind me to chase it",   # the first part isn't a request
    "milk and also remind me to call mom",
    "Dr. Smith appointment friday at 3pm",        # a title isn't a sentence end
    "take the pills at 9pm. i took them with food yesterday",  # the second part states a fact
    "bread, milk, eggs",                          # a list of things, not of requests
])
def test_unclear_joins_stay_whole(source):
    assert split_requests(source) == [source]


def test_more_parts_than_a_capture_may_ask_for_stay_whole():
    flood = " and ".join(f"buy item {n}" for n in range(MAX_REQUESTS + 1))
    assert split_requests(flood) == [flood]
    fits = " and ".join(f"buy item {n}" for n in range(MAX_REQUESTS))
    assert len(split_requests(fits)) == MAX_REQUESTS


def test_a_channel_tail_after_a_split_stays_with_its_own_request():
    read, _ = RulesInterpreter().read("buy milk and call the plumber, text me", CONTEXT)
    assert [(a.text, a.channels) for a in read] == [("Buy milk", None),
                                                    ("Call the plumber", ["twilio_sms"])]


def test_each_split_action_keeps_the_whole_message_as_its_source_text():
    read, _ = RulesInterpreter().read(ADR_EXAMPLE, CONTEXT)
    assert [a.source_text for a in read] == [ADR_EXAMPLE] * 3


def test_a_clean_split_leaves_no_several_requests_doubt_but_keeps_real_ones():
    _, clean = RulesInterpreter().read("buy milk and then call the dentist at 3pm", CONTEXT)
    assert clean.sure
    _, vague = RulesInterpreter().read(ADR_EXAMPLE, CONTEXT)
    assert "several_requests" not in vague.doubts
    assert "unread_time" in vague.doubts  # the plumber part still names a time the rules can't read


def test_an_urgent_mark_belongs_to_the_clause_that_carries_it():
    read, _ = RulesInterpreter().read("!take the insulin at 8am and buy milk", CONTEXT)
    assert [(a.text, a.urgent) for a in read] == [("Take the insulin", True), ("Buy milk", False)]


# --- vague spans (the `when` gap, ADR 0017) ----------------------------------------------------


@pytest.mark.parametrize("source,text", [
    ("call the plumber sometime next week", "Call the plumber sometime next week"),
    ("renew the passport soon", "Renew the passport soon"),
    ("book the mot at some point this month", "Book the mot at some point this month"),
])
def test_a_vague_span_is_kept_as_words_not_read_as_a_day(source, text):
    item = parse(source, now=NOW, tz=TZ)
    assert (item.text, item.due_at) == (text, None)


def test_a_bare_next_week_or_month_is_still_a_date():
    assert parse("plan the sprint next week", now=NOW, tz=TZ).due_at == dt(2026, 11, 3, 9, 0)
    assert parse("book a hotel next month", now=NOW, tz=TZ).due_at == dt(2026, 11, 27, 9, 0)


# --- end to end --------------------------------------------------------------------------------


def split(store, text, now=NOW):
    read, confidence = interpret_assessed(text, Context(tz=TZ, now=now))
    return gaps.split_apply(store, read, confidence, "api", "", text, now=now)


def test_the_adr_example_saves_two_and_holds_one(store=None):
    with SQLiteStore(":memory:", tz=TZ) as store:
        result = split(store, ADR_EXAMPLE)
        assert [i.text for i in result.added] == ["Book flights", "Check in"]
        assert [(i.text, i.gap) for i in result.needs_look] == [
            ("Call the plumber sometime next week", "when")]
        assert result.added[1].due_at == dt(2026, 10, 30, 9, 0)
        # one undo token takes back all three (ADR 0017)
        assert result.undo_id is not None
        assert {i.capture_id for i in result.saved} == {result.undo_id}
        assert [i.text for i in store.needs_look()] == ["Call the plumber sometime next week"]


def test_the_api_splits_a_capture_the_same_way():
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from maestro.api import create_app
    from maestro.captures import Confirmation
    from maestro.storage import SQLiteStore as Store

    with TestClient(create_app(Store(tz=TZ), api_token="t0ken", confirm=Confirmation())) as client:
        auth = {"Authorization": "Bearer t0ken"}
        # The API reads at the real now, so join two requests whose times survive any clock.
        body = client.post("/capture", headers=auth, json={
            "text": "buy milk and remind me to stretch in 2 hours and call the plumber sometime "
                    "next week"}).json()
        assert [i["text"] for i in body["added"]] == ["Buy milk", "Stretch"]
        assert [(i["text"], i["gap"]) for i in body["needs_look"]] == [
            ("Call the plumber sometime next week", "when")]
        assert body["undo"] is not None
        # undo removes everything the one capture added
        undone = client.post(f"/captures/{body['undo']['id']}/undo", headers=auth)
        assert undone.status_code == 200
        assert client.get("/items", headers=auth).json() == []
        assert client.get("/needs-look", headers=auth).json() == []


def test_the_cli_reports_each_saved_part(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "m.db")
    assert main(["--db", db, "add", "buy", "milk", "and", "then", "call", "the",
                 "dentist", "at", "3pm"]) == 0
    out = capsys.readouterr().out
    assert "Buy milk" in out and "Call the dentist" in out


# --- word hours ("friday at nine", from the ADR example) ---------------------------------------


@pytest.mark.parametrize("source,text,due", [
    ("remind me friday at nine to check in", "Check in", dt(2026, 10, 30, 9, 0)),
    ("dinner at seven tonight", "Dinner", dt(2026, 10, 27, 19, 0)),
    ("standup at nine o'clock", "Standup", dt(2026, 10, 27, 9, 0)),
])
def test_an_hour_said_as_a_word_is_read(source, text, due):
    item = parse(source, now=NOW, tz=TZ)
    assert (item.text, item.due_at) == (text, due)


@pytest.mark.parametrize("source", [
    "buy one loaf of bread",       # no "at": a count, not a time
    "the cat has nine lives",
    "choose between the two options",
])
def test_a_number_word_without_its_lead_stays_text(source):
    assert parse(source, now=NOW, tz=TZ).due_at is None
