"""#56: property-based tests. Hypothesis generates inputs (including DST edge cases and arbitrary text)
and checks invariants that hand-picked examples can miss."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from maestro import recurrence
from maestro.models import Item, Kind
from maestro.parser import parse
from maestro.store import Store
from maestro.tz import add_elapsed

# Zones chosen for awkward rules: DST in both hemispheres, a 30-minute DST shift (Lord Howe),
# a 45-minute offset (Chatham), no DST at all (Kolkata).
ZONES = [ZoneInfo(z) for z in ("UTC", "America/New_York", "Europe/London", "Asia/Kolkata",
                               "Australia/Lord_Howe", "Pacific/Chatham", "America/Sao_Paulo")]

zones = st.sampled_from(ZONES)
instants = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 12, 31))
times = st.times().map(lambda t: t.replace(second=0, microsecond=0))
rules = st.one_of(
    st.just("daily"),
    st.just("weekdays"),
    st.integers(0, 6).map(lambda d: f"weekly:{d}"),
    st.integers(1, 31).map(lambda d: f"monthly:{d}"),
    st.integers(1, 60 * 24 * 3).map(lambda m: f"interval:{m}"),
    st.integers(2, 30).map(lambda n: f"days:{n}"),
    st.tuples(st.integers(2, 8), st.integers(0, 6)).map(lambda p: f"weeks:{p[0]}:{p[1]}"),
)


def aware(naive: datetime, tz: ZoneInfo) -> datetime:
    return naive.replace(tzinfo=tz)


def exists(dt: datetime) -> bool:
    """False for wall-clock times skipped by a DST jump (e.g. 02:30 on a spring-forward night)."""
    return dt.astimezone(UTC).astimezone(dt.tzinfo).replace(tzinfo=None) == dt.replace(tzinfo=None)


@given(rules, instants, times, zones)
@settings(max_examples=400)
def test_next_occurrence_is_strictly_later(rule, after, at, tz):
    after = aware(after, tz)
    assert recurrence.next_occurrence(rule, after, at) > after


@given(rules.filter(lambda r: not r.startswith("interval")), instants, times, zones)
@settings(max_examples=400)
def test_calendar_rules_keep_their_wall_clock_time(rule, after, at, tz):
    nxt = recurrence.next_occurrence(rule, aware(after, tz), at)
    assert nxt.time() == at
    assert nxt.tzinfo is tz


@given(rules.filter(lambda r: not r.startswith("interval")), instants, times, zones)
@settings(max_examples=400)
def test_calendar_rules_land_on_matching_days(rule, after, at, tz):
    nxt = recurrence.next_occurrence(rule, aware(after, tz), at)
    kind, _, arg = rule.partition(":")
    if kind == "weekdays":
        assert nxt.weekday() < 5
    elif kind == "weekly":
        assert nxt.weekday() == int(arg)
    elif kind == "monthly":
        last_day = (date(nxt.year + nxt.month // 12, nxt.month % 12 + 1, 1) - timedelta(days=1)).day
        assert nxt.day == min(int(arg), last_day)


@given(st.integers(2, 30), instants, st.integers(0, 400), times, zones)
@settings(max_examples=300)
def test_every_n_days_stays_on_its_grid_after_any_outage(n, anchor, outage_days, at, tz):
    anchor = aware(datetime.combine(anchor.date(), at), tz)
    after = anchor + timedelta(days=outage_days, hours=1)
    nxt = recurrence.next_occurrence(f"days:{n}", after, at, anchor=anchor)
    assert nxt > after
    assert (nxt.date() - anchor.date()).days % n == 0
    assert nxt - after <= timedelta(days=n + 1)  # skips ahead, never further than one period


@given(st.integers(2, 8), st.integers(0, 6), instants, st.integers(0, 120), times, zones)
@settings(max_examples=300)
def test_every_n_weeks_keeps_its_weekday_and_parity(n, weekday, anchor, outage_weeks, at, tz):
    first = recurrence.next_occurrence(f"weekly:{weekday}", aware(anchor, tz), at)
    after = first + timedelta(weeks=outage_weeks, days=1)
    nxt = recurrence.next_occurrence(f"weeks:{n}:{weekday}", after, at, anchor=first)
    assert nxt > after and nxt.weekday() == weekday
    assert (nxt.date() - first.date()).days % (7 * n) == 0


@given(instants, st.integers(1, 60 * 24 * 7), zones)
@settings(max_examples=300)
def test_elapsed_time_is_exact_across_dst(start, minutes, tz):
    start = aware(start, tz)
    assume(exists(start))
    end = add_elapsed(start, timedelta(minutes=minutes))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=minutes)
    assert end.tzinfo is tz


@given(instants, zones, zones)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_storage_round_trips_any_instant_in_any_zone(when, write_zone, read_zone):
    due = aware(when.replace(microsecond=0), write_zone)
    assume(exists(due))
    with Store(tz=write_zone) as store:
        item = store.add(Item(text="x", kind=Kind.REMINDER, created_at=due, due_at=due))
        store.tz = read_zone  # same database, read by a user in another zone
        loaded = store.get(item.id)
    assert loaded.due_at == due  # same instant
    assert loaded.due_at.tzinfo is read_zone


@given(st.text(max_size=200), instants, zones)
@settings(max_examples=500)
def test_parse_never_crashes_and_keeps_the_text(source, now, tz):
    assume(source.strip())
    item = parse(source, now=aware(now, tz), tz=tz)
    assert item.text  # never empty
    assert item.source_text == source
    assert (item.kind == Kind.REMINDER) == (item.due_at is not None)
    if item.due_at is not None:
        assert item.due_at.tzinfo is not None


@given(st.sampled_from(["remind me to call mom", "pay rent", "water the plants", "dentist"]),
       st.sampled_from(["tomorrow at 5pm", "in 2 hours", "next friday at 10am", "on the 5th",
                        "every day at 8am", "every other friday at 9am", "this weekend", "at noon"]),
       instants, zones)
@settings(max_examples=300)
def test_parsed_reminders_are_in_the_future(task, when, now, tz):
    now = aware(now, tz)
    assume(exists(now))
    item = parse(f"{task} {when}", now=now, tz=tz)
    assert item.due_at is not None and item.due_at > now


def test_wall_clock_check_flags_nonexistent_times():
    # Sanity check for the helper above: 02:30 didn't exist in New York on 2026-03-08.
    assert not exists(datetime.combine(date(2026, 3, 8), time(2, 30), tzinfo=ZoneInfo("America/New_York")))
    assert exists(datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/New_York")))
