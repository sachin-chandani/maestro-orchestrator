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

    assert client.post(f"/items/{item['id']}/done", headers=AUTH).json()["status"] == "done"
    assert client.delete(f"/items/{item['id']}", headers=AUTH).status_code == 204
    assert client.get(f"/items/{item['id']}", headers=AUTH).status_code == 404


def test_rejects_empty_capture(client):
    assert client.post("/items", json={"text": ""}, headers=AUTH).status_code == 422
