"""Devices signed in with a token of their own (#108, ADR 0018): the module, the API and the CLI."""

from datetime import timedelta

import pytest
from conftest import TZ, dt

from maestro import devices
from maestro.cli import main
from maestro.storage import SQLiteStore

NOW = dt(2026, 11, 12, 9, 0)


# --- the module ------------------------------------------------------------------------------


def test_register_stores_only_the_hash_and_hands_out_the_token_once():
    with SQLiteStore(":memory:", tz=TZ) as store:
        device, token = devices.register(store, "  Sachin's iPhone  ", NOW)
        assert token.startswith(devices.PREFIX) and len(token) > 40
        assert device.name == "Sachin's iPhone"
        assert device.token_hash == devices.hash_token(token)
        assert token not in device.token_hash
        seen = devices.authenticate(store, token, NOW)
        assert seen is not None and seen.id == device.id and seen.last_seen_at == NOW


def test_tokens_are_unpredictable_and_a_name_is_bounded():
    with SQLiteStore(":memory:", tz=TZ) as store:
        _, one = devices.register(store, "a", NOW)
        _, two = devices.register(store, "b", NOW)
        assert one != two
        long_name, _ = devices.register(store, "x" * 500, NOW)
        assert len(long_name.name) == devices.MAX_NAME
        blank, _ = devices.register(store, "   ", NOW)
        assert blank.name == "Device"


def test_authenticate_rejects_unknown_revoked_and_non_device_tokens():
    with SQLiteStore(":memory:", tz=TZ) as store:
        device, token = devices.register(store, "Phone", NOW)
        assert devices.authenticate(store, "master-token-guess", NOW) is None
        assert devices.authenticate(store, devices.PREFIX + "nope", NOW) is None
        assert store.revoke_device(device.id, NOW)
        assert devices.authenticate(store, token, NOW) is None


def test_authenticate_records_last_seen_at_most_once_an_hour():
    with SQLiteStore(":memory:", tz=TZ) as store:
        device, token = devices.register(store, "Phone", NOW)
        assert devices.authenticate(store, token, NOW).last_seen_at == NOW
        soon = NOW + timedelta(minutes=30)
        assert devices.authenticate(store, token, soon).last_seen_at == NOW  # not written again
        later = NOW + devices.SEEN_EVERY
        assert devices.authenticate(store, token, later).last_seen_at == later
        assert store.get_device(device.id).last_seen_at == later


# --- the API ---------------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro.api import create_app  # noqa: E402
from maestro.ratelimit import RateLimits  # noqa: E402

MASTER = {"Authorization": "Bearer t0ken"}


@pytest.fixture
def client():
    with TestClient(create_app(SQLiteStore(":memory:", tz=TZ), api_token="t0ken",
                               limits=RateLimits(auth_failures_per_minute=3))) as c:
        yield c


def sign_in(client, name="Sachin's iPhone"):
    res = client.post("/devices", json={"name": name}, headers=MASTER)
    assert res.status_code == 201, res.text
    body = res.json()
    return body, {"Authorization": f"Bearer {body['token']}"}


def test_a_device_signs_in_with_the_master_token_and_then_uses_its_own(client):
    body, device = sign_in(client)
    assert body["name"] == "Sachin's iPhone" and body["token"].startswith(devices.PREFIX)
    assert body["current"] is False and body["last_seen_at"] is None

    assert client.get("/items", headers=device).status_code == 200
    saved = client.post("/items", json={"text": "call mom at 5pm"}, headers=device)
    assert saved.status_code == 201
    assert client.post("/capture", json={"text": "buy milk"}, headers=device).status_code == 201
    assert client.get("/metrics", headers=device).status_code == 200


def test_the_token_is_never_shown_again(client):
    body, device = sign_in(client)
    listed = client.get("/devices", headers=MASTER).json()
    assert listed == [{"id": body["id"], "name": "Sachin's iPhone", "created_at": body["created_at"],
                       "last_seen_at": listed[0]["last_seen_at"], "current": False}]
    assert "token" not in listed[0]
    # A device sees itself marked as the one asking.
    mine = client.get("/devices", headers=device).json()
    assert [d["current"] for d in mine] == [True]
    assert mine[0]["last_seen_at"] is not None


def test_a_device_cannot_manage_devices(client):
    body, device = sign_in(client)
    assert client.post("/devices", json={"name": "spare key"}, headers=device).status_code == 403
    assert client.delete(f"/devices/{body['id']}", headers=device).status_code == 403
    assert len(client.get("/devices", headers=MASTER).json()) == 1


def test_revoking_a_device_signs_it_out_at_once(client):
    body, device = sign_in(client)
    assert client.delete(f"/devices/{body['id']}", headers=MASTER).status_code == 204
    assert client.delete(f"/devices/{body['id']}", headers=MASTER).status_code == 404
    res = client.get("/items", headers=device)
    assert res.status_code == 401 and res.json()["detail"] == "Invalid API token"
    assert client.get("/devices", headers=MASTER).json() == []


def test_a_wrong_device_token_counts_as_a_guess(client):
    bad = {"Authorization": f"Bearer {devices.PREFIX}guess"}
    assert [client.get("/items", headers=bad).status_code for _ in range(4)] == [401, 401, 401, 429]


def test_a_missing_header_still_does_not_count_and_a_bare_token_is_refused(client):
    assert client.get("/items").status_code == 401
    assert client.get("/items").status_code == 401
    _, device = sign_in(client)
    bare = {"Authorization": device["Authorization"].removeprefix("Bearer ")}
    assert client.get("/items", headers=bare).status_code == 401
    assert client.get("/items", headers=device).status_code == 200  # not locked out by the above


def test_device_names_are_validated(client):
    assert client.post("/devices", json={"name": ""}, headers=MASTER).status_code == 422
    assert client.post("/devices", json={"name": "x" * 81}, headers=MASTER).status_code == 422


def test_without_a_master_token_devices_are_open_like_everything_else():
    with TestClient(create_app(SQLiteStore(":memory:", tz=TZ))) as c:
        assert c.post("/devices", json={"name": "Phone"}).status_code == 201
        assert c.get("/items").status_code == 200


# --- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)
    return str(tmp_path / "m.db")


def test_devices_cli_adds_lists_and_revokes(db, capsys):
    assert main(["--db", db, "devices", "list"]) == 0
    assert "No devices signed in" in capsys.readouterr().out

    assert main(["--db", db, "devices", "add", "kitchen", "tablet"]) == 0
    out = capsys.readouterr().out
    assert "Added device #1 (kitchen tablet)" in out
    token = out.strip().splitlines()[-1]
    assert token.startswith(devices.PREFIX)

    assert main(["--db", db, "devices", "list"]) == 0
    out = capsys.readouterr().out
    assert "#1  kitchen tablet" in out and "last seen never" in out and token not in out

    with SQLiteStore(db, tz=TZ) as store:
        assert devices.authenticate(store, token, NOW) is not None

    assert main(["--db", db, "devices", "revoke", "1"]) == 0
    assert "Revoked device #1" in capsys.readouterr().out
    assert main(["--db", db, "devices", "revoke", "1"]) == 1
    assert "No device #1" in capsys.readouterr().err
    with SQLiteStore(db, tz=TZ) as store:
        assert devices.authenticate(store, token, NOW) is None
