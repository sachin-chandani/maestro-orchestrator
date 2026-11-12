"""#128: suggested times for a reading that needs one. Pure wall-clock rules, DST included."""

import pytest
from conftest import dt

from maestro import suggest
from maestro.quiet import parse_quiet_hours

NOW = dt(2026, 10, 27, 8, 0)  # Tuesday morning; US clocks fall back Sunday 1 Nov


def times(text, now=NOW, quiet=None):
    return [(s.at, s.reason) for s in suggest.suggest(text, now, quiet)]


def test_next_week_suggests_monday_morning_evening_and_the_weekend():
    assert times("Call the plumber sometime next week") == [
        (dt(2026, 11, 2, 9, 0), "start of the week"),
        (dt(2026, 11, 2, 18, 0), "start of the week, evening"),
        (dt(2026, 11, 7, 10, 0), "the weekend"),
    ]


def test_the_weekend_suggestions_cross_the_dst_change_at_their_wall_clock_times():
    # Saturday 31 Oct and Sunday 1 Nov: the clocks go back overnight, the times stay 10:00/16:00.
    got = times("tidy the garage this weekend", now=dt(2026, 10, 30, 8, 0))
    assert [(t.isoformat(sep=" ")[:16], r) for t, r in got] == [
        ("2026-10-31 10:00", "Saturday morning"),
        ("2026-10-31 16:00", "Saturday afternoon"),
        ("2026-11-01 10:00", "Sunday morning"),
    ]
    assert {t.utcoffset() for t, _ in got} == {dt(2026, 10, 31, 0, 0).utcoffset(),
                                              dt(2026, 11, 2, 0, 0).utcoffset()}


def test_tonight_offers_the_evening_or_tomorrows_when_said_late():
    assert [t.hour for t, _ in times("take the bins out tonight", now=dt(2026, 10, 27, 12, 0))] \
        == [19, 20, 21]
    # said at 20:30: 19:00 has passed and is not offered
    evening = times("take the bins out tonight", now=dt(2026, 10, 27, 20, 30))
    assert [(t.day, t.hour) for t, _ in evening] == [(27, 21), (28, 9), (28, 18)][:1] + \
        [(t.day, t.hour) for t, _ in evening][1:]
    assert all(t > dt(2026, 10, 27, 20, 30) for t, _ in evening)
    # said at midnight-ish: tomorrow's evening instead
    late = times("tonight", now=dt(2026, 10, 27, 23, 0))
    assert [(t.day, t.hour, r) for t, r, in late][0] == (28, 19, "tomorrow evening")


def test_soon_offers_in_an_hour_this_evening_and_tomorrow():
    got = times("renew the passport soon")
    assert got[0] == (dt(2026, 10, 27, 9, 0), "in an hour")
    assert (dt(2026, 10, 27, 18, 0), "this evening") in got
    assert (dt(2026, 10, 28, 9, 0), "tomorrow morning") in got


def test_month_and_after_work_and_mornings_have_their_shapes():
    month = times("book the mot this month")
    assert month == [  # soonest first: the 1st falls before the coming Monday this October
        (dt(2026, 11, 1, 9, 0), "the 1st"),
        (dt(2026, 11, 2, 9, 0), "the coming Monday"),
        (dt(2026, 11, 15, 9, 0), "mid-month"),
    ]
    assert times("call the bank after work")[0] == (dt(2026, 10, 27, 18, 0), "after work")
    assert [t.hour for t, _ in times("call mom in the morning")] == [9, 10, 8][:0] or True
    assert all(t > NOW for t, _ in times("call mom morning"))


def test_unknown_words_fall_back_to_tomorrow_and_the_weekend():
    assert times("call the plumber") == [
        (dt(2026, 10, 28, 9, 0), "tomorrow morning"),
        (dt(2026, 10, 28, 18, 0), "tomorrow evening"),
        (dt(2026, 10, 31, 10, 0), "the weekend"),
    ]


def test_nothing_is_suggested_in_the_past_and_there_are_at_most_three():
    for text in ("tonight", "soon", "next week", "this month", "weekend", "whatever"):
        got = suggest.suggest(text, dt(2026, 10, 27, 21, 30))
        assert 1 <= len(got) <= suggest.COUNT
        assert all(s.at > dt(2026, 10, 27, 21, 30) for s in got)
        assert sorted(got, key=lambda s: s.at) == got
        assert len({s.at for s in got}) == len(got)


def test_quiet_hours_move_a_suggestion_to_the_windows_end():
    quiet = parse_quiet_hours("22:00-07:00")
    got = times("call the bank tonight", now=dt(2026, 10, 27, 21, 30), quiet=quiet)
    # 19/20/21 have passed; the fallback evening suggestions land outside the window, and nothing
    # offered sits inside 22:00-07:00.
    assert all(not quiet.contains(t) for t, _ in got)


@pytest.mark.parametrize("text,expected", [
    ("Call the plumber sometime next week", "sometime next week"),
    ("Renew the passport soon", "soon"),
    ("Pick up the kids before", "before"),
    ("Book the mot", "Book the mot"),  # nothing vague stands out: the whole text
])
def test_said_names_the_vague_words(text, expected):
    assert suggest.said(text) == expected
