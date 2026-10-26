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
        "event": "reminder.due", "id": 7, "text": "Call mom", "occurrence_id": "7:1793091600",
        "due_at": "2026-10-27T09:00:00+00:00", "when": "Tue 27 Oct 09:00, daily", "recurrence": "daily",
    }
    assert request.get_header("X-maestro-signature") is None  # no secret configured


@pytest.mark.parametrize("fmt, expected", [
    ("slack", {"text": "⏰ Call mom"}),
    ("discord", {"content": "⏰ Call mom", "allowed_mentions": {"parse": []}}),
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


def test_mentions_and_markup_in_reminder_text_are_neutralised():
    text = "tell @everyone <!channel> a<b>c & d"
    item = Item(id=1, text=text, kind=Kind.REMINDER, created_at=DUE, due_at=DUE)
    slack, discord = Recorder(), Recorder()
    WebhookNotifier("https://x.example.com", fmt="slack", opener=slack).send(item)
    WebhookNotifier("https://x.example.com", fmt="discord", opener=discord).send(item)
    escaped = "⏰ tell @everyone &lt;!channel&gt; a&lt;b&gt;c &amp; d"
    assert json.loads(slack.requests[0].data)["text"] == escaped
    assert json.loads(discord.requests[0].data)["allowed_mentions"] == {"parse": []}


def test_long_reminders_fit_discords_limit():
    item = Item(id=1, text="x" * 2000, kind=Kind.REMINDER, created_at=DUE, due_at=DUE)
    opener = Recorder()
    WebhookNotifier("https://discord.com/api/webhooks/1/x", fmt="discord", opener=opener).send(item)
    assert len(json.loads(opener.requests[0].data)["content"]) == 2000


def test_redirects_are_refused_not_followed():
    import http.server
    import threading

    hits = []

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(("POST", self.path))
            self.send_response(301)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def do_GET(self):
            hits.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        notifier = CHANNELS["webhook"](Config(webhook_url=f"http://127.0.0.1:{server.server_port}/hook"))
        with pytest.raises(urllib.error.HTTPError, match="refusing to follow redirect"):
            notifier.send(ITEM)
    finally:
        server.shutdown()
        server.server_close()
    assert hits == [("POST", "/hook")]  # no silent GET to the redirect target


def test_errors_never_print_the_secret_url():
    with pytest.raises(ConfigError) as error:
        CHANNELS["webhook"](Config(webhook_url="hooks.slack.com/services/T000/B000/XXXXSECRET"))
    assert "XXXXSECRET" not in str(error.value)


@pytest.mark.parametrize("url, local", [
    ("http://homeassistant:8123/api/webhook/x", True),        # Docker Compose service name
    ("http://100.101.2.3:8123/api/webhook/x", True),          # Tailscale
    ("http://homeassistant.local.:8123/x", True),             # trailing dot
    ("http://[::ffff:8.8.8.8]/hook", False),                   # IPv4-mapped public address
    ("http://[::ffff:192.168.1.2]/hook", True),
])
def test_own_network_detection(url, local):
    if local:
        assert isinstance(CHANNELS["webhook"](Config(webhook_url=url)), WebhookNotifier)
    else:
        with pytest.raises(ConfigError, match="must use https"):
            CHANNELS["webhook"](Config(webhook_url=url))


def test_whitespace_around_the_url_is_ignored():
    assert Config.from_env({"MAESTRO_WEBHOOK_URL": "  https://x.example.com/h \n"}).webhook_url == \
        "https://x.example.com/h"
