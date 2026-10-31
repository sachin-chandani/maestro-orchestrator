"""What every storage backend must do, exercised only through `maestro.storage.Store`.

These tests are the contract: no SQL, no connection, nothing a PostgreSQL backend (#34) could not
satisfy. They run against each backend in BACKENDS, which today is SQLite alone; adding a backend
means adding one entry here and making the suite pass.

Backend-specific behaviour (WAL, `PRAGMA user_version`, locking between processes) stays in
`tests/test_channels.py` and `tests/test_lease.py`.
"""

from datetime import timedelta

import pgsupport
import pytest
from conftest import TZ, dt

from maestro.models import Acknowledgement, Delivery, Item, Kind, Status
from maestro.storage import SQLiteStore, logic, open_store, parse_url

NOW = dt(2026, 10, 26, 9, 0)  # a Monday
LATER = dt(2026, 10, 26, 17, 0)
TOMORROW = dt(2026, 10, 27, 9, 0)

BACKENDS = {"sqlite": lambda: SQLiteStore(":memory:", tz=TZ)}

# PostgreSQL needs a server, so it joins in when one is offered and is skipped otherwise; CI always
# offers one. A PostgreSQL database is reused between tests, so it is emptied first, where a fresh
# in-memory SQLite database starts empty by itself.
if pgsupport.URL:
    BACKENDS["postgres"] = lambda: pgsupport.truncate(open_store(pgsupport.URL, tz=TZ))


@pytest.fixture(params=sorted(BACKENDS))
def store(request):
    with BACKENDS[request.param]() as store:
        yield store


def reminder(store, text="Take pills", due=NOW, recurrence=None, created=None):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=created or NOW, due_at=due,
                          recurrence=recurrence, source_text=text))


def note(store, text="Wifi password is on the router", created=None):
    return store.add(Item(text=text, kind=Kind.NOTE, created_at=created or NOW, source_text=text))


def deliver(store, item, occurrence, channel="console", at=None):
    """Record a successful delivery, as the scheduler would."""
    store.record_delivery(item.id, occurrence,
                          Delivery(channel=channel, attempts=1, delivered_at=at or occurrence))


# --- adding and reading ---------------------------------------------------------------------


def test_an_added_item_comes_back_with_an_id(store):
    item = reminder(store)
    assert item.id is not None
    assert store.get(item.id) == item


def test_ids_are_unique_per_item(store):
    assert reminder(store).id != reminder(store, text="Walk the dog").id


def test_an_unknown_id_is_none(store):
    assert store.get(12345) is None


def test_a_note_keeps_its_kind_and_has_no_due_time(store):
    saved = store.get(note(store).id)
    assert saved.kind == Kind.NOTE and saved.due_at is None


def test_every_field_survives_a_round_trip(store):
    item = store.add(Item(text="Take pills", kind=Kind.REMINDER, created_at=NOW, due_at=TOMORROW,
                          status=Status.PENDING, source_text="take pills every day at 9am",
                          recurrence="daily"))
    assert store.get(item.id) == item


def test_times_come_back_aware_and_in_the_store_timezone(store):
    saved = store.get(reminder(store).id)
    assert saved.due_at.utcoffset() is not None
    assert saved.due_at == NOW and saved.created_at == NOW


def test_a_naive_time_is_read_as_local_wall_clock(store):
    item = reminder(store, due=NOW.replace(tzinfo=None))
    assert store.get(item.id).due_at == NOW


def test_counts_are_grouped_by_kind_and_status(store):
    reminder(store)
    reminder(store, text="Walk the dog")
    note(store)
    assert store.counts() == {(Kind.REMINDER, Status.PENDING): 2, (Kind.NOTE, Status.PENDING): 1}


# --- listing and searching ------------------------------------------------------------------


def test_list_puts_reminders_before_notes_soonest_first(store):
    kept = note(store)
    later = reminder(store, text="Pay rent", due=TOMORROW)
    sooner = reminder(store, text="Take pills", due=NOW)
    assert [i.id for i in store.list()] == [sooner.id, later.id, kept.id]


def test_list_orders_a_snoozed_reminder_by_its_re_send(store):
    item = reminder(store, due=TOMORROW, recurrence="daily")
    other = reminder(store, text="Pay rent", due=LATER)
    store.snooze(item.id, dt(2026, 10, 26, 12, 0), via="cli", now=NOW)
    assert [i.id for i in store.list()] == [item.id, other.id]


def test_items_captured_in_the_same_second_keep_a_stable_order(store):
    """Times are stored to the second, so same-second captures tie. The order must be the same on
    every backend and must not reshuffle when something unrelated is updated."""
    notes = [note(store, text=f"Note {n}", created=NOW) for n in range(4)]
    expected = [n.id for n in notes]
    assert [i.id for i in store.list()] == expected
    assert [i.id for i in store.search("Note")] == list(reversed(expected))  # newest first

    store.set_status(notes[1].id, Status.FIRED)  # rewrites the row without touching a sort key
    store.set_status(notes[1].id, Status.PENDING)
    assert [i.id for i in store.list()] == expected
    assert [i.id for i in store.search("Note")] == list(reversed(expected))


def test_due_reminders_sharing_a_time_keep_a_stable_order(store):
    reminders = [reminder(store, text=f"Reminder {n}", due=NOW) for n in range(4)]
    assert [i.id for i in store.due(NOW)] == [r.id for r in reminders]


def test_list_orders_notes_by_when_they_were_captured(store):
    second = note(store, text="Second", created=LATER)
    first = note(store, text="First", created=NOW)
    assert [i.text for i in store.list()] == ["First", "Second"]
    assert {first.id, second.id} == {i.id for i in store.list()}


def test_list_hides_done_items_unless_asked(store):
    item = reminder(store)
    store.set_status(item.id, Status.DONE)
    assert store.list() == []
    assert [i.id for i in store.list(include_done=True)] == [item.id]


def test_list_shows_fired_items(store):
    item = reminder(store)
    store.set_status(item.id, Status.FIRED)
    assert [i.id for i in store.list()] == [item.id]


def test_search_matches_part_of_the_text_whatever_the_case(store):
    item = reminder(store, text="Take PILLS")
    assert [i.id for i in store.search("pills")] == [item.id]
    assert [i.id for i in store.search("TAKE")] == [item.id]


def test_search_returns_newest_first_and_nothing_for_a_miss(store):
    old = note(store, text="old password", created=NOW)
    new = note(store, text="new password", created=LATER)
    assert [i.id for i in store.search("password")] == [new.id, old.id]
    assert store.search("nothing here") == []


def test_search_finds_done_items_too(store):
    item = reminder(store, text="Take pills")
    store.set_status(item.id, Status.DONE)
    assert [i.id for i in store.search("pills")] == [item.id]


# --- what is due ------------------------------------------------------------------------------


def test_due_returns_reminders_whose_time_has_come(store):
    now_due = reminder(store, due=NOW)
    reminder(store, text="Later", due=LATER)
    assert [i.id for i in store.due(NOW)] == [now_due.id]


def test_due_ignores_notes_and_items_that_are_not_pending(store):
    note(store)
    fired = reminder(store, text="Already fired")
    store.set_status(fired.id, Status.FIRED)
    done = reminder(store, text="Done")
    store.set_status(done.id, Status.DONE)
    assert store.due(LATER) == []


def test_due_returns_the_soonest_first(store):
    later = reminder(store, text="Later", due=LATER)
    sooner = reminder(store, text="Sooner", due=NOW)
    assert [i.id for i in store.due(LATER)] == [sooner.id, later.id]


def test_due_includes_a_snoozed_re_send_before_its_series(store):
    item = reminder(store, due=TOMORROW, recurrence="daily")
    store.snooze(item.id, LATER, via="cli", now=NOW)
    assert store.due(NOW) == []
    assert [i.id for i in store.due(LATER)] == [item.id]


# --- status, rescheduling and deletion --------------------------------------------------------


def test_set_status_reports_whether_anything_changed(store):
    item = reminder(store)
    assert store.set_status(item.id, Status.FIRED) is True
    assert store.get(item.id).status == Status.FIRED
    assert store.set_status(999, Status.FIRED) is False


def test_reschedule_moves_an_item_and_makes_it_pending_again(store):
    item = reminder(store)
    store.set_status(item.id, Status.FIRED)
    assert store.reschedule(item.id, TOMORROW) is True
    assert store.get(item.id).due_at == TOMORROW
    assert store.get(item.id).status == Status.PENDING
    assert store.reschedule(999, TOMORROW) is False


def test_rescheduling_a_note_turns_it_into_a_reminder(store):
    item = note(store)
    store.reschedule(item.id, TOMORROW)
    assert store.get(item.id).kind == Kind.REMINDER


def test_delete_removes_the_item_and_everything_recorded_about_it(store):
    item = reminder(store)
    deliver(store, item, NOW)
    store.add_once("sms", "SM1", Item(text="From SMS", kind=Kind.NOTE, created_at=NOW))
    assert store.delete(item.id) is True
    assert store.get(item.id) is None
    assert store.deliveries(item.id, NOW) == {}
    assert store.last_delivered("console") is None
    assert store.unacknowledged() == []
    assert store.delete(item.id) is False


def test_deleting_an_item_releases_the_inbound_message_that_made_it(store):
    item, _ = store.add_once("sms", "SM1", Item(text="From SMS", kind=Kind.NOTE, created_at=NOW))
    store.delete(item.id)
    assert store.is_claimed("sms", "SM1") is False


# --- done, stop and snooze --------------------------------------------------------------------


def test_done_on_a_one_off_marks_it_done(store):
    item = reminder(store)
    assert store.complete(item.id, via="cli", now=NOW) is True
    assert store.get(item.id).status == Status.DONE
    assert store.complete(999, via="cli", now=NOW) is False


def test_done_on_a_repeating_reminder_moves_it_to_the_next_occurrence(store):
    item = reminder(store, due=NOW, recurrence="daily")
    store.complete(item.id, via="cli", now=NOW)
    moved = store.get(item.id)
    assert moved.status == Status.PENDING and moved.due_at == TOMORROW


def test_stop_ends_a_repeating_reminder_for_good(store):
    item = reminder(store, due=NOW, recurrence="daily")
    store.snooze(item.id, LATER, via="cli", now=NOW)
    assert store.stop(item.id, via="cli", now=NOW) is True
    stopped = store.get(item.id)
    assert stopped.status == Status.DONE and stopped.snoozed_until is None
    assert store.stop(999, via="cli", now=NOW) is False


def test_snooze_moves_a_one_off_and_makes_it_pending(store):
    item = reminder(store)
    store.set_status(item.id, Status.FIRED)
    assert store.snooze(item.id, LATER, via="cli", now=NOW) is True
    snoozed = store.get(item.id)
    assert snoozed.due_at == LATER and snoozed.status == Status.PENDING
    assert snoozed.snoozed_until is None
    assert store.snooze(999, LATER, via="cli", now=NOW) is False


def test_snooze_on_a_repeating_reminder_keeps_the_series_time(store):
    item = reminder(store, due=NOW, recurrence="daily")
    store.snooze(item.id, LATER, via="cli", now=NOW)
    snoozed = store.get(item.id)
    assert snoozed.snoozed_until == LATER  # sent again this evening
    assert snoozed.due_at == TOMORROW  # and the series keeps 9am


def test_done_and_snooze_follow_the_shared_rules(store):
    """The store applies `maestro.storage.logic`, whose cases are covered in test_storage_logic."""
    item = reminder(store, due=NOW, recurrence="weekly:0")
    deliver(store, item, NOW)
    store.snooze(item.id, LATER, via="sms", now=NOW)
    series = logic.Series("weekly:0", NOW, None)
    assert store.get(item.id).due_at == logic.snooze(series, LATER, target=NOW).due_at


# --- the scheduler's bookkeeping ---------------------------------------------------------------


def test_finishing_a_one_off_occurrence_marks_it_fired(store):
    item = reminder(store)
    assert store.finish_occurrence(item.id, NOW, next_due=None) is True
    assert store.get(item.id).status == Status.FIRED


def test_finishing_a_repeating_occurrence_moves_it_on(store):
    item = reminder(store, due=NOW, recurrence="daily")
    assert store.finish_occurrence(item.id, NOW, next_due=TOMORROW) is True
    assert store.get(item.id).due_at == TOMORROW
    assert store.get(item.id).status == Status.PENDING


def test_finishing_does_nothing_once_someone_has_acted(store):
    """A done or snooze that lands between sending and finishing must not be overwritten."""
    item = reminder(store, due=NOW)
    store.complete(item.id, via="cli", now=NOW)
    assert store.finish_occurrence(item.id, NOW, next_due=None) is False
    assert store.get(item.id).status == Status.DONE


def test_finishing_does_nothing_if_the_occurrence_already_moved(store):
    item = reminder(store, due=NOW, recurrence="daily")
    store.finish_occurrence(item.id, NOW, next_due=TOMORROW)
    assert store.finish_occurrence(item.id, NOW, next_due=TOMORROW) is False


def test_finishing_a_snoozed_re_send_clears_it(store):
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    store.snooze(item.id, LATER, via="cli", now=NOW)  # this morning's reminder, again this evening
    assert store.get(item.id).snoozed_until == LATER
    assert store.finish_snooze(item.id, LATER) is True
    assert store.get(item.id).snoozed_until is None
    assert store.get(item.id).due_at == TOMORROW  # the series was never moved
    assert store.finish_snooze(item.id, LATER) is False


# --- deliveries ---------------------------------------------------------------------------------


def test_delivery_state_is_kept_per_channel_and_occurrence(store):
    item = reminder(store)
    store.record_delivery(item.id, NOW, Delivery(channel="console", attempts=1, delivered_at=NOW))
    store.record_delivery(item.id, NOW, Delivery(channel="ntfy", attempts=2, last_error="timeout",
                                                 next_attempt_at=LATER))
    state = store.deliveries(item.id, NOW)
    assert state["console"] == Delivery(channel="console", attempts=1, delivered_at=NOW)
    assert state["ntfy"] == Delivery(channel="ntfy", attempts=2, next_attempt_at=LATER,
                                     last_error="timeout")
    assert store.deliveries(item.id, TOMORROW) == {}


def test_recording_a_delivery_again_replaces_its_state(store):
    item = reminder(store)
    store.record_delivery(item.id, NOW, Delivery(channel="ntfy", attempts=1, last_error="timeout"))
    store.record_delivery(item.id, NOW, Delivery(channel="ntfy", attempts=2, delivered_at=LATER))
    assert store.deliveries(item.id, NOW) == {
        "ntfy": Delivery(channel="ntfy", attempts=2, delivered_at=LATER)
    }


def test_the_latest_delivered_occurrence_ignores_attempts_that_failed(store):
    item = reminder(store, due=NOW, recurrence="daily")
    assert store.latest_delivered_occurrence(item.id) is None
    deliver(store, item, NOW)
    store.record_delivery(item.id, TOMORROW, Delivery(channel="ntfy", attempts=1, last_error="down"))
    assert store.latest_delivered_occurrence(item.id) == NOW


def test_last_delivered_names_the_most_recent_item_on_a_channel(store):
    first = reminder(store, text="First")
    second = reminder(store, text="Second")
    deliver(store, first, NOW, channel="telegram", at=NOW)
    deliver(store, second, NOW, channel="telegram", at=LATER)
    assert store.last_delivered("telegram") == second.id
    assert store.last_delivered("ntfy") is None


def test_last_delivered_counts_escalation_stages_on_the_same_channel(store):
    item = reminder(store)
    deliver(store, item, NOW, channel="escalation2:twilio_sms")
    assert store.last_delivered("twilio_sms") == item.id


def test_last_delivered_reads_the_channel_after_the_first_colon(store):
    """An escalation stage is "escalation<N>:<channel>"; a channel whose own name has a colon still
    belongs to that channel, and every backend must split it the same way."""
    item = reminder(store)
    deliver(store, item, NOW, channel="escalation2:odd:name")
    assert store.last_delivered("odd:name") == item.id
    assert store.last_delivered("odd") is None


def test_last_delivered_forgets_a_deleted_item(store):
    item = reminder(store)
    deliver(store, item, NOW, channel="telegram")
    store.delete(item.id)
    assert store.last_delivered("telegram") is None


# --- acknowledgements ----------------------------------------------------------------------------


def test_a_delivered_reminder_is_unacknowledged_until_someone_acts(store):
    item = reminder(store)
    assert store.unacknowledged() == []
    deliver(store, item, NOW)
    assert store.unacknowledged() == [(store.get(item.id), NOW)]
    assert store.unacknowledged_ids() == frozenset({item.id})


def test_acting_on_a_recent_delivery_acknowledges_it(store):
    item = reminder(store)
    deliver(store, item, NOW)
    store.complete(item.id, via="telegram", now=NOW + timedelta(minutes=5))
    assert store.unacknowledged() == []
    assert store.unacknowledged_ids() == frozenset()


def test_an_acknowledgement_records_when_it_came_and_from_where(store):
    item = reminder(store)
    deliver(store, item, NOW)
    assert store.acknowledgement(item.id, NOW) is None
    replied = NOW + timedelta(minutes=5)
    store.complete(item.id, via="telegram", now=replied)
    assert store.acknowledgement(item.id, NOW) == Acknowledgement(acknowledged_at=replied,
                                                                  via="telegram")
    assert store.acknowledgement(item.id, TOMORROW) is None


def test_the_first_acknowledgement_of_an_occurrence_wins(store):
    """Two interfaces answering the same reminder (a Telegram tap, then a text) is one
    acknowledgement, not two."""
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    store.complete(item.id, via="telegram", now=NOW + timedelta(minutes=1))
    store.snooze(item.id, LATER, via="twilio_sms", now=NOW + timedelta(minutes=2), occurrence=NOW)
    assert store.acknowledgement(item.id, NOW).via == "telegram"


def test_each_occurrence_is_acknowledged_on_its_own(store):
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    store.complete(item.id, via="cli", now=NOW)
    deliver(store, item, TOMORROW)
    store.complete(item.id, via="web", now=TOMORROW)
    assert store.acknowledgement(item.id, NOW).via == "cli"
    assert store.acknowledgement(item.id, TOMORROW).via == "web"


def test_stopping_a_series_acknowledges_the_delivery_that_prompted_it(store):
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    store.stop(item.id, via="telegram", now=NOW + timedelta(minutes=5))
    assert store.acknowledgement(item.id, NOW).via == "telegram"
    assert store.unacknowledged_ids() == frozenset()


def test_a_snooze_acknowledges_the_delivery_too(store):
    item = reminder(store)
    deliver(store, item, NOW)
    store.snooze(item.id, LATER, via="sms", now=NOW + timedelta(minutes=5))
    assert store.unacknowledged_ids() == frozenset()


def test_an_old_delivery_is_not_what_a_reply_is_about(store):
    """Acting long after a reminder went out is about the next one, so the old delivery stays
    unacknowledged (`logic.ack_cutoff`)."""
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    store.complete(item.id, via="cli", now=NOW + logic.ACK_WINDOW + timedelta(minutes=1))
    assert store.unacknowledged_ids() == frozenset({item.id})


def test_a_reply_can_name_the_occurrence_it_answers_however_late(store):
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    store.complete(item.id, via="email", now=NOW + logic.ACK_WINDOW + timedelta(hours=1),
                   occurrence=NOW)
    assert store.unacknowledged_ids() == frozenset()


def test_only_the_latest_occurrence_counts_as_unacknowledged(store):
    item = reminder(store, due=NOW, recurrence="daily")
    deliver(store, item, NOW)
    deliver(store, item, TOMORROW)
    assert store.unacknowledged() == [(store.get(item.id), TOMORROW)]


def test_unacknowledged_can_be_limited_to_recent_occurrences(store):
    old = reminder(store, text="Old")
    new = reminder(store, text="New")
    deliver(store, old, NOW)
    deliver(store, new, TOMORROW)
    assert [i.text for i, _ in store.unacknowledged()] == ["Old", "New"]  # oldest delivery first
    assert [i.text for i, _ in store.unacknowledged(since=TOMORROW)] == ["New"]


def test_a_done_item_is_never_awaiting_acknowledgement(store):
    item = reminder(store)
    deliver(store, item, NOW)
    store.set_status(item.id, Status.DONE)
    assert store.unacknowledged() == []


# --- inbound messages -----------------------------------------------------------------------------


def test_the_same_inbound_message_captures_one_item(store):
    first, created = store.add_once("sms", "SM1", Item(text="Buy milk", kind=Kind.NOTE, created_at=NOW))
    again, created_again = store.add_once("sms", "SM1", Item(text="Buy milk", kind=Kind.NOTE,
                                                             created_at=NOW))
    assert created is True and created_again is False
    assert again.id == first.id
    assert len(store.list()) == 1


def test_the_same_id_from_another_source_is_a_different_message(store):
    store.add_once("sms", "ID", Item(text="From SMS", kind=Kind.NOTE, created_at=NOW))
    _, created = store.add_once("telegram", "ID", Item(text="From chat", kind=Kind.NOTE, created_at=NOW))
    assert created is True
    assert len(store.list()) == 2


def test_an_inbound_message_can_be_claimed_once(store):
    item = reminder(store)
    assert store.is_claimed("email", "<m1@example.com>") is False
    assert store.claim_inbound("email", "<m1@example.com>", item.id) is True
    assert store.is_claimed("email", "<m1@example.com>") is True
    assert store.claim_inbound("email", "<m1@example.com>", item.id) is False


# --- the scheduler lease ---------------------------------------------------------------------------


def test_a_free_lease_is_granted(store):
    assert store.lease_holder(NOW) is None
    assert store.acquire_lease("a", NOW, NOW + timedelta(minutes=1)) == (True, "a")
    assert store.lease_holder(NOW) == "a"


def test_a_held_lease_is_refused_to_everyone_else(store):
    store.acquire_lease("a", NOW, NOW + timedelta(minutes=1))
    assert store.acquire_lease("b", NOW, NOW + timedelta(minutes=1)) == (False, "a")
    assert store.lease_holder(NOW) == "a"


def test_the_holder_can_renew_its_own_lease(store):
    store.acquire_lease("a", NOW, NOW + timedelta(minutes=1))
    assert store.acquire_lease("a", NOW, NOW + timedelta(minutes=5)) == (True, "a")
    assert store.lease_holder(NOW + timedelta(minutes=2)) == "a"


def test_an_expired_lease_is_nobodys_and_can_be_taken(store):
    store.acquire_lease("a", NOW, NOW + timedelta(minutes=1))
    expired = NOW + timedelta(minutes=2)
    assert store.lease_holder(expired) is None
    assert store.acquire_lease("b", expired, expired + timedelta(minutes=1)) == (True, "b")


def test_releasing_hands_the_lease_over_at_once(store):
    store.acquire_lease("a", NOW, NOW + timedelta(minutes=10))
    store.release_lease("a")
    assert store.lease_holder(NOW) is None
    assert store.acquire_lease("b", NOW, NOW + timedelta(minutes=1)) == (True, "b")


def test_only_the_holder_can_release_the_lease(store):
    store.acquire_lease("a", NOW, NOW + timedelta(minutes=10))
    store.release_lease("b")
    assert store.lease_holder(NOW) == "a"


# --- lifecycle --------------------------------------------------------------------------------------


def test_a_new_database_is_migrated_to_the_current_schema(store):
    assert store.schema_version > 0


def test_a_store_says_which_backend_it_is_and_where_its_data_lives(store):
    """`maestro doctor`, `/health` and `/metrics` report these, so every backend must answer."""
    # The name is the URL scheme that opens it, which is what makes it useful in a metric label.
    assert parse_url(f"{store.backend}://wherever").backend == store.backend
    where = store.describe()
    assert where and where == store.describe()  # stable, so doctor and /health agree


def test_closing_is_idempotent_and_visible(store):
    assert store.closed is False
    store.close()
    assert store.closed is True
    store.close()
