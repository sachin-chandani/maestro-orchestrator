from urllib.parse import urlencode

import pytest

from maestro.config import Config
from maestro.observability import METRICS
from maestro.ratelimit import RateLimits, TokenBucket
from maestro.sms import WEBHOOK_PATH, SmsHandler, twilio_signature
from maestro.store import Store


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_bucket_spends_and_refills():
    clock = Clock()
    bucket = TokenBucket(3, period=60, clock=clock)
    assert [bucket.take("a") for _ in range(4)] == [True, True, True, False]
    assert bucket.retry_after("a") == 20  # one token per 20 s
    clock.t = 20
    assert bucket.take("a") and not bucket.take("a")
    assert bucket.take("b")  # keys are independent


def test_bucket_never_exceeds_capacity():
    clock = Clock()
    bucket = TokenBucket(2, clock=clock)
    clock.t = 10_000
    assert [bucket.take("a") for _ in range(3)] == [True, True, False]


def test_bucket_memory_is_bounded():
    bucket = TokenBucket(1, max_keys=100, clock=Clock())
    for n in range(1_000):
        bucket.take(f"10.0.{n // 256}.{n % 256}")
    assert len(bucket._buckets) == 100


@pytest.mark.parametrize("trust, forwarded, expected", [
    (False, "1.2.3.4", "10.0.0.9"),            # headers ignored unless trusted
    (True, "1.2.3.4", "1.2.3.4"),
    (True, "6.6.6.6, 1.2.3.4", "1.2.3.4"),     # spoofed first entry ignored; the proxy's entry wins
    (True, None, "10.0.0.9"),
])
def test_client_address(trust, forwarded, expected):
    assert RateLimits(trust_proxy_headers=trust).client("10.0.0.9", forwarded) == expected


def test_config_parsing():
    config = Config.from_env({"MAESTRO_RATE_LIMIT_WEBHOOK": "0", "MAESTRO_TRUST_PROXY_HEADERS": "true"})
    assert config.rate_limit_webhook == 0
    assert config.rate_limit_auth_failures == 10
    assert config.trust_proxy_headers is True
    with pytest.raises(ValueError, match="MAESTRO_RATE_LIMIT_WEBHOOK must be a whole number"):
        Config.from_env({"MAESTRO_RATE_LIMIT_WEBHOOK": "lots"})


def test_cli_reports_bad_limits_as_config_errors(tmp_path, monkeypatch, capsys):
    from maestro.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAESTRO_RATE_LIMIT_AUTH_FAILURES", "-1")
    assert main(["--db", str(tmp_path / "m.db"), "list"]) == 2
    assert "MAESTRO_RATE_LIMIT_AUTH_FAILURES must be a whole number" in capsys.readouterr().err


# --- through the HTTP server ---------------------------------------------------------------

TOKEN, OWNER, PUBLIC = "s3cr3t", "+15552223333", "https://maestro.example.com"


def client(limits, api_token="api"):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app

    store = Store()
    return TestClient(create_app(store, api_token=api_token, sms=SmsHandler(store, TOKEN, OWNER),
                                 public_url=PUBLIC, limits=limits))


def signed_sms(c, body):
    params = {"From": OWNER, "To": "+15550001111", "Body": body}
    return c.post(WEBHOOK_PATH, content=urlencode(params),
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "X-Twilio-Signature": twilio_signature(TOKEN, PUBLIC + WEBHOOK_PATH, params)})


def test_webhooks_are_limited_per_client():
    with client(RateLimits(webhook_per_minute=3)) as c:
        assert [signed_sms(c, f"note {n}").status_code for n in range(4)] == [200, 200, 200, 429]
        res = signed_sms(c, "one more")
        assert res.status_code == 429 and int(res.headers["Retry-After"]) >= 1
        # The authenticated API is not affected by the webhook limit.
        assert c.get("/items", headers={"Authorization": "Bearer api"}).status_code == 200
    assert METRICS.get("maestro_rate_limited_total", reason="webhook") == 2


def test_repeated_auth_failures_lock_the_client_out():
    with client(RateLimits(auth_failures_per_minute=3)) as c:
        bad = {"Authorization": "Bearer guess"}
        assert [c.get("/items", headers=bad).status_code for _ in range(3)] == [401, 401, 401]
        # Locked out even with the right token, so the token can't be brute-forced.
        assert c.get("/items", headers={"Authorization": "Bearer api"}).status_code == 429
        assert c.get("/health").status_code == 429
    assert METRICS.get("maestro_rate_limited_total", reason="auth_failures") == 2


def test_bad_webhook_signatures_count_as_auth_failures():
    with client(RateLimits(auth_failures_per_minute=2)) as c:
        forged = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "x="}
        assert [c.post(WEBHOOK_PATH, content="Body=hi", headers=forged).status_code for _ in range(3)] \
            == [403, 403, 429]


def test_successful_requests_are_not_limited():
    with client(RateLimits(webhook_per_minute=1, auth_failures_per_minute=1)) as c:
        ok = {"Authorization": "Bearer api"}
        assert all(c.get("/items", headers=ok).status_code == 200 for _ in range(50))


def test_limits_can_be_turned_off():
    with client(RateLimits(webhook_per_minute=0, auth_failures_per_minute=0)) as c:
        assert all(signed_sms(c, f"note {n}").status_code == 200 for n in range(20))
        assert all(c.get("/items").status_code == 401 for _ in range(20))
