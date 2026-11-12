"""#126: a capture splits per reading — sure ones save at once, unsure ones wait with a named gap.

Nothing here reaches a network. The model interpreter appears only as a fake, to produce the
several-actions-from-one-message case the rules can't.
"""

from datetime import datetime, timedelta

import pytest
from conftest import TZ, dt
from test_corpus import KNOWN_GAPS, check, load
from test_corpus import NOW as CORPUS_NOW
from test_corpus import TZ as CORPUS_TZ

from maestro import captures, gaps
from maestro.actions import CreateNote, CreateReminder, CreateTask
from maestro.captures import Confirmation, ConfirmMode
from maestro.confidence import GAPS, Confidence, gap
from maestro.config import Config, confirmation
from maestro.interpret import Context, RulesInterpreter, interpret_assessed, read_time_phrase
from maestro.models import CaptureStatus, Item, Kind, Status
from maestro.store import Store

NOW = dt(2026, 11, 11, 9, 0)  # a Wednesday morning
CONTEXT = Context(tz=TZ, now=NOW)
OWNER = ("telegram", "42")

# What the rules read with no time at all, in a message that clearly named one: "before 3pm" lost
# its 3pm, and with it the sense of the phrase.
UNSURE = "pick up the kids before"
OTHER_UNSURE = "call mom morning"
SURE = "call mom tomorrow at 5pm"


@pytest.fixture
def store():
    with Store(":memory:", tz=TZ) as s:
        yield s


def read(text: str):
    return interpret_assessed(text, CONTEXT)


def split(store, text: str, requestable=None, now=NOW, dedupe_key=None, owner=OWNER):
    actions, confidence = read(text)
    return gaps.split_apply(store, actions, confidence, owner[0], owner[1], text,
                            dedupe_key=dedupe_key, requestable=requestable, now=now)


# --- judging one reading at a time ---------------------------------------------------------


def judged(text: str, dropped=()) -> str | None:
    """The gap the rules would open for their reading of `text`."""
    actions, _ = read(text)
    return gap(actions[0].to_item(NOW), dropped)


@pytest.mark.parametrize("text", [
    "pick up the kids before",
    "call mom morning",
    "sort out the insurance after work",
    "water the plants before",
])
def test_a_reading_with_no_time_in_a_message_that_named_one_needs_a_time(text):
    assert judged(text) == "when"


@pytest.mark.parametrize("text", [
    "call the plumber",  # names no time at all: a task, as it has always been
    "milk",
    "renew the passport",
    "remember that my passport is in the blue drawer",
])
def test_a_reading_that_never_wanted_a_time_is_left_alone(text):
    assert judged(text) is None


@pytest.mark.parametrize("text", [
    "call mom tomorrow at 5pm",
    "work from 9am to 5pm",  # doubted (5pm was left) but it has a time, so it is not held back
    "pay rent on the 1st of every month",
    "remind me to check the oven before 3pm",
    "plan the sprint next week",  # a bare "next week" is read as one; only vague spans gap (#130)
])
def test_a_reading_that_already_has_a_time_never_needs_one(text):
    """The rule that keeps the new default safe: whatever the doubts, a reading with a usable time
    is saved and scheduled exactly as it was before ADR 0017."""
    actions, confidence = read(text)
    item = actions[0].to_item(NOW)
    assert item.due_at is not None
    assert gap(item) is None, f"{text!r} is doubted ({confidence.doubts}) but it has a time"


def test_a_reading_with_no_words_of_its_own_needs_a_description():
    assert gap(Item(text="um", kind=Kind.NOTE, created_at=NOW)) == "what"
    assert gap(Item(text="Uh... um", kind=Kind.NOTE, created_at=NOW)) == "what"


def test_an_unconfigured_channel_is_not_a_gap():
    """ADR 0011's behaviour is kept: the item is saved, goes out on the default channels and the
    person is told. Holding it back would stop a reminder with a perfectly good time (#126)."""
    actions, _ = read("pay rent friday, text me")
    item = actions[0].to_item(NOW)
    item.channels = None  # as apply_channel_choice leaves it when nothing survives
    assert gap(item, dropped=["twilio_sms"]) is None


def test_every_gap_has_a_name_from_the_fixed_set():
    assert set(gaps.WANTED) == set(GAPS)
    for case in load():
        assert gap(RulesInterpreter().read(case.source, CONTEXT)[0][0].to_item(NOW)) in (*GAPS, None)


def test_no_phrase_the_rules_read_correctly_is_ever_held_back():
    """The promise of the new default, measured over the whole corpus: a capture the rules get
    right is saved and scheduled, never turned into a question."""
    held_back = []
    for case in load():
        actions, _ = RulesInterpreter().read(case.source, Context(tz=CORPUS_TZ, now=CORPUS_NOW))
        if check(case) is None and gap(actions[0].to_item(CORPUS_NOW)) is not None:
            held_back.append(case.source)
    assert held_back == []


def test_a_timed_phrase_is_never_held_back_even_when_it_is_misread():
    """Stronger still: even among the phrases the rules get wrong, one that produced a time keeps
    firing on that time. A wrong time stays a wrong time — it does not become silence."""
    for source in KNOWN_GAPS:
        actions, _ = RulesInterpreter().read(source, Context(tz=CORPUS_TZ, now=CORPUS_NOW))
        item = actions[0].to_item(CORPUS_NOW)
        if item.due_at is not None:
            assert gap(item) is None, source


# --- splitting ------------------------------------------------------------------------------


def test_a_sure_capture_is_saved_and_scheduled_at_once(store):
    result = split(store, SURE)
    (item,) = result.added
    assert result.needs_look == [] and item.gap is None
    assert item.kind is Kind.REMINDER and [i.id for i in store.due(item.due_at)] == [item.id]


def test_an_unsure_capture_is_saved_visible_but_never_scheduled(store):
    result = split(store, UNSURE)
    (item,) = result.needs_look
    assert result.added == [] and item.gap == "when"
    assert [i.id for i in store.needs_look(*OWNER)] == [item.id]
    assert store.list() == [store.get(item.id)]  # visible in the ordinary list too
    assert store.due(NOW + timedelta(days=365)) == []  # and never due, however long nobody answers


def test_one_message_saves_what_is_clear_and_asks_only_about_the_rest(store):
    """The point of the issue: a second request that can't be read never holds back the first."""
    actions = [CreateReminder(text="Call mom", due_at=NOW + timedelta(hours=8), interpreter="model"),
               CreateTask(text="Pack before", interpreter="model"),
               CreateNote(text="The gate code is 4321", interpreter="model")]
    result = gaps.split_apply(store, actions, Confidence(("several_requests",)), *OWNER,
                              "call mom at 5pm and pack before and the gate code is 4321", now=NOW)
    assert [i.text for i in result.added] == ["Call mom", "The gate code is 4321"]
    assert [(i.text, i.gap) for i in result.needs_look] == [("Pack before", "when")]
    assert [i.text for i in result.saved] == ["Call mom", "Pack before", "The gate code is 4321"]


def test_every_split_capture_is_one_undo_token_covering_all_of_it(store):
    actions = [CreateReminder(text="Call mom", due_at=NOW + timedelta(hours=8)),
               CreateTask(text="Pack before")]
    result = gaps.split_apply(store, actions, Confidence(), *OWNER, "…", now=NOW)
    capture = store.get_capture(result.undo_id)
    assert capture.status == CaptureStatus.APPLIED and result.undo_until == NOW + captures.UNDO_WINDOW
    assert all(i.capture_id == capture.id for i in result.saved)

    outcome, removed = captures.undo(store, capture.id, via="telegram", now=NOW + timedelta(minutes=5))
    assert (outcome, removed) == (captures.Outcome.UNDONE, 2)
    assert store.list() == [] and store.needs_look() == []


def test_undo_past_its_window_changes_nothing(store):
    result = split(store, SURE)
    outcome, removed = captures.undo(store, result.undo_id, via="telegram",
                                     now=NOW + timedelta(minutes=11))
    assert (outcome, removed) == (captures.Outcome.TOO_LATE, 0)
    assert len(store.list()) == 1


def test_undo_also_takes_back_a_gap_that_was_filled_in_the_meantime(store):
    """"Exactly what that capture added" includes an answer given since: the item is the same one."""
    result = split(store, UNSURE)
    item = result.needs_look[0]
    assert gaps.fill(store, item, "tomorrow at 9am", now=NOW) is not None
    outcome, removed = captures.undo(store, result.undo_id, via="telegram", now=NOW)
    assert (outcome, removed) == (captures.Outcome.UNDONE, 1) and store.list() == []


def test_a_retried_provider_message_is_split_once(store):
    first = split(store, UNSURE, dedupe_key="SM-1")
    again = split(store, UNSURE, dedupe_key="SM-1")
    assert first.undo_id == again.undo_id and not again.created
    assert [i.id for i in again.saved] == [i.id for i in first.saved]
    assert len(store.list()) == 1 and len(store.captures()) == 1


def test_a_capture_with_no_provider_id_is_split_every_time(store):
    """Two deliberate captures of the same words are two things to remember, as they always were."""
    first, second = split(store, UNSURE), split(store, UNSURE)
    assert first.undo_id != second.undo_id and len(store.needs_look()) == 2


def test_an_unconfigured_channel_is_still_saved_and_still_reported(store):
    result = split(store, "pay rent friday, text me", requestable=frozenset())
    (item,) = result.added
    assert result.dropped == ["twilio_sms"] and item.channels is None
    assert "twilio_sms isn't configured" in gaps.worded(result)
    assert [i.id for i in store.due(item.due_at)] == [item.id]  # and it still goes out


# --- answering ---------------------------------------------------------------------------------


def test_a_bare_time_fills_the_one_recent_question(store):
    item = split(store, UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, "5pm", now=NOW)
    message, filled = gaps.answer(store, reply, now=NOW)
    assert filled.id == item.id and filled.gap is None
    assert filled.kind is Kind.REMINDER and filled.due_at == NOW.replace(hour=17)
    assert "Scheduled" in message and store.needs_look() == []
    assert [i.id for i in store.due(filled.due_at)] == [filled.id]


@pytest.mark.parametrize("phrase,hour", [("5pm", 17), ("at 5pm", 17), ("tonight", 20)])
def test_the_shapes_of_a_bare_time_that_count_as_an_answer(store, phrase, hour):
    split(store, UNSURE)
    reply = gaps.match_reply(store, *OWNER, phrase, now=NOW)
    assert reply is not None and gaps.answer(store, reply, now=NOW)[1].due_at.hour == hour


@pytest.mark.parametrize("text", [
    "call dad at 5pm",  # says what to do: a capture in its own right
    "buy milk",
    "the meeting is at 5pm",
])
def test_a_reply_that_says_something_of_its_own_is_captured_not_an_answer(store, text):
    item = split(store, UNSURE).needs_look[0]
    assert gaps.match_reply(store, *OWNER, text, now=NOW) is None
    assert store.get(item.id).gap == "when"  # the question is left exactly as it was


def test_a_bare_time_outside_the_window_is_captured_and_the_question_survives(store):
    item = split(store, UNSURE).needs_look[0]
    late = NOW + gaps.GAP_REPLY_WINDOW + timedelta(minutes=1)
    assert gaps.match_reply(store, *OWNER, "5pm", now=late) is None
    assert store.get(item.id).gap == "when"
    # Still reachable by name, however old: only the shortcut expires, never the item.
    named = gaps.match_reply(store, *OWNER, f"#{item.id} 5pm", now=late)
    assert named.item.id == item.id and gaps.answer(store, named, now=late)[1].gap is None


def test_two_open_questions_make_a_bare_time_ask_which_and_save_nothing(store):
    first = split(store, UNSURE).needs_look[0]
    second = split(store, OTHER_UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, "5pm", now=NOW)
    message, filled = gaps.answer(store, reply, now=NOW)
    assert filled is None and "Which one?" in message
    assert f"#{first.id}" in message and f"#{second.id}" in message
    assert {i.gap for i in store.needs_look()} == {"when"}  # neither was touched
    assert len(store.list()) == 2  # and nothing was captured either


def test_naming_one_of_several_answers_exactly_that_one(store):
    first = split(store, UNSURE).needs_look[0]
    second = split(store, OTHER_UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, f"#{second.id} 5pm", now=NOW)
    assert gaps.answer(store, reply, now=NOW)[1].id == second.id
    assert [i.id for i in store.needs_look()] == [first.id]


def test_a_bare_time_with_nothing_waiting_is_captured_as_usual(store):
    assert gaps.match_reply(store, *OWNER, "5pm", now=NOW) is None


def test_a_named_item_that_is_not_this_senders_question_is_captured(store):
    """A guessed number reaches nothing, and says nothing about whether it exists (ADR 0016)."""
    theirs = split(store, UNSURE, owner=("telegram", "7")).needs_look[0]
    assert gaps.match_reply(store, *OWNER, f"#{theirs.id} 5pm", now=NOW) is None
    assert gaps.match_reply(store, "twilio_sms", "42", f"#{theirs.id} 5pm", now=NOW) is None
    assert store.get(theirs.id).gap == "when"


def test_a_named_item_with_no_gap_is_captured(store):
    """The invariant: a reply can only ever complete a question, never move a real reminder."""
    reminder = split(store, SURE).added[0]
    assert gaps.match_reply(store, *OWNER, f"#{reminder.id} 9am", now=NOW) is None
    assert store.get(reminder.id).due_at == dt(2026, 11, 12, 17, 0)


def test_a_time_that_cannot_be_read_leaves_the_question_open(store):
    item = split(store, UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, f"#{item.id} whenever", now=NOW)
    message, filled = gaps.answer(store, reply, now=NOW)
    assert filled is None and "still needs a time" in message
    assert store.get(item.id).gap == "when"


def test_dismissing_removes_the_question_and_nothing_else(store):
    kept = split(store, SURE).added[0]
    item = split(store, UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, "dismiss", now=NOW)
    message, _ = gaps.answer(store, reply, now=NOW)
    assert "Dismissed" in message and store.get(item.id) is None
    assert store.get(kept.id) is not None


def test_dismissing_with_two_open_questions_asks_which(store):
    split(store, UNSURE)
    split(store, "pick up the kids before")
    reply = gaps.match_reply(store, *OWNER, "dismiss", now=NOW)
    assert "Which one?" in gaps.answer(store, reply, now=NOW)[0]
    assert len(store.needs_look()) == 2


def test_answering_a_question_someone_already_answered_says_so(store):
    item = split(store, UNSURE).needs_look[0]
    assert gaps.fill(store, item, "5pm", now=NOW) is not None
    assert gaps.fill(store, item, "6pm", now=NOW) is None  # the stale copy loses
    assert store.get(item.id).due_at == NOW.replace(hour=17)


def test_answering_a_question_that_was_undone_says_no(store):
    result = split(store, UNSURE)
    item = result.needs_look[0]
    captures.undo(store, result.undo_id, via="telegram", now=NOW)
    assert gaps.fill(store, item, "5pm", now=NOW) is None
    assert not gaps.dismiss(store, item)


def test_a_filled_time_in_the_past_is_moved_on_like_any_other_capture(store):
    """The rules put a time that has gone by today on tomorrow, so an answer is never overdue."""
    item = split(store, UNSURE, now=NOW.replace(hour=18)).needs_look[0]
    filled = gaps.fill(store, item, "5pm", now=NOW.replace(hour=18))
    assert filled.due_at == dt(2026, 11, 12, 17, 0)


def test_a_repeat_can_answer_a_question_too(store):
    item = split(store, UNSURE).needs_look[0]
    filled = gaps.fill(store, item, "every monday at 9am", now=NOW)
    assert filled.recurrence == "weekly:0" and filled.gap is None


def test_a_channel_question_is_answered_with_a_channel(store):
    """Nothing the rules open, but the vocabulary works end to end for an interpreter that does."""
    item = store.add(Item(text="Text me the code", kind=Kind.TASK, created_at=NOW, gap="channel"))
    assert gaps.fill(store, item, "sms", frozenset({"telegram"}), now=NOW) is None
    filled = gaps.fill(store, item, "telegram", frozenset({"telegram"}), now=NOW)
    assert filled.channels == ["telegram"] and filled.gap is None


def test_a_description_question_is_answered_with_words(store):
    """The answer is read as a capture in its own right, so a time in it becomes the item's time
    rather than being left sitting in the text of something that never fires."""
    item = store.add(Item(text="Um", kind=Kind.NOTE, created_at=NOW, gap="what"))
    filled = gaps.fill(store, item, "call the dentist at 5pm", now=NOW)
    assert (filled.text, filled.gap) == ("Call the dentist", None)
    assert filled.kind is Kind.REMINDER and filled.due_at == NOW.replace(hour=17)
    assert [i.id for i in store.due(filled.due_at)] == [filled.id]


def test_a_description_answered_without_a_time_becomes_a_task_not_a_note(store):
    item = store.add(Item(text="Um", kind=Kind.NOTE, created_at=NOW, gap="what"))
    filled = gaps.fill(store, item, "call the dentist", now=NOW)
    assert (filled.text, filled.kind, filled.due_at) == ("Call the dentist", Kind.TASK, None)


def test_dictation_filler_on_its_own_asks_what_was_meant(store):
    """The `what` gap really is reachable from the rules, so its answering path is not dead code."""
    result = split(store, "um uh")
    assert [(i.text, i.gap) for i in result.needs_look] == [("Um uh", "what")]
    assert result.added == []


# --- reading a bare time -------------------------------------------------------------------------


@pytest.mark.parametrize("text,is_time", [
    ("5pm", True), ("tomorrow 9am", True), ("at 5pm", True), ("tonight", True), ("noon", True),
    ("every monday", True),
    ("call dad at 5pm", False), ("milk", False), ("", False), ("whenever", False),
])
def test_what_counts_as_nothing_but_a_time(text, is_time):
    assert (read_time_phrase(text, CONTEXT) is not None) is is_time


def test_an_unreadable_reply_is_not_an_answer_rather_than_an_error():
    assert read_time_phrase("in 99999999999 hours", CONTEXT) is None


# --- the modes -----------------------------------------------------------------------------------


def test_split_is_the_default_and_never_still_turns_everything_off():
    assert confirmation(Config.from_env({})).mode is ConfirmMode.SPLIT
    assert confirmation(Config.from_env({"MAESTRO_CONFIRM": "split"})).splits
    assert not confirmation(Config.from_env({"MAESTRO_CONFIRM": "unsure"})).splits
    assert confirmation(Config.from_env({"MAESTRO_CONFIRM": "never"})) is None


def test_unsure_still_holds_the_whole_message(store):
    """#117 is untouched: in `unsure` a doubted capture reaches no item at all."""
    actions, confidence = read(UNSURE)
    assert captures.needs_confirmation(actions, confidence)
    proposal, _ = captures.propose(store, actions, confidence, *OWNER, UNSURE, now=NOW)
    assert proposal.status == CaptureStatus.PENDING and store.list() == []
    assert store.needs_look() == []


def test_never_saves_even_an_unsure_reading_with_no_gap(store, monkeypatch, capsys, tmp_path):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_CONFIRM", "never")
    db = str(tmp_path / "m.db")
    assert main(["--db", db, "add", *UNSURE.split()]) == 0
    with Store(db, tz=TZ) as s:
        (item,) = s.list()
        assert item.gap is None and s.needs_look() == []


# --- the channels ----------------------------------------------------------------------------------


def telegram_bot(store):
    from test_telegram import CHAT, FakeTransport

    from maestro.telegram import TelegramBot, TelegramClient

    transport = FakeTransport()
    return transport, TelegramBot(TelegramClient(transport), store, str(CHAT),
                                  confirm=Confirmation(mode=ConfirmMode.SPLIT))


def test_telegram_adds_what_is_clear_and_asks_about_the_rest(store):
    from test_telegram import CHAT, message

    transport, bot = telegram_bot(store)
    bot.handle(message(UNSURE))
    (sent,) = transport.sent()
    assert "needs a time: reply with one" in sent["text"]
    (item,) = store.needs_look("telegram", str(CHAT))

    bot.handle(message("5pm", update_id=2))
    assert "Scheduled" in transport.sent()[1]["text"]
    assert store.get(item.id).gap is None and store.needs_look() == []


def test_telegram_offers_undo_when_something_needed_a_look(store):
    from test_telegram import message

    transport, bot = telegram_bot(store)
    bot.handle(message(f"{SURE} and {UNSURE}"))  # one message, so the rules read one thing
    (sent,) = transport.sent()
    assert "needs a time" in sent["text"] or "Saved" in sent["text"]


def test_sms_adds_what_is_clear_and_a_bare_time_answers(store):
    from maestro.sms import SmsHandler

    handler = SmsHandler(store, "token", "+15550001111",
                         confirm=Confirmation(mode=ConfirmMode.SPLIT))
    reply = handler.handle({"From": "+15550001111", "Body": UNSURE, "MessageSid": "SM1"})
    assert "needs a time" in reply
    (item,) = store.needs_look("twilio_sms", "+15550001111")

    answered = handler.handle({"From": "+15550001111", "Body": "5pm", "MessageSid": "SM2"})
    assert "Scheduled" in answered and store.get(item.id).gap is None


def test_sms_undo_takes_back_a_split_capture(store):
    from maestro.sms import SmsHandler

    handler = SmsHandler(store, "token", "+15550001111",
                         confirm=Confirmation(mode=ConfirmMode.SPLIT))
    handler.handle({"From": "+15550001111", "Body": SURE, "MessageSid": "SM1"})
    assert len(store.list()) == 1
    undone = handler.handle({"From": "+15550001111", "Body": "UNDO", "MessageSid": "SM2"})
    assert "Undone" in undone and store.list() == []


def test_cli_saves_lists_fills_and_dismisses(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    db = str(tmp_path / "m.db")
    assert main(["--db", db, "add", *UNSURE.split()]) == 0
    assert "Needs a time" in capsys.readouterr().out

    assert main(["--db", db, "list", "--needs-look"]) == 0
    out = capsys.readouterr().out
    assert "needs a time" in out

    with Store(db, tz=TZ) as s:
        (item,) = s.needs_look()
    assert main(["--db", db, "fill", str(item.id), "tomorrow", "at", "9am"]) == 0
    assert "Scheduled" in capsys.readouterr().out
    with Store(db, tz=TZ) as s:
        assert s.get(item.id).gap is None and s.get(item.id).kind is Kind.REMINDER

    assert main(["--db", db, "fill", str(item.id), "9am"]) == 1  # no longer a question
    assert main(["--db", db, "add", *UNSURE.split()]) == 0
    capsys.readouterr()
    with Store(db, tz=TZ) as s:
        (second,) = s.needs_look()
    assert main(["--db", db, "dismiss", str(second.id)]) == 0
    with Store(db, tz=TZ) as s:
        assert s.needs_look() == []


def test_cli_list_keeps_questions_in_their_own_section(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", *SURE.split()])
    main(["--db", db, "add", *UNSURE.split()])
    capsys.readouterr()
    assert main(["--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "Reminders" in out and "Needs a look" in out
    assert out.index("Reminders") < out.index("Needs a look")


# --- the API ----------------------------------------------------------------------------------------


@pytest.fixture
def client(store):
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with TestClient(create_app(store, api_token="t0ken",
                               confirm=Confirmation(mode=ConfirmMode.SPLIT))) as c:
        c.headers["Authorization"] = "Bearer t0ken"
        yield c


def test_the_api_reports_what_was_added_what_waits_and_how_to_undo(client, store):
    body = client.post("/capture", json={"text": SURE}).json()
    assert len(body["added"]) == 1 and body["needs_look"] == []
    assert body["undo"]["id"] is not None and body["undo"]["expires_at"]

    waiting = client.post("/capture", json={"text": UNSURE}).json()
    assert waiting["added"] == [] and waiting["needs_look"][0]["gap"] == "when"
    assert waiting["undo"] is None  # nothing was added, so there is nothing to take back


def test_post_items_still_answers_with_one_item(client):
    """Existing clients keep working: the shape is unchanged, and `gap` says when it is waiting."""
    saved = client.post("/items", json={"text": SURE})
    assert saved.status_code == 201 and saved.json()["gap"] is None
    waiting = client.post("/items", json={"text": UNSURE})
    assert waiting.status_code == 201 and waiting.json()["gap"] == "when"
    assert waiting.json()["id"] is not None


def test_the_api_lists_fills_and_dismisses_what_needs_a_look(client, store):
    item_id = client.post("/capture", json={"text": UNSURE}).json()["needs_look"][0]["id"]
    assert [i["id"] for i in client.get("/needs-look").json()] == [item_id]

    refused = client.post(f"/needs-look/{item_id}", json={"phrase": "whenever"})
    assert refused.status_code == 422 and "still needs a time" in refused.json()["detail"]

    filled = client.post(f"/needs-look/{item_id}", json={"phrase": "tomorrow at 9am"}).json()
    assert filled["gap"] is None and filled["kind"] == "reminder"
    assert client.get("/needs-look").json() == []
    assert client.post(f"/needs-look/{item_id}", json={"phrase": "9am"}).status_code == 404

    second = client.post("/capture", json={"text": UNSURE}).json()["needs_look"][0]["id"]
    assert client.delete(f"/needs-look/{second}").status_code == 204
    assert store.get(second) is None
    assert client.delete(f"/needs-look/{second}").status_code == 404


def test_the_api_needs_a_token_for_the_new_routes(store):
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with TestClient(create_app(store, api_token="t0ken")) as c:
        assert c.get("/needs-look").status_code == 401
        assert c.post("/needs-look/1", json={"phrase": "5pm"}).status_code == 401
        assert c.delete("/needs-look/1").status_code == 401
        assert c.post("/capture", json={"text": SURE}).status_code == 401


# --- what the operator sees ---------------------------------------------------------------------------


def test_splitting_is_counted(store):
    before = METRICS_SNAPSHOT()
    split(store, UNSURE)
    split(store, SURE)
    after = METRICS_SNAPSHOT()
    assert after.get("needs_look", 0) - before.get("needs_look", 0) == 1
    assert after.get("saved", 0) - before.get("saved", 0) == 1


def METRICS_SNAPSHOT() -> dict[str, float]:
    from maestro.observability import METRICS

    rendered = METRICS.render()
    counts = {}
    for line in rendered.splitlines():
        if line.startswith("maestro_capture_split_total{"):
            result = line.split('result="')[1].split('"')[0]
            counts[result] = float(line.rsplit(" ", 1)[1])
    return counts


# --- upgrading ---------------------------------------------------------------------------------


def test_migrating_from_the_schema_before_readings_needed_a_look(tmp_path):
    """Schema 12 is the release before #126. Its items come through with no gap — an upgrade never
    invents a question and never silences a reminder — and the new column works afterwards."""
    from maestro.store import MIGRATIONS

    path = tmp_path / "v12.db"
    with Store(path, tz=TZ) as s:
        s.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=NOW,
                   due_at=NOW + timedelta(hours=8), source_text="call mom at 5pm",
                   interpreter="rules"))
        s.add(Item(text="Pick up the kids before", kind=Kind.TASK, created_at=NOW,
                   source_text=UNSURE, interpreter="rules"))
        s.conn.execute("ALTER TABLE items DROP COLUMN gap")
        s.conn.execute("PRAGMA user_version = 12")
        s.conn.commit()

    with Store(path, tz=TZ) as s:
        assert s.schema_version == len(MIGRATIONS) == 15
        reminder_row, task_row = s.list()
        assert (reminder_row.gap, task_row.gap) == (None, None)
        assert s.needs_look() == []
        assert [i.id for i in s.due(NOW + timedelta(hours=8))] == [reminder_row.id]

        waiting = s.add(Item(text="Pack before", kind=Kind.TASK, created_at=NOW, gap="when"))
        assert [i.id for i in s.needs_look()] == [waiting.id]
        assert s.fill_gap(waiting.id, due_at=NOW + timedelta(hours=8))
        assert s.get(waiting.id).gap is None and s.get(waiting.id).kind is Kind.REMINDER


def test_the_bare_reply_window_is_a_setting_with_a_sane_range():
    assert confirmation(Config.from_env({})).window == timedelta(minutes=30)
    assert confirmation(Config.from_env({"MAESTRO_GAP_REPLY_MINUTES": "5"})).window == \
        timedelta(minutes=5)
    with pytest.raises(ValueError, match="between 0 and 1440"):
        confirmation(Config.from_env({"MAESTRO_GAP_REPLY_MINUTES": "0"}))
    with pytest.raises(ValueError, match="between 0 and 1440"):
        confirmation(Config.from_env({"MAESTRO_GAP_REPLY_MINUTES": "2000"}))


def test_a_shorter_window_is_honoured_by_the_channels(store):
    """The setting really narrows the shortcut, and naming the item still reaches it."""
    item = split(store, UNSURE).needs_look[0]
    soon = NOW + timedelta(minutes=10)
    assert gaps.match_reply(store, *OWNER, "5pm", now=soon, window=timedelta(minutes=5)) is None
    assert gaps.match_reply(store, *OWNER, "5pm", now=soon, window=timedelta(minutes=30)) is not None
    named = gaps.match_reply(store, *OWNER, f"#{item.id} 5pm", now=soon, window=timedelta(minutes=5))
    assert named is not None and named.item.id == item.id


def test_a_command_in_a_capture_is_applied_and_never_asked_about(store):
    """Done, snooze and stop name an item that already exists, so there is nothing missing to ask
    about — they are carried out alongside whatever the same message saved."""
    from maestro.actions import Done

    existing = split(store, SURE).added[0]
    result = gaps.split_apply(store, [Done(item_id=existing.id), CreateTask(text="Pack before")],
                              Confidence(), *OWNER, "done 1 and pack before", now=NOW)
    assert [(i.text, i.gap) for i in result.saved] == [("Pack before", "when")]
    assert store.get(existing.id).status is Status.DONE


def test_dismissing_by_name_reaches_exactly_that_question(store):
    first = split(store, UNSURE).needs_look[0]
    second = split(store, OTHER_UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, f"dismiss #{second.id}", now=NOW)
    assert "Dismissed" in gaps.answer(store, reply, now=NOW)[0]
    assert [i.id for i in store.needs_look()] == [first.id]
    # A number that is not this sender's question reaches nothing and is captured instead.
    assert gaps.match_reply(store, *OWNER, f"dismiss #{second.id}", now=NOW) is None
    assert gaps.match_reply(store, "telegram", "7", f"dismiss #{first.id}", now=NOW) is None


def test_several_questions_from_one_message_are_listed_together(store):
    actions = [CreateTask(text="Pack before"), CreateTask(text="Call mom morning")]
    result = gaps.split_apply(store, actions, Confidence(), *OWNER, "…", now=NOW)
    said = gaps.worded(result)
    assert "2 need a look" in said
    assert all(f"#{i.id} {i.text} needs a time" in said for i in result.needs_look)


def test_a_question_about_words_asks_for_words_not_a_time(store):
    item = store.add(Item(text="Um", kind=Kind.NOTE, created_at=NOW, gap="what"))
    assert "needs a description: reply with a description" in gaps.asked([item])
    assert gaps.asked([]) == ""


def test_doctor_says_which_mode_is_in_force(monkeypatch, capsys, tmp_path):
    """The default changed, so an operator should see it without reading the changelog."""
    from maestro import doctor

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    for value, expected in [("split", "split: a reading with no readable time"),
                            ("unsure", "unsure: a doubted message is held whole"),
                            ("never", "never: every capture is applied at once")]:
        config = Config.from_env({"MAESTRO_CONFIRM": value, "MAESTRO_DB": str(tmp_path / "m.db")})
        doctor.run(config)
        assert expected in capsys.readouterr().out

    config = Config.from_env({"MAESTRO_CONFIRM": "maybe", "MAESTRO_DB": str(tmp_path / "m.db")})
    assert doctor.run(config) is False
    assert "must be split, unsure or never" in capsys.readouterr().out


def test_an_absurd_item_number_in_a_reply_answers_nothing_and_raises_nothing(store):
    """Anything from the network is untrusted (ENGINEERING.md). An id of thousands of digits must
    not reach int(), which raises above 4300 of them, so the matcher bounds what it will read as
    one. An unreadable time doesn't raise either: it simply isn't an answer."""
    item = split(store, UNSURE).needs_look[0]
    huge = "9" * 5000
    assert gaps.match_reply(store, *OWNER, f"#{huge} 5pm", now=NOW) is None
    assert gaps.match_reply(store, *OWNER, f"fill {huge} 5pm", now=NOW) is None
    assert gaps.match_reply(store, *OWNER, f"dismiss #{huge}", now=NOW) is None
    assert gaps.match_reply(store, *OWNER, huge, now=NOW) is None
    assert gaps.match_reply(store, *OWNER, "in 99999999999 hours", now=NOW) is None
    assert store.get(item.id).gap == "when"  # and the question is left exactly as it was


def test_filling_a_gap_is_not_counted_as_a_new_capture(store):
    """The item was counted when its message was split; answering completes it, it isn't a capture."""
    from maestro.sms import SmsHandler

    handler = SmsHandler(store, "token", "+15550001111",
                         confirm=Confirmation(mode=ConfirmMode.SPLIT))
    handler.handle({"From": "+15550001111", "Body": UNSURE, "MessageSid": "SM1"})
    before = _captures_counted("sms")
    handler.handle({"From": "+15550001111", "Body": "5pm", "MessageSid": "SM2"})
    assert _captures_counted("sms") == before


def _captures_counted(source: str) -> float:
    from maestro.observability import METRICS

    for line in METRICS.render().splitlines():
        if line.startswith("maestro_captures_total{") and f'source="{source}"' in line:
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_voice_still_applies_directly(store):
    """ADR 0016's exemption stands (ADR 0017): a phone call has no reply loop, so a voice capture
    is never held back as a question nobody on a call could answer."""
    from maestro.voice import VoiceHandler

    voice = VoiceHandler(store, "token", "+15550001111")  # built without confirmation settings
    voice.capture({"From": "+15550001111", "SpeechResult": UNSURE, "Confidence": "0.9",
                   "CallSid": "CA1"})
    (item,) = store.list()
    assert item.gap is None and store.needs_look() == []


def test_dismiss_with_nothing_waiting_is_captured_as_usual(store):
    """ADR 0005: with no question to answer, "dismiss" is just something someone said."""
    assert gaps.match_reply(store, *OWNER, "dismiss", now=NOW) is None
    assert gaps.match_reply(store, *OWNER, "dismiss #4", now=NOW) is None


def test_an_unreadable_answer_to_a_what_question_leaves_it_open(store):
    item = store.add(Item(text="Um", kind=Kind.NOTE, created_at=NOW, gap="what"))
    assert gaps.fill(store, item, "in 99999999999 hours", now=NOW) is None
    assert store.get(item.id).gap == "what"


def test_dismissing_something_another_answer_already_removed_says_so(store):
    """The matched item is a snapshot; by the time the answer runs, someone may have dealt with it."""
    item = split(store, UNSURE).needs_look[0]
    reply = gaps.match_reply(store, *OWNER, "dismiss", now=NOW)
    assert gaps.dismiss(store, item)  # the other channel gets there first
    said, filled = gaps.answer(store, reply, now=NOW)
    assert filled is None and f"No item #{item.id} needing a look" in said


# --- suggestions, keep and ISO answers (#128) --------------------------------------------------


def test_suggestions_for_a_when_gap_come_with_reasons(client):
    plumber = client.post("/capture", json={"text": "call the plumber sometime next week"}).json()
    item_id = plumber["needs_look"][0]["id"]
    body = client.get(f"/needs-look/{item_id}/suggestions").json()
    assert body["said"] == "sometime next week"
    assert len(body["suggestions"]) == 3
    for s in body["suggestions"]:
        assert datetime.fromisoformat(s["at"]) > datetime.now(TZ)
        assert s["reason"]
    assert [s["reason"] for s in body["suggestions"]][0] == "start of the week"
    assert client.get("/needs-look/999/suggestions").status_code == 404


def test_a_suggestion_round_trips_exactly_as_the_answer(client, store):
    item_id = client.post("/capture", json={"text": UNSURE}).json()["needs_look"][0]["id"]
    chosen = client.get(f"/needs-look/{item_id}/suggestions").json()["suggestions"][0]["at"]
    filled = client.post(f"/needs-look/{item_id}", json={"phrase": chosen}).json()
    assert filled["gap"] is None and filled["kind"] == "reminder"
    assert datetime.fromisoformat(filled["due_at"]) == datetime.fromisoformat(chosen)
    # scheduled for real: the item is due when its time comes
    item = store.get(item_id)
    assert item.due_at == datetime.fromisoformat(chosen).astimezone(TZ)


def test_a_naive_iso_stamp_goes_through_the_rules_not_the_shortcut(client, store):
    item_id = client.post("/capture", json={"text": UNSURE}).json()["needs_look"][0]["id"]
    # No offset, so not a suggestion of ours; the rules read it as a date, in the user's timezone.
    filled = client.post(f"/needs-look/{item_id}", json={"phrase": "2027-01-04T09:00:00"}).json()
    assert filled["gap"] is None
    assert store.get(item_id).due_at == dt(2027, 1, 4, 9, 0)


def test_keep_closes_the_gap_and_leaves_an_anytime_task(client, store):
    item_id = client.post("/capture", json={"text": UNSURE}).json()["needs_look"][0]["id"]
    kept = client.post(f"/needs-look/{item_id}/keep").json()
    assert kept["gap"] is None and kept["kind"] == "task" and kept["due_at"] is None
    assert client.get("/needs-look").json() == []
    feed_body = client.get("/feed").json()
    assert [i["id"] for i in feed_body["anytime"]] == [item_id]
    # keeping it twice: the question is gone
    assert client.post(f"/needs-look/{item_id}/keep").status_code == 404
    assert client.post("/needs-look/999/keep").status_code == 404
