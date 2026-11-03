import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from maestro.api import create_app  # noqa: E402
from maestro.store import MIGRATIONS, Store  # noqa: E402

AUTH = {"Authorization": "Bearer t0ken"}


@pytest.fixture
def client():
    with TestClient(create_app(Store(), api_token="t0ken")) as c:
        yield c


def test_health_is_public(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["schema"] == len(MIGRATIONS)


def test_web_ui_is_public_and_holds_no_data(client):
    client.post("/items", json={"text": "remember the gate code is 4321"}, headers=AUTH)
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>Maestro</title>" in page.text
    assert "4321" not in page.text  # data only comes through the authenticated API


def test_requires_token(client):
    assert client.get("/items").status_code == 401
    assert client.get("/items", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_capture_list_snooze_done_delete(client):
    created = client.post("/items", json={"text": "gym every monday at 7am"}, headers=AUTH)
    assert created.status_code == 201
    item = created.json()
    assert item["text"] == "Gym"
    assert item["recurrence_label"] == "every Monday"

    client.post("/items", json={"text": "remember the gate code is 4321"}, headers=AUTH)
    assert [i["text"] for i in client.get("/items", headers=AUTH).json()] == ["Gym", "The gate code is 4321"]
    assert [i["text"] for i in client.get("/items?q=gate", headers=AUTH).json()] == ["The gate code is 4321"]

    snoozed = client.post(f"/items/{item['id']}/snooze", json={"duration": "10m"}, headers=AUTH)
    assert snoozed.status_code == 200
    bad = client.post(f"/items/{item['id']}/snooze", json={"duration": "soon"}, headers=AUTH)
    assert bad.status_code == 422

    done = client.post(f"/items/{item['id']}/done", headers=AUTH).json()
    assert done["status"] == "pending"  # a weekly reminder: done is about this week, the series goes on
    assert client.post(f"/items/{item['id']}/stop", headers=AUTH).json()["status"] == "done"
    assert client.delete(f"/items/{item['id']}", headers=AUTH).status_code == 204
    assert client.get(f"/items/{item['id']}", headers=AUTH).status_code == 404


def test_rejects_empty_capture(client):
    assert client.post("/items", json={"text": ""}, headers=AUTH).status_code == 422


def test_items_expose_channels_and_urgent():
    with TestClient(create_app(Store(), api_token="t0ken")) as c:
        item = c.post("/items", json={"text": "!pay rent tomorrow, text me"}, headers=AUTH).json()
    assert item["channels"] == ["twilio_sms"]
    assert item["urgent"] is True
    assert item["text"] == "Pay rent"


def test_capture_drops_channels_the_server_has_no_settings_for():
    # The server knows what is configured (`requestable`): a choice it can't honour is dropped at
    # capture rather than surprising the user at delivery time (ADR 0011).
    with TestClient(create_app(Store(), api_token="t0ken", requestable=frozenset({"email"}))) as c:
        dropped = c.post("/items", json={"text": "pay rent tomorrow, text me"}, headers=AUTH).json()
        kept = c.post("/items", json={"text": "pay gas tomorrow, email me"}, headers=AUTH).json()
    assert dropped["channels"] is None
    assert kept["channels"] == ["email"]
