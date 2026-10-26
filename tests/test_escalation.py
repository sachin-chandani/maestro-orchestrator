"""#12: unacknowledged reminders escalate to louder channels."""

from datetime import timedelta

import pytest
from conftest import dt

from maestro.config import Config
from maestro.escalation import build_stages, parse_spec
from maestro.models import Item, Kind
from maestro.notifier import CHANNELS, ConfigError, MultiNotifier
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.scheduler import Lease, fire_due
from maestro.store import Store

DUE = dt(2026, 10, 27, 9, 0)


class Recorder:
    def __init__(self, failures=0):
        self.sent = []
        self.failures = failures

    def send(self, item):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("provider down")
        self.sent.append(item.text)


@pytest.fixture
def channels(monkeypatch):
    """Fake 'sms' and 'call' channels registered like real ones."""
    fakes = {"sms": Recorder(), "call": Recorder()}
    for name, fake in fakes.items():
        monkeypatch.setitem(CHANNELS, name, lambda _config, fake=fake: fake)
    return fakes


def stages(spec):
    return build_stages(Config(escalation=spec))


def tick(store, push, now, spec="10m=sms, 30m=call+sms", **kw):
    return fire_due(store, MultiNotifier({"push": push}), now=now, escalation=stages(spec), **kw)


def reminder(store, text="Take pills", due=DUE):
    return store.add(Item(text=text, kind=Kind.REMINDER, created_at=due - timedelta(days=1), due_at=due))


@pytest.mark.parametrize("spec, expected", [
    ("10m=twilio_sms", [(timedelta(minutes=10), ["twilio_sms"])]),
    (" 10m = twilio_sms , 1h=telegram+twilio_sms ", [(timedelta(minutes=10), ["twilio_sms"]),
                                                    (timedelta(hours=1), ["telegram", "twilio_sms"])]),
    ("", []),
])
def test_parse_spec(spec, expected):
    assert [(d, [c.strip() for c in cs]) for d, cs in parse_spec(spec)] == expected


@pytest.mark.parametrize("spec, error", [
    ("twilio_sms", "should look like 10m=twilio_sms"),
    ("10m=", "should look like"),
    ("soon=sms", "Can't read duration"),
    ("30m=sms, 10m=call", "increasing"),
    ("10m=sms, 10m=call", "increasing"),
    ("30s=sms", "Can't read duration"),
    ("0m=sms", "at least 1 minute"),
    ("13h=sms", "under 12:00:00"),
])
def test_parse_spec_errors(spec, error):
    with pytest.raises(ConfigError, match=error):
        parse_spec(spec)


def test_build_stages_checks_channels():
    with pytest.raises(ConfigError, match="unknown channel 'pager'"):
        stages("10m=pager")
    with pytest.raises(ConfigError, match="twilio_sms channel needs"):
        stages("10m=twilio_sms")  # known channel, but not configured


def test_stages_fire_in_order_once_each(channels):
    store = Store()
    reminder(store)
    push = Recorder()
    tick(store, push, DUE)  # first notice
    tick(store, push, DUE + timedelta(minutes=9))
    assert channels["sms"].sent == []

    tick(store, push, DUE + timedelta(minutes=10))
    tick(store, push, DUE + timedelta(minutes=11))
    assert channels["sms"].sent == ["Still waiting: Take pills"]  # once

    tick(store, push, DUE + timedelta(minutes=30))
    tick(store, push, DUE + timedelta(minutes=45))
    assert channels["call"].sent == ["Still waiting: Take pills"]
    assert channels["sms"].sent == ["Still waiting: Take pills"] * 2  # stage 2 sends SMS again, once
    assert push.sent == ["Take pills"]
    assert METRICS.get("maestro_escalations_total", stage="1") == 1
    assert METRICS.get("maestro_escalations_total", stage="2") == 1


def test_acknowledging_stops_escalation(channels):
    store = Store()
    item = reminder(store)
    tick(store, Recorder(), DUE)
    store.snooze(item.id, DUE + timedelta(hours=2), via="api", now=DUE + timedelta(minutes=5))
    tick(store, Recorder(), DUE + timedelta(minutes=40))
    assert channels["sms"].sent == [] and channels["call"].sent == []


def test_recurring_reminders_escalate_the_delivered_occurrence(channels):
    store = Store()
    store.add(parse("take vitamins every day at 8am", now=dt(2026, 10, 27, 7, 0)))
    tick(store, Recorder(), dt(2026, 10, 27, 8, 0))  # fires, then moves on to tomorrow
    tick(store, Recorder(), dt(2026, 10, 27, 8, 10))
    assert channels["sms"].sent == ["Still waiting: Take vitamins"]


def test_old_reminders_never_escalate(channels):
    # Turning escalation on must not page about reminders from days ago.
    store = Store()
    reminder(store, due=dt(2026, 10, 24, 9, 0))
    fire_due(store, MultiNotifier({"push": Recorder()}), now=dt(2026, 10, 24, 9, 0))  # no escalation then
    tick(store, Recorder(), dt(2026, 10, 27, 9, 0))
    assert channels["sms"].sent == []


def test_a_failing_stage_is_retried_without_blocking_others(channels, monkeypatch):
    flaky = Recorder(failures=1)
    monkeypatch.setitem(CHANNELS, "sms", lambda _config: flaky)
    store = Store()
    reminder(store, "A")
    reminder(store, "B")
    tick(store, Recorder(), DUE)
    tick(store, Recorder(), DUE + timedelta(minutes=10))
    assert flaky.sent == ["Still waiting: B"]  # A failed once, B still went out
    tick(store, Recorder(), DUE + timedelta(minutes=12))  # after A's 1-minute backoff
    assert sorted(flaky.sent) == ["Still waiting: A", "Still waiting: B"]


def test_replying_done_to_an_escalation_sms_acts_on_that_reminder(channels, monkeypatch):
    from maestro.sms import SmsHandler

    monkeypatch.setitem(CHANNELS, "twilio_sms", lambda _config: channels["sms"])
    store = Store()
    item = reminder(store)
    fire_due(store, MultiNotifier({"push": Recorder()}), now=DUE)
    fire_due(store, MultiNotifier({"push": Recorder()}), now=DUE + timedelta(minutes=10),
             escalation=build_stages(Config(escalation="10m=twilio_sms")))
    reply = SmsHandler(store, "t", "+15550001111").handle({"From": "+15550001111", "Body": "done"})
    assert "Done: Take pills" in reply
    assert store.get(item.id).status == "done"


def test_standby_schedulers_do_not_escalate(channels, tmp_path):
    path = tmp_path / "m.db"
    with Store(path) as leader_store:
        reminder(leader_store)
        leader = Lease(owner="leader", clock=lambda: DUE, ttl=timedelta(hours=1))  # still alive at +10m
        tick(leader_store, Recorder(), DUE, lease=leader)
    with Store(path) as standby_store:
        standby = Lease(owner="standby", clock=lambda: DUE + timedelta(minutes=10))
        tick(standby_store, Recorder(), DUE + timedelta(minutes=10), lease=standby)
    assert channels["sms"].sent == []


def test_doctor_shows_the_plan(channels, tmp_path):
    from maestro import doctor

    lines = []
    doctor.run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                      escalation="10m=sms, 30m=call+sms"), out=lines.append)
    assert "✓ escalation: after 10m: sms; after 30m: call, sms" in lines
    lines = []
    doctor.run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                      escalation="10m=pager"), out=lines.append)
    assert any(line.startswith("✗ escalation: MAESTRO_ESCALATION: unknown channel 'pager'") for line in lines)


def test_stages_count_from_the_first_notice_not_the_due_time(channels):
    # The first notice went out 40 minutes late (downtime): stages still wait their delays after it.
    store = Store()
    reminder(store)
    late = DUE + timedelta(minutes=40)
    tick(store, Recorder(), late)
    assert channels["sms"].sent == [] and channels["call"].sent == []  # not everything at once
    tick(store, Recorder(), late + timedelta(minutes=10))
    assert channels["sms"].sent == ["Still waiting: Take pills"] and channels["call"].sent == []


def test_cron_run_once_escalates(channels, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from maestro.cli import main

    monkeypatch.setitem(CHANNELS, "console", lambda _config: Recorder())
    path = tmp_path / "m.db"
    now = datetime.now(UTC).replace(microsecond=0)
    with Store(path) as store:
        reminder(store, due=now - timedelta(minutes=20))
        fire_due(store, MultiNotifier({"push": Recorder()}), now=now - timedelta(minutes=15))
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_ESCALATION", "10m=sms")
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(path), "run", "--once"]) == 0
    assert channels["sms"].sent == ["Still waiting: Take pills"]


def test_escalation_stops_when_the_lease_is_lost(channels, tmp_path):
    path = tmp_path / "m.db"
    store = Store(path)
    for n in range(3):
        reminder(store, f"R{n}")
    tick(store, Recorder(), DUE)
    calls = {"n": 0}
    leader = Lease(owner="a", clock=lambda: DUE + timedelta(minutes=10), ttl=timedelta(hours=1))
    real = leader.acquire

    def acquire(store_, now):
        calls["n"] += 1
        return real(store_, now) if calls["n"] <= 3 else False  # lost after the first reminder

    leader.acquire = acquire
    fire_due(store, MultiNotifier({"push": Recorder()}), now=DUE + timedelta(minutes=10), lease=leader,
             escalation=stages("10m=sms"))
    assert channels["sms"].sent == ["Still waiting: R0"]


def test_one_broken_item_does_not_stop_escalation_for_others(channels, monkeypatch):
    store = Store()
    reminder(store, "A")
    reminder(store, "B")
    tick(store, Recorder(), DUE)
    real = store.deliveries

    def broken_for_a(item_id, occurrence):
        if item_id == 1:
            raise RuntimeError("corrupt row")
        return real(item_id, occurrence)

    monkeypatch.setattr(store, "deliveries", broken_for_a)
    tick(store, Recorder(), DUE + timedelta(minutes=10), spec="10m=sms")
    assert channels["sms"].sent == ["Still waiting: B"]


def test_reply_matching_is_exact_per_channel():
    store = Store()
    item = reminder(store)
    store.record_delivery(item.id, DUE, _delivered("escalation1:twilioXsms"))
    assert store.last_delivered("twilio_sms") is None  # '_' is not a wildcard
    store.record_delivery(item.id, DUE, _delivered("escalation12:twilio_sms"))
    assert store.last_delivered("twilio_sms") == item.id


def _delivered(channel):
    from maestro.models import Delivery

    return Delivery(channel=channel, attempts=1, delivered_at=DUE)


def test_a_channel_listed_twice_in_a_stage_is_an_error():
    with pytest.raises(ConfigError, match="lists a channel twice"):
        parse_spec("10m=sms+sms")


def test_doctor_send_test_covers_escalation_only_channels(channels, tmp_path):
    from maestro import doctor

    lines = []
    doctor.run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                      escalation="10m=sms"), send_test=True, out=lines.append)
    assert "✓ send sms: delivered" in lines
