"""Keep the docs honest: examples and claims in them must hold against the real API."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PHONE_GUIDE = ROOT / "docs" / "integrations" / "phone-shortcuts.md"
AUTH = {"Authorization": "Bearer t0ken"}


def find(pattern: str, text: str, what: str, flags: int = 0) -> str:
    m = re.search(pattern, text, flags)
    assert m, f"couldn't find {what} in {PHONE_GUIDE.name}; update this test if the guide's format changed"
    return m[1]


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app
    from maestro.store import Store

    with TestClient(create_app(Store(), api_token="t0ken")) as c:
        yield c


def test_phone_guide_request_and_response_match_the_api(client):
    guide = PHONE_GUIDE.read_text()
    curl = find(r"```bash\n(curl .*?)```", guide, "the curl example", re.S)
    path = find(r"https://maestro\.example\.com(/\S+)", curl, "the request path")
    body = json.loads(find(r"-d '(.*?)'", curl, "the request body"))
    example = find(r"```json\n(.*?)```", guide, "the response example", re.S)
    documented = set(re.findall(r'"(\w+)":', example))

    res = client.post(path, json=body, headers=AUTH)
    assert res.status_code == 201
    assert documented <= set(res.json()), "the guide documents fields the API doesn't return"
    assert res.json()["when"] != "note" and res.json()["kind"] == "reminder"


def test_phone_guide_error_claims(client):
    # "A missing or wrong token gets 401; after 10 wrong tokens in a minute, the client gets 429."
    assert client.post("/items", json={"text": "x"}).status_code == 401
    wrong = {"Authorization": "Bearer guess"}
    codes = [client.post("/items", json={"text": "x"}, headers=wrong).status_code for _ in range(11)]
    assert codes == [401] * 10 + [429]


def test_text_with_awkward_characters_survives_a_json_body(client):
    # What the Tasker JavaScriptlet (JSON.stringify) produces for dictated or typed text.
    text = 'pick up "the" parcel from C:\\ drive\nat 5pm'
    res = client.post("/items", content=json.dumps({"text": text}),
                      headers={**AUTH, "Content-Type": "application/json"})
    assert res.status_code == 201
    assert res.json()["source_text"] == text


def test_notes_say_note(client):
    res = client.post("/items", json={"text": "the gate code is 4321"}, headers=AUTH)
    assert (res.json()["kind"], res.json()["when"], res.json()["due_at"]) == ("note", "note", None)


def test_links_to_repository_files_resolve():
    def visible(path: Path) -> bool:  # skips .venv, .github, caches
        return not any(part.startswith(".") for part in path.relative_to(ROOT).parts)

    docs = [p for p in ROOT.rglob("*.md") if visible(p)]
    assert len(docs) > 10
    for doc in docs:
        text = re.sub(r"```.*?```", "", doc.read_text(), flags=re.S)  # code samples aren't links
        for target in re.findall(r"\]\(((?!https?://|mailto:|#)[^)\s]+)\)", text):
            relative = target.split("#")[0]
            base = ROOT if relative.startswith("/") else doc.parent
            path = (base / relative.lstrip("/")).resolve()
            assert path.exists(), f"{doc.relative_to(ROOT)} links to missing {target}"
