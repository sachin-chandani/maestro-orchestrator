"""Keep the docs honest: examples in them must work against the real API."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PHONE_GUIDE = ROOT / "docs" / "integrations" / "phone-shortcuts.md"


def test_phone_guide_request_and_response_match_the_api():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.api import create_app
    from maestro.store import Store

    guide = PHONE_GUIDE.read_text()
    curl = re.search(r"```bash\n(curl .*?)```", guide, re.S)[1]
    path = re.search(r"https://maestro\.example\.com(/\S+)", curl)[1]
    body = json.loads(re.search(r"-d '(.*?)'", curl)[1])
    documented_fields = set(re.findall(r'"(\w+)":', re.search(r"```json\n(.*?)```", guide, re.S)[1]))

    with TestClient(create_app(Store(), api_token="t0ken")) as client:
        res = client.post(path, json=body, headers={"Authorization": "Bearer t0ken"})
        assert res.status_code == 201
        assert documented_fields <= set(res.json()), "the guide documents fields the API doesn't return"
        assert res.json()["kind"] == "reminder"
        assert client.post(path, json=body).status_code == 401  # as the guide says


def test_links_to_repository_files_resolve():
    adrs = sorted((ROOT / "docs" / "adr").glob("*.md"))
    docs = [ROOT / "README.md", ROOT / "ENGINEERING.md", PHONE_GUIDE, *adrs]
    for doc in docs:
        for target in re.findall(r"\]\(((?!https?://|#)[^)\s]+)\)", doc.read_text()):
            path = (doc.parent / target.split("#")[0]).resolve()
            assert path.exists(), f"{doc.relative_to(ROOT)} links to missing {target}"
