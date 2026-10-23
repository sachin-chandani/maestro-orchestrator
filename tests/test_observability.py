import json
import logging
from datetime import timedelta

import pytest
from conftest import dt

from maestro.models import Item, Kind
from maestro.notifier import MultiNotifier
from maestro.observability import METRICS, JsonFormatter, Metrics, configure_logging
from maestro.scheduler import MAX_ATTEMPTS, fire_due
from maestro.store import Store

DUE = dt(2026, 10, 23, 9, 0)


class Flaky:
    def __init__(self, failures):
        self.failures = failures

    def send(self, item):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("down")


def test_scheduler_counts_deliveries_fires_and_lag():
    store = Store()
    store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=DUE, due_at=DUE))
    notifier = MultiNotifier({"push": Flaky(0), "sms": Flaky(1)})

    fire_due(store, notifier, now=DUE + timedelta(seconds=90))
    assert METRICS.get("maestro_scheduler_lag_seconds") == 90
    assert METRICS.get("maestro_deliveries_total", channel="push", result="ok") == 1
    assert METRICS.get("maestro_deliveries_total", channel="sms", result="error") == 1
    assert METRICS.get("maestro_reminders_fired_total") == 0

    fire_due(store, notifier, now=DUE + timedelta(minutes=5))
    assert METRICS.get("maestro_deliveries_total", channel="sms", result="ok") == 1
    assert METRICS.get("maestro_reminders_fired_total") == 1
    assert METRICS.get("maestro_scheduler_ticks_total") == 2

    fire_due(store, notifier, now=DUE + timedelta(minutes=6))
    assert METRICS.get("maestro_scheduler_lag_seconds") == 0  # nothing due


def test_gave_up_is_counted():
    store = Store()
    store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=DUE, due_at=DUE))
    notifier = MultiNotifier({"sms": Flaky(999)})
    now = DUE
    for _ in range(MAX_ATTEMPTS):
        fire_due(store, notifier, now=now)
        now += timedelta(hours=1)
    assert METRICS.get("maestro_deliveries_total", channel="sms", result="error") == MAX_ATTEMPTS - 1
    assert METRICS.get("maestro_deliveries_total", channel="sms", result="gave_up") == 1


def test_render_prometheus_text_format():
    m = Metrics()
    m.describe("demo_total", "counter", "A demo.")
    m.inc("demo_total", channel='we"ird\\name')
    m.inc("demo_total", 2, channel="b")
    m.set("lonely", 1.5)
    text = m.render(extra={"demo_items": {(("kind", "note"),): 3}})
    assert text.splitlines() == [
        "# HELP demo_items ",
        "# TYPE demo_items untyped",
        'demo_items{kind="note"} 3',
        "# HELP demo_total A demo.",
        "# TYPE demo_total counter",
        'demo_total{channel="b"} 2',
        'demo_total{channel="we\\"ird\\\\name"} 1',
        "# HELP lonely ",
        "# TYPE lonely untyped",
        "lonely 1.5",
    ]


def test_render_keeps_full_precision():
    m = Metrics()
    m.set("ts", 1792784439.372)
    m.set("big", 1792784439.0)
    assert "ts 1792784439.372" in m.render()
    assert "big 1792784439\n" in m.render()


def test_metrics_endpoint():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(Store(), api_token="t")) as client:
        assert client.get("/metrics").status_code == 401
        client.post("/items", json={"text": "call mom tomorrow at 5pm"}, headers=auth)
        client.post("/items", json={"text": "remember the gate code is 4321"}, headers=auth)
        res = client.get("/metrics", headers=auth)
    assert res.headers["content-type"].startswith("text/plain")
    assert 'maestro_captures_total{source="api"} 2' in res.text
    assert 'maestro_items{kind="reminder",status="pending"} 1' in res.text
    assert 'maestro_items{kind="note",status="pending"} 1' in res.text
    assert "4321" not in res.text


def test_json_log_lines():
    record = logging.LogRecord("maestro.scheduler", logging.WARNING, __file__, 1,
                               "Reminder #%s failed", (3,), None)
    entry = json.loads(JsonFormatter().format(record))
    assert entry["level"] == "warning"
    assert entry["logger"] == "maestro.scheduler"
    assert entry["msg"] == "Reminder #3 failed"
    assert entry["ts"].endswith("+00:00")

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    assert "RuntimeError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_configure_logging_validates_format():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("json", "debug")
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.DEBUG
        with pytest.raises(ValueError, match="MAESTRO_LOG_FORMAT"):
            configure_logging("xml")
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])
