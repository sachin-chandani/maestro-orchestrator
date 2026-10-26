"""#18: the generic webhook channel (Slack, Discord, Home Assistant, anything)."""

import hashlib
import hmac
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from maestro.config import Config
from maestro.models import Item, Kind
from maestro.notifier import CHANNELS, ConfigError, MultiNotifier, WebhookNotifier
from maestro.scheduler import fire_due
from maestro.store import Store

DUE = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
ITEM = Item(id=7, text="Call mom", kind=Kind.REMINDER, created_at=DUE, due_at=DUE, recurrence="daily")


class Recorder:
    def __init__(self, fail_with=None):
        self.requests = []
        self.fail_with = fail_with

    def __call__(self, request):
        self.requests.append(request)
        if self.fail_with:
            raise self.fail_with
        return 200


def test_json_payload():
    opener = Recorder()
    WebhookNotifier("https://hooks.example.com/maestro", opener=opener).send(ITEM)
    (request,) = opener.requests
    assert request.get_method() == "POST" and request.full_url == "https://hooks.example.com/maestro"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {
        "event": "reminder.due", "id": 7, "text": "Call mom", "due_at": "2026-10-27T09:00:00+00:00",
        "when": "Tue 27 Oct 09:00, daily", "recurrence": "daily",
    }
    assert request.get_header("X-maestro-signature") is None  # no secret configured


@pytest.mark.parametrize("fmt, expected", [
    ("slack", {"text": "⏰ Call mom"}), ("discord", {"content": "⏰ Call mom"}),
])
def test_service_formats(fmt, expected):
    opener = Recorder()
    WebhookNotifier("https://hooks.slack.com/x", fmt=fmt, opener=opener).send(ITEM)
    assert json.loads(opener.requests[0].data) == expected


def test_signature_can_be_verified_by_the_receiver():
    opener = Recorder()
    signed = WebhookNotifier("https://hooks.example.com/m", secret="s3cr3t", opener=opener,
                             clock=lambda: 1793091600)
    signed.send(ITEM)
    request = opener.requests[0]
    timestamp = request.get_header("X-maestro-timestamp")
    assert timestamp == "1793091600"
    # What a receiver does: recompute over "<timestamp>.<body>" and compare.
    expected = hmac.new(b"s3cr3t", f"{timestamp}.".encode() + request.data, hashlib.sha256).hexdigest()
    assert request.get_header("X-maestro-signature") == f"sha256={expected}"


def test_non_ascii_text_is_sent_as_utf8():
    opener = Recorder()
    item = Item(id=1, text="Café meeting ☕", kind=Kind.REMINDER, created_at=DUE, due_at=DUE)
    WebhookNotifier("https://hooks.example.com/m", opener=opener).send(item)
    assert json.loads(opener.requests[0].data.decode("utf-8"))["text"] == "Café meeting ☕"


def test_http_errors_are_retried_by_the_scheduler():
    error = urllib.error.HTTPError("https://hooks.example.com/m", 503, "Service Unavailable", {}, None)
    opener = Recorder(fail_with=error)
    store = Store()
    store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=DUE, due_at=DUE))
    webhook = WebhookNotifier("https://hooks.example.com/m", opener=opener)
    fire_due(store, MultiNotifier({"webhook": webhook}), now=DUE)
    state = store.deliveries(1, DUE)["webhook"]
    assert state.delivered_at is None and state.attempts == 1 and "503" in state.last_error
    assert state.next_attempt_at == DUE + timedelta(minutes=1)


@pytest.mark.parametrize("url", [
    "https://hooks.slack.com/services/T/B/x",
    "http://localhost:8123/api/webhook/maestro",
    "http://192.168.1.20:8123/api/webhook/maestro",
    "http://homeassistant.local:8123/api/webhook/maestro",
    "http://[::1]:8123/hook",
])
def test_accepted_urls(url):
    assert isinstance(CHANNELS["webhook"](Config(webhook_url=url)), WebhookNotifier)


@pytest.mark.parametrize("url, error", [
    ("http://hooks.example.com/m", "must use https unless it points at your own network"),
    ("http://8.8.8.8/m", "must use https"),
    ("ftp://example.com/m", r"must be an http\(s\) URL"),
    ("hooks.example.com/m", r"must be an http\(s\) URL"),
])
def test_rejected_urls(url, error):
    with pytest.raises(ConfigError, match=error):
        CHANNELS["webhook"](Config(webhook_url=url))


def test_config():
    with pytest.raises(ConfigError, match="webhook channel needs MAESTRO_WEBHOOK_URL"):
        CHANNELS["webhook"](Config())
    with pytest.raises(ConfigError, match="MAESTRO_WEBHOOK_FORMAT must be one of json, slack, discord"):
        CHANNELS["webhook"](Config(webhook_url="https://x.example.com", webhook_format="teams"))
    config = Config.from_env({"MAESTRO_WEBHOOK_URL": "https://x.example.com",
                              "MAESTRO_WEBHOOK_FORMAT": "Slack", "MAESTRO_WEBHOOK_SECRET": "s"})
    notifier = CHANNELS["webhook"](config)
    assert (notifier.format, notifier.secret) == ("slack", "s")
