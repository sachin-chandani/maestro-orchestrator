"""The released wheel must ship the React app (#111).

`python -m build` builds the wheel from the sdist, so both hatch targets need the app in
`artifacts`. The real build is checked in CI by scripts/check_wheel_app.py; these tests cover
the config and the checker without running a build.
"""

import sys
import tomllib
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from check_wheel_app import check  # noqa: E402

ROOT = Path(__file__).parent.parent


def test_both_hatch_targets_ship_the_app():
    # Regression test for #111: before the fix only the wheel target listed the app, and the
    # sdist (which the release wheel is built from) silently dropped it.
    with open(ROOT / "pyproject.toml", "rb") as f:
        targets = tomllib.load(f)["tool"]["hatch"]["build"]["targets"]
    for target in ("wheel", "sdist"):
        assert "maestro/web/app/**" in targets[target].get("artifacts", []), target


def make_wheel(path, names):
    with zipfile.ZipFile(path, "w") as wheel:
        for name in names:
            wheel.writestr(name, "x")
    return str(path)


def test_checker_accepts_a_wheel_with_the_app(tmp_path):
    wheel = make_wheel(
        tmp_path / "ok.whl",
        ["maestro/web/app/index.html", "maestro/web/app/assets/index-abc.js", "maestro/api.py"],
    )
    assert check(wheel) is None


def test_checker_rejects_a_wheel_without_the_app(tmp_path):
    wheel = make_wheel(tmp_path / "bare.whl", ["maestro/api.py", "maestro/web/index.html"])
    error = check(wheel)
    assert error is not None and "index.html" in error


def test_checker_rejects_a_wheel_without_assets(tmp_path):
    wheel = make_wheel(tmp_path / "noassets.whl", ["maestro/web/app/index.html"])
    error = check(wheel)
    assert error is not None and "assets" in error
