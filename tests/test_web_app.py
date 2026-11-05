"""The React web app (ADR 0014) served at /app. Uses a fake build, so these tests never need Node."""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from maestro import api  # noqa: E402
from maestro.store import Store  # noqa: E402

INDEX = "<!doctype html><title>Maestro app</title>"


@pytest.fixture
def build(tmp_path, monkeypatch):
    (tmp_path / "app" / "assets").mkdir(parents=True)
    (tmp_path / "app" / "index.html").write_text(INDEX)
    (tmp_path / "app" / "assets" / "index.js").write_text("console.log('maestro')")
    (tmp_path / "secret.txt").write_text("api token")  # next to the build, never inside it
    monkeypatch.setattr(api, "WEB_APP_DIR", tmp_path / "app")


@pytest.fixture
def no_build(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "WEB_APP_DIR", tmp_path / "missing")


def client():
    return TestClient(api.create_app(Store(), api_token="t0ken"))


@pytest.mark.usefixtures("build")
def test_app_serves_the_build_without_a_token():
    with client() as c:
        page = c.get("/app/")
        assert page.status_code == 200
        assert page.text == INDEX
        assert c.get("/app", follow_redirects=True).text == INDEX

        script = c.get("/app/assets/index.js")
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]


@pytest.mark.usefixtures("build")
def test_missing_files_and_files_outside_the_build_are_404():
    with client() as c:
        assert c.get("/app/assets/old.js").status_code == 404
        for path in ("/app/../secret.txt", "/app/%2e%2e/secret.txt", "/app/..%2fsecret.txt"):
            response = c.get(path)
            assert "api token" not in response.text, path


@pytest.mark.usefixtures("no_build")
def test_without_a_build_app_explains_how_to_build():
    with client() as c:
        for path in ("/app", "/app/", "/app/assets/index.js"):
            response = c.get(path)
            assert response.status_code == 503, path
            assert "hasn't been built" in response.text
            assert "npm run build" in response.text


@pytest.mark.parametrize("fixture", ["build", "no_build"])
def test_existing_page_and_api_are_unchanged(fixture, request):
    request.getfixturevalue(fixture)
    with client() as c:
        page = c.get("/")
        assert page.status_code == 200
        assert "<title>Maestro</title>" in page.text
        assert c.get("/items").status_code == 401  # the API still needs the token
        paths = c.get("/openapi.json").json()["paths"]
        assert not any(p.startswith("/app") for p in paths)
