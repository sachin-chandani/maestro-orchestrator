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


def test_lookups_do_not_create_buckets():
    bucket = TokenBucket(1, max_keys=2, clock=Clock())
    bucket.take("attacker")
    for n in range(100):
        assert not bucket.is_empty(f"visitor-{n}")
    assert list(bucket._buckets) == ["attacker"]  # still tracked, not evicted by lookups
    assert bucket.is_empty("attacker")


def test_bucket_memory_is_bounded():
    bucket = TokenBucket(1, max_keys=100, clock=Clock())
    for n in range(1_000):
        bucket.take(f"10.0.{n // 256}.{n % 256}")
    assert len(bucket._buckets) == 100


@pytest.mark.parametrize("trust, forwarded, expected", [
    (False, ["1.2.3.4"], "10.0.0.9"),             # headers ignored unless trusted
    (True, ["1.2.3.4"], "1.2.3.4"),
    (True, ["6.6.6.6, 1.2.3.4"], "1.2.3.4"),      # spoofed first entry ignored; the proxy's entry wins
    (True, ["6.6.6.6", "1.2.3.4"], "1.2.3.4"),    # proxy appended its own header line
    (True, [], "10.0.0.9"),
    (True, [" , "], "10.0.0.9"),
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


def forged_sms(c):
    return c.post(WEBHOOK_PATH, content="Body=hi",
                  headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "x="})


def test_forged_webhooks_are_limited_but_signed_ones_never_are():
    with client(RateLimits(webhook_per_minute=3)) as c:
        assert [forged_sms(c).status_code for _ in range(4)] == [403, 403, 403, 429]
        assert int(forged_sms(c).headers["Retry-After"]) >= 1
        # Genuine Twilio callbacks keep working: the owner's texts are never dropped.
        assert all(signed_sms(c, f"note {n}").status_code == 200 for n in range(20))
    assert METRICS.get("maestro_rate_limited_total", reason="webhook") == 2


def test_wrong_tokens_lock_the_client_out():
    with client(RateLimits(auth_failures_per_minute=3)) as c:
        bad = {"Authorization": "Bearer guess"}
        assert [c.get("/items", headers=bad).status_code for _ in range(4)] == [401, 401, 401, 429]
        # Locked out even with the right token, so the token can't be brute-forced.
        res = c.get("/items", headers={"Authorization": "Bearer api"})
        assert res.status_code == 429 and int(res.headers["Retry-After"]) >= 1
        # The SMS webhook keeps working: it has its own signature auth.
        assert signed_sms(c, "call mom at 5pm").status_code == 200


def test_missing_token_is_not_a_guess():
    # The web UI polls without a token until one is entered; that must not lock the owner out.
    with client(RateLimits(auth_failures_per_minute=2)) as c:
        assert all(c.get("/items").status_code == 401 for _ in range(30))
        assert c.get("/items", headers={"Authorization": "Bearer api"}).status_code == 200


def test_concurrent_guesses_cannot_exceed_the_limit():
    pytest.importorskip("fastapi")
    import asyncio

    import httpx

    from maestro.api import create_app

    app = create_app(Store(), api_token="api", limits=RateLimits(auth_failures_per_minute=10))

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await asyncio.gather(*(c.get("/items", headers={"Authorization": f"Bearer {n}"})
                                          for n in range(50)))

    codes = [r.status_code for r in asyncio.run(burst())]
    assert codes.count(401) == 10 and codes.count(429) == 40


def test_lockout_follows_the_real_client_behind_a_proxy():
    with client(RateLimits(auth_failures_per_minute=2, trust_proxy_headers=True)) as c:
        def get(ip, token):
            headers = {"Authorization": f"Bearer {token}", "X-Forwarded-For": ip}
            return c.get("/items", headers=headers).status_code

        assert [get("6.6.6.6", "guess") for _ in range(3)] == [401, 401, 429]
        assert get("1.2.3.4", "api") == 200  # another client behind the same proxy is unaffected


def test_successful_requests_are_not_limited():
    with client(RateLimits(webhook_per_minute=1, auth_failures_per_minute=1)) as c:
        ok = {"Authorization": "Bearer api"}
        assert all(c.get("/items", headers=ok).status_code == 200 for _ in range(50))


def test_limits_can_be_turned_off():
    with client(RateLimits(webhook_per_minute=0, auth_failures_per_minute=0)) as c:
        assert all(forged_sms(c).status_code == 403 for _ in range(20))
        wrong = {"Authorization": "Bearer x"}
        assert all(c.get("/items", headers=wrong).status_code == 401 for _ in range(20))
