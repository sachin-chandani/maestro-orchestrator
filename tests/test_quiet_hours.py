"""#13: quiet hours hold non-urgent reminders (and their escalation) until the window ends."""

from datetime import time, timedelta

import pytest
from conftest import dt

from maestro.config import Config
from maestro.escalation import build_stages
from maestro.models import Item, Kind
from maestro.notifier import CHANNELS, ConfigError, MultiNotifier
from maestro.observability import METRICS
from maestro.quiet import QuietHours, is_urgent, parse_quiet_hours
from maestro.scheduler import fire_due
from maestro.store import Store

NIGHT = QuietHours(time(22, 0), time(7, 0))


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item.text)


def reminder(store, text, due, recurrence=None):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=due - timedelta(days=1), due_at=due,
                          recurrence=recurrence))


def tick(store, push, now, quiet=NIGHT, **kw):
    return fire_due(store, MultiNotifier({"push": push}), now=now, quiet=quiet, **kw)


@pytest.mark.parametrize("spec, expected", [
    ("22:00-07:00", NIGHT),
    (" 9:30 - 17:00 ", QuietHours(time(9, 30), time(17, 0))),
    ("", None),
])
def test_parse(spec, expected):
    assert parse_quiet_hours(spec) == expected


@pytest.mark.parametrize("spec, error", [
    ("22:00", "should look like 22:00-07:00"),
    ("10pm-7am", "should look like"),
    ("24:00-07:00", "should look like"),
    ("22:00-22:00", "must differ"),
])
def test_parse_errors(spec, error):
    with pytest.raises(ConfigError, match=error):
        parse_quiet_hours(spec)


@pytest.mark.parametrize("hour, minute, inside", [
    (21, 59, False), (22, 0, True), (23, 30, True), (0, 0, True),
    (6, 59, True), (7, 0, False), (12, 0, False),
])
def test_window_wraps_past_midnight(hour, minute, inside):
    assert NIGHT.contains(dt(2026, 10, 27, hour, minute)) is inside


def test_same_day_window():
    lunch = QuietHours(time(13, 0), time(14, 0))
    assert lunch.contains(dt(2026, 10, 27, 13, 30)) and not lunch.contains(dt(2026, 10, 27, 14, 0))
    assert not lunch.contains(dt(2026, 10, 27, 23, 0))


def test_window_end():
    assert NIGHT.ends_after(dt(2026, 10, 27, 23, 0)) == dt(2026, 10, 28, 7, 0)
    assert NIGHT.ends_after(dt(2026, 10, 28, 3, 0)) == dt(2026, 10, 28, 7, 0)


@pytest.mark.parametrize("text, urgent", [
    ("!take insulin", True), ("  !call vet", True), ("Urgent: call the plumber", True),
    ("call the plumber, URGENT", True), ("read about insurgents", False), ("buy milk!", False),
])
def test_urgency(text, urgent):
    assert is_urgent(Item(text=text, kind=Kind.REMINDER, created_at=dt(2026, 10, 27, 9, 0))) is urgent


def test_reminders_due_overnight_wait_for_the_morning():
    store = Store()
    reminder(store, "Water the plants", dt(2026, 10, 27, 23, 0))
    push = Recorder()
    assert tick(store, push, dt(2026, 10, 27, 23, 0)) == []
    tick(store, push, dt(2026, 10, 28, 3, 0))
    assert push.sent == []
    assert METRICS.get("maestro_reminders_held") == 1
    assert METRICS.get("maestro_scheduler_lag_seconds") == 0  # holding isn't lag; nights don't page ops
    tick(store, push, dt(2026, 10, 28, 7, 0))
    assert push.sent == ["Water the plants"]
    assert METRICS.get("maestro_reminders_held") == 0


def test_urgent_reminders_ignore_quiet_hours():
    store = Store()
    reminder(store, "!Take insulin", dt(2026, 10, 27, 23, 0))
    push = Recorder()
    tick(store, push, dt(2026, 10, 27, 23, 0))
    assert push.sent == ["!Take insulin"]


def test_a_recurring_reminder_held_overnight_sends_one_notice():
    store = Store()
    item = reminder(store, "Stretch", dt(2026, 10, 27, 21, 0), recurrence="interval:60")
    push = Recorder()
    tick(store, push, dt(2026, 10, 27, 21, 0))
    for hour in range(22, 31):  # every hour through the night, until 06:00
        tick(store, push, dt(2026, 10, 27, 0, 0) + timedelta(hours=hour))
    tick(store, push, dt(2026, 10, 28, 7, 0))
    assert push.sent == ["Stretch", "Stretch"]  # 21:00, then once at 07:00
    assert store.get(item.id).due_at == dt(2026, 10, 28, 8, 0)


def test_without_quiet_hours_nothing_is_held():
    store = Store()
    reminder(store, "Water the plants", dt(2026, 10, 27, 23, 0))
    push = Recorder()
    tick(store, push, dt(2026, 10, 27, 23, 0), quiet=None)
    assert push.sent == ["Water the plants"]


@pytest.fixture
def sms(monkeypatch):
    fake = Recorder()
    monkeypatch.setitem(CHANNELS, "sms", lambda _config: fake)
    return fake


def test_escalation_waits_for_the_window_to_end(sms):
    store = Store()
    reminder(store, "Take pills", dt(2026, 10, 27, 21, 55))
    stages = build_stages(Config(escalation="10m=sms"))
    tick(store, Recorder(), dt(2026, 10, 27, 21, 55), escalation=stages)  # first notice before the window
    tick(store, Recorder(), dt(2026, 10, 27, 22, 5), escalation=stages)
    tick(store, Recorder(), dt(2026, 10, 28, 2, 0), escalation=stages)
    assert sms.sent == []
    tick(store, Recorder(), dt(2026, 10, 28, 7, 0), escalation=stages)
    assert sms.sent == ["Still waiting: Take pills"]


def test_urgent_reminders_escalate_overnight(sms):
    store = Store()
    reminder(store, "Take pills, urgent", dt(2026, 10, 27, 23, 0))
    stages = build_stages(Config(escalation="10m=sms"))
    tick(store, Recorder(), dt(2026, 10, 27, 23, 0), escalation=stages)
    tick(store, Recorder(), dt(2026, 10, 27, 23, 10), escalation=stages)
    assert sms.sent == ["Still waiting: Take pills, urgent"]


def test_doctor_reports_quiet_hours(tmp_path):
    from maestro import doctor

    def lines(spec):
        out = []
        doctor.run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                          quiet_hours=spec), out=out.append)
        return out

    assert "✓ quiet hours: 22:00-07:00: non-urgent reminders wait; start one with ! for urgent" in lines(
        "22:00-07:00")
    assert any(line.startswith("✗ quiet hours: MAESTRO_QUIET_HOURS '10pm' should look like")
               for line in lines("10pm"))
    assert not any("quiet hours" in line for line in lines(""))


def test_bad_quiet_hours_stop_the_scheduler_from_starting(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_QUIET_HOURS", "late")
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(tmp_path / "m.db"), "run", "--once"]) == 2
    assert "MAESTRO_QUIET_HOURS 'late'" in capsys.readouterr().err


def test_config_from_env():
    assert Config.from_env({"MAESTRO_QUIET_HOURS": " 22:00-07:00 "}).quiet_hours == "22:00-07:00"
