"""#117: confirm before acting on an unsure interpretation, with undo.

Confirmation is opt-in (`MAESTRO_CONFIRM=unsure`); with the default, every test here that checks
"nothing changed" is checking the promise that rules-only behaviour is untouched. Every test uses
the rules or a fake provider: nothing reaches a network or a real key.
"""

from datetime import datetime, timedelta

import pytest
from conftest import TZ, dt

from maestro import captures
from maestro.captures import Confirmation, Outcome
from maestro.config import Config, confirmation
from maestro.interpret import Context, interpret_assessed
from maestro.models import CaptureStatus, Item, Kind
from maestro.observability import METRICS
from maestro.store import MIGRATIONS, Store

NOW = dt(2026, 10, 27, 8, 0)  # a Tuesday morning
CONTEXT = Context(tz=TZ, now=NOW)
SURE = "call mom tomorrow at 5pm"
UNSURE = "call the plumber sometime next week"  # unread_time: the rules are unsure (#116)


@pytest.fixture
def store():
    with Store(":memory:", tz=TZ) as s:
        yield s


def read(text):
    return interpret_assessed(text, CONTEXT)


def propose(store, text=UNSURE, now=NOW, ttl=timedelta(hours=24), dedupe_key=None,
            source="telegram", address="42"):
    actions, confidence = read(text)
    capture, _ = captures.propose(store, actions, confidence, source, address, text,
                                  ttl=ttl, dedupe_key=dedupe_key, now=now)
    return capture


# --- opt-in: the default never asks --------------------------------------------------------------


def test_confirmation_is_off_by_default():
    assert confirmation(Config()) is None
    assert confirmation(Config.from_env({})) is None
    assert confirmation(Config.from_env({"MAESTRO_CONFIRM": "unsure"})) == \
        Confirmation(ttl=timedelta(hours=24))
    assert confirmation(Config.from_env({"MAESTRO_CONFIRM": "unsure",
                                         "MAESTRO_CONFIRM_HOURS": "2"})).ttl == timedelta(hours=2)


@pytest.mark.parametrize("env,complaint", [
    ({"MAESTRO_CONFIRM": "maybe"}, "must be never or unsure"),
    ({"MAESTRO_CONFIRM": "unsure", "MAESTRO_CONFIRM_HOURS": "0"}, "between 0 and 168"),
])
def test_a_bad_confirm_setting_is_one_sentence(env, complaint):
    with pytest.raises(ValueError, match=complaint):
        confirmation(Config.from_env(env))


def test_with_the_default_even_an_unsure_capture_is_applied_at_once(tmp_path, monkeypatch, capsys):
    """Rules-only behaviour unchanged: no question, no captures row, the item just saves."""
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    db = str(tmp_path / "m.db")
    assert main(["--db", db, "add", *UNSURE.split()]) == 0
    assert "Did you mean" not in capsys.readouterr().out
    with Store(db, tz=TZ) as s:
        (item,) = s.list()
        assert item.text.startswith("Call the plumber")
        assert s.captures() == []


# --- what needs confirmation ----------------------------------------------------------------------


def test_an_unsure_reading_needs_confirmation_and_a_sure_one_does_not():
    actions, confidence = read(UNSURE)
    assert captures.needs_confirmation(actions, confidence)
    actions, confidence = read(SURE)
    assert not captures.needs_confirmation(actions, confidence)


def test_several_actions_from_one_message_need_confirmation_even_when_sure():
    """Saving three things nobody re-read is the riskiest reading, whoever produced it."""
    from maestro.confidence import Confidence

    (one, sure) = read(SURE)[0], Confidence()
    assert not captures.needs_confirmation([one], sure)
    assert captures.needs_confirmation([one, one], sure)


# --- proposing ------------------------------------------------------------------------------------


def test_a_proposal_is_stored_not_saved(store):
    capture = propose(store)
    assert capture.status == CaptureStatus.PENDING
    assert store.list() == []  # nothing reaches items until confirmed
    assert capture.interpreter == "rules" and "unread_time" in capture.doubts
    assert captures.proposed_lines(capture) == ["⏰ Call the plumber sometime (Tue 03 Nov 09:00)"]
    assert METRICS.get("maestro_captures_confirmation_total", outcome="proposed") == 1


def test_a_proposal_expires_at_the_due_time_when_that_is_sooner(store):
    soon = propose(store, "check the oven in 1 hour and in 99999999999 hours", now=NOW)
    assert soon.expires_at == NOW + timedelta(hours=1)  # not 24h: past the hour it is noise
    later = propose(store, UNSURE, now=NOW)
    assert later.expires_at == NOW + timedelta(hours=24)  # "sometime next week" is days away


def test_the_question_names_the_capture_and_when_it_expires(store):
    capture = propose(store)
    question = captures.question(capture)
    assert "Did you mean:" in question and "Call the plumber sometime" in question
    assert f"#{capture.id}" in question and "expires" in question


# --- confirming exactly once ----------------------------------------------------------------------


def test_confirming_applies_once_and_a_double_tap_changes_nothing(store):
    capture = propose(store)
    first, items = captures.confirm(store, capture.id, via="telegram", now=NOW)
    again, same = captures.confirm(store, capture.id, via="telegram", now=NOW)
    assert (first, again) == (Outcome.APPLIED, Outcome.ALREADY)
    assert [i.id for i in items] == [i.id for i in same]  # the double tap sees the items, saves none
    (item,) = store.list()
    assert item.capture_id == capture.id and item.interpreter == "rules"
    assert store.get_capture(capture.id).status == CaptureStatus.APPLIED
    assert METRICS.get("maestro_captures_confirmation_total", outcome="confirmed") == 1


def test_a_capture_that_proposed_several_things_saves_them_all_exactly_once(store):
    one, confidence = read(SURE)
    (action,) = one
    capture, _ = captures.propose(store, [action, action], confidence, "api", "", SURE, now=NOW)
    _, items = captures.confirm(store, capture.id, via="api", now=NOW)
    captures.confirm(store, capture.id, via="api", now=NOW)
    assert len(items) == 2 and len(store.list()) == 2
    assert all(i.capture_id == capture.id for i in store.list())


def test_cancelling_saves_nothing_and_is_final(store):
    capture = propose(store)
    assert captures.cancel(store, capture.id, via="cli", now=NOW) == Outcome.CANCELLED
    assert store.list() == []
    outcome, items = captures.confirm(store, capture.id, via="cli", now=NOW)
    assert outcome == Outcome.ALREADY and items == []  # a late yes can't resurrect it


# --- expiry ---------------------------------------------------------------------------------------


def test_an_expired_capture_stays_visible_but_is_never_applied(store):
    capture = propose(store, ttl=timedelta(hours=1))
    late = NOW + timedelta(hours=2)
    outcome, items = captures.confirm(store, capture.id, via="cli", now=late)
    assert (outcome, items) == (Outcome.EXPIRED, [])
    assert store.list() == []
    assert store.get_capture(capture.id).status == CaptureStatus.EXPIRED  # visible, marked
    assert captures.expire(store, now=late) == 0  # the refused confirm already tidied it


def test_the_scheduler_tick_sweeps_expired_captures(store):
    from maestro.notifier import MultiNotifier
    from maestro.scheduler import tick

    propose(store, ttl=timedelta(minutes=-1), now=datetime.now(TZ))  # already overdue
    tick(store, MultiNotifier([]))
    (capture,) = store.captures()
    assert capture.status == CaptureStatus.EXPIRED
    assert METRICS.get("maestro_captures_confirmation_total", outcome="expired") == 1


# --- undo -----------------------------------------------------------------------------------------


def test_undo_removes_exactly_what_the_capture_created(store):
    kept = store.add(Item(text="Keep me", kind=Kind.NOTE, created_at=NOW))
    capture = propose(store)
    _, items = captures.confirm(store, capture.id, via="telegram", now=NOW)
    outcome, removed = captures.undo(store, capture.id, via="telegram",
                                     now=NOW + timedelta(minutes=9))
    assert (outcome, removed) == (Outcome.UNDONE, 1)
    assert [i.id for i in store.list()] == [kept.id]
    assert store.get_capture(capture.id).status == CaptureStatus.UNDONE
    # and never twice: a second undo (or a confirm) has nothing left to act on
    assert captures.undo(store, capture.id, via="telegram", now=NOW)[0] == Outcome.ALREADY
    assert captures.confirm(store, capture.id, via="telegram", now=NOW)[0] == Outcome.ALREADY


def test_undo_after_ten_minutes_is_too_late(store):
    capture = propose(store)
    captures.confirm(store, capture.id, via="cli", now=NOW)
    outcome, removed = captures.undo(store, capture.id, via="cli", now=NOW + timedelta(minutes=10))
    assert (outcome, removed) == (Outcome.TOO_LATE, 0)
    assert len(store.list()) == 1  # still there; done/stop/rm are the way now


# --- matching replies -----------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("yes", ("yes", None)), ("YES", ("yes", None)), ("confirm", ("yes", None)),
    ("no", ("no", None)), ("cancel 12", ("no", 12)), ("yes #12", ("yes", 12)),
    ("undo", ("undo", None)), ("Undo 3", ("undo", 3)),
    ("yes please", None), ("note to self: yes", None), ("undo the damage", None),
])
def test_only_exact_reply_shapes_match(text, expected):
    assert captures.match_reply(text) == expected


def test_a_bare_yes_answers_the_senders_most_recent_pending_capture(store):
    older = propose(store, "pick up the kids before 3pm", now=NOW - timedelta(minutes=5))
    newer = propose(store, UNSURE, now=NOW)
    target = captures.target(store, "telegram", "42", "yes", None, now=NOW)
    assert target.id == newer.id
    assert captures.target(store, "telegram", "42", "yes", older.id, now=NOW).id == older.id


def test_a_reply_never_reaches_another_senders_capture(store):
    capture = propose(store, source="telegram", address="42")
    assert captures.target(store, "telegram", "666", "yes", capture.id, now=NOW) is None
    assert captures.target(store, "twilio_sms", "42", "yes", capture.id, now=NOW) is None
    assert captures.target(store, "telegram", "666", "yes", None, now=NOW) is None


def test_undo_matches_the_most_recent_applied_capture_still_in_its_window(store):
    capture = propose(store)
    assert captures.target(store, "telegram", "42", "undo", None, now=NOW) is None  # nothing applied
    captures.confirm(store, capture.id, via="telegram", now=NOW)
    assert captures.target(store, "telegram", "42", "undo", None,
                           now=NOW + timedelta(minutes=5)).id == capture.id
    assert captures.target(store, "telegram", "42", "undo", None,
                           now=NOW + timedelta(minutes=11)) is None  # window over


# --- migrating from the previous schema -----------------------------------------------------------


def test_migrating_from_the_schema_before_confirmations(tmp_path):
    """Schema 11 is the release before #117 (one migration after #116). Its items come through
    with no capture to undo, and the new table works afterwards."""
    path = tmp_path / "v11.db"
    with Store(path, tz=TZ) as s:
        s.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=NOW,
                   due_at=dt(2026, 10, 28, 17, 0), source_text="call mom tomorrow at 5pm",
                   interpreter="rules"))
        s.conn.execute("DROP TABLE captures")
        s.conn.execute("ALTER TABLE items DROP COLUMN capture_id")
        s.conn.execute("PRAGMA user_version = 11")
        s.conn.commit()

    with Store(path, tz=TZ) as s:
        assert s.schema_version == len(MIGRATIONS) == 12
        (item,) = s.list()
        assert (item.text, item.interpreter, item.capture_id) == ("Call mom", "rules", None)
        capture = propose(s)
        _, items = captures.confirm(s, capture.id, via="cli", now=NOW)
        assert s.get(items[0].id).capture_id == capture.id


# --- the channels ---------------------------------------------------------------------------------


def telegram_bot(store, reply=None):
    from test_telegram import CHAT, FakeTransport

    from maestro.telegram import TelegramBot, TelegramClient

    transport = FakeTransport()
    bot = TelegramBot(TelegramClient(transport), store, str(CHAT), confirm=Confirmation())
    return transport, bot


def test_telegram_asks_with_buttons_and_yes_saves_exactly_once(store):
    from test_telegram import button, message

    transport, bot = telegram_bot(store)
    bot.handle(message(UNSURE))
    (question,) = transport.sent()
    assert "Did you mean:" in question["text"] and store.list() == []
    (row,) = question["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in row] == ["✅ Yes", "✏️ Change", "✖ Cancel"]
    (capture,) = store.captures()

    bot.handle(button(f"cap:{capture.id}:yes", update_id=2))
    bot.handle(button(f"cap:{capture.id}:yes", update_id=3))  # the double tap
    (item,) = store.list()
    assert item.text == "Call the plumber sometime" and item.capture_id == capture.id
    answers = [p["text"] for p, in zip(transport.sent("answerCallbackQuery"))]
    assert "Saved" in answers[0] and "Already saved" in answers[1]
    assert METRICS.get("maestro_captures_total", source="telegram") == 1


def test_telegram_plain_yes_and_undo_answer_in_words_too(store):
    from test_telegram import message

    transport, bot = telegram_bot(store)
    bot.handle(message(UNSURE))
    bot.handle(message("yes", update_id=2))
    (item,) = store.list()
    assert item.text == "Call the plumber sometime"
    assert "undo" in transport.sent()[1]["text"].lower()
    bot.handle(message("undo", update_id=3))
    assert store.list() == []
    assert "Undone" in transport.sent()[2]["text"]


def test_telegram_change_cancels_and_invites_the_corrected_wording(store):
    from test_telegram import button, message

    transport, bot = telegram_bot(store)
    bot.handle(message(UNSURE))
    (capture,) = store.captures()
    bot.handle(button(f"cap:{capture.id}:change", update_id=2))
    assert store.get_capture(capture.id).status == CaptureStatus.CANCELLED
    ((answer,),) = zip(transport.sent("answerCallbackQuery"))
    assert "send it again" in answer["text"].lower()


def test_telegram_yes_with_nothing_pending_stays_a_capture(store):
    from test_telegram import message

    transport, bot = telegram_bot(store)
    bot.handle(message("yes"))
    (item,) = store.list()  # ADR 0005: anything that isn't exactly a command is captured
    assert item.text == "Yes" and item.kind == Kind.NOTE


def test_telegram_a_sure_capture_is_saved_at_once_even_with_confirm_on(store):
    from test_telegram import message

    transport, bot = telegram_bot(store)
    bot.handle(message(SURE))
    (item,) = store.list()
    assert item.text == "Call mom" and item.capture_id is None
    assert store.captures() == []


def sms_handler(store):
    from test_capture_paths import SMS_TOKEN
    from test_sms import OWNER

    from maestro.sms import SmsHandler

    return SmsHandler(store, SMS_TOKEN, OWNER, confirm=Confirmation()), OWNER


def test_sms_asks_and_yes_undo_work_by_reply(store):
    handler, owner = sms_handler(store)
    asked = handler.handle({"From": owner, "To": "+15550001111", "Body": UNSURE, "MessageSid": "SM1"})
    assert "Did you mean:" in asked and "Reply YES or NO" in asked and store.list() == []

    retried = handler.handle({"From": owner, "To": "+15550001111", "Body": UNSURE, "MessageSid": "SM1"})
    assert len(store.captures()) == 1  # the provider retry is asked again, not proposed again
    assert "Did you mean:" in retried

    saved = handler.handle({"From": owner, "To": "+15550001111", "Body": "YES", "MessageSid": "SM2"})
    assert "Saved" in saved and "UNDO within 10 min" in saved
    assert len(store.list()) == 1
    assert METRICS.get("maestro_captures_total", source="sms") == 1

    undone = handler.handle({"From": owner, "To": "+15550001111", "Body": "undo", "MessageSid": "SM3"})
    assert "Undone" in undone and store.list() == []


def test_sms_no_cancels_and_a_stranger_cannot_answer(store):
    handler, owner = sms_handler(store)
    handler.handle({"From": owner, "To": "+15550001111", "Body": UNSURE, "MessageSid": "SM1"})
    assert handler.handle({"From": "+19998887777", "Body": "YES", "MessageSid": "SM2"}) \
        == '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'  # not the owner: ignored
    answer = handler.handle({"From": owner, "To": "+15550001111", "Body": "NO", "MessageSid": "SM3"})
    assert "Cancelled" in answer and store.list() == []
    (capture,) = store.captures()
    assert capture.status == CaptureStatus.CANCELLED


@pytest.fixture
def api_client(store):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    with TestClient(create_app(store, api_token="t0ken", confirm=Confirmation())) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        yield client


def test_the_api_answers_202_with_a_proposal_and_confirms_once(api_client, store):
    asked = api_client.post("/items", json={"text": UNSURE})
    assert asked.status_code == 202
    proposal = asked.json()
    assert proposal["status"] == "pending" and proposal["doubts"] == ["unread_time"]
    assert store.list() == []

    first = api_client.post(f"/captures/{proposal['id']}/confirm")
    again = api_client.post(f"/captures/{proposal['id']}/confirm")
    assert first.status_code == again.status_code == 200
    assert first.json()["item_ids"] == again.json()["item_ids"] and len(store.list()) == 1

    undone = api_client.post(f"/captures/{proposal['id']}/undo")
    assert undone.status_code == 200 and undone.json()["status"] == "undone"
    assert store.list() == []
    assert api_client.post(f"/captures/{proposal['id']}/confirm").status_code == 409
    assert api_client.post("/captures/9999/confirm").status_code == 404


def test_the_api_lists_pending_and_expired_proposals(api_client, store):
    api_client.post("/items", json={"text": UNSURE})
    sure = api_client.post("/items", json={"text": SURE})
    assert sure.status_code == 201 and sure.json()["text"] == "Call mom"  # sure: saved as always

    listed = api_client.get("/captures").json()
    assert [c["status"] for c in listed] == ["pending"]
    expired = propose(store, ttl=timedelta(minutes=-1), now=datetime.now(TZ), source="api",
                      address="")
    listed = api_client.get("/captures").json()
    assert sorted(c["status"] for c in listed) == ["expired", "pending"]
    assert api_client.get("/captures?status=expired").json()[0]["id"] == expired.id
    assert api_client.get("/captures?status=nonsense").status_code == 422


def test_cli_asks_on_a_terminal_and_undo_takes_it_back(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_CONFIRM", "unsure")
    db = str(tmp_path / "m.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["--db", db, "add", *UNSURE.split()]) == 0
    out = capsys.readouterr().out
    assert "Did you mean:" in out and "Saved" in out and "maestro undo 1" in out

    assert main(["--db", db, "undo", "1"]) == 0
    assert "Undone: removed 1 item" in capsys.readouterr().out
    with Store(db, tz=TZ) as s:
        assert s.list() == []


def test_cli_without_a_terminal_leaves_it_pending_and_confirm_saves_it(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_CONFIRM", "unsure")
    db = str(tmp_path / "m.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["--db", db, "add", *UNSURE.split()]) == 0
    out = capsys.readouterr().out
    assert "maestro confirm 1" in out
    with Store(db, tz=TZ) as s:
        assert s.list() == []

    assert main(["--db", db, "list", "--proposals"]) == 0
    assert "Call the plumber sometime" in capsys.readouterr().out
    assert main(["--db", db, "confirm", "1"]) == 0
    assert "Saved" in capsys.readouterr().out
    assert main(["--db", db, "cancel", "1"]) == 0  # already decided: said, not re-done
    assert "Already" in capsys.readouterr().out
    with Store(db, tz=TZ) as s:
        assert len(s.list()) == 1


def test_cli_rejects_a_bad_confirm_setting_in_one_sentence(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_CONFIRM", "maybe")
    assert main(["--db", str(tmp_path / "m.db"), "add", "milk"]) == 2
    assert "MAESTRO_CONFIRM must be never or unsure" in capsys.readouterr().err
