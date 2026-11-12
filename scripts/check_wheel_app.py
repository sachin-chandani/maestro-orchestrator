"""Fail unless a built wheel ships the React app (#111).

`python -m build` builds the wheel from the sdist, so the app survives only if both hatch
targets list it in `artifacts`. Run after `python -m build`:

    python scripts/check_wheel_app.py dist/*.whl
"""

import sys
import zipfile
from pathlib import PurePosixPath


def check(wheel_path: str) -> str | None:
    """Return an error sentence, or None if the wheel ships the app."""
    with zipfile.ZipFile(wheel_path) as wheel:
        files = {PurePosixPath(name) for name in wheel.namelist()}
    index = PurePosixPath("maestro/web/app/index.html")
    if index not in files:
        return f"{wheel_path} has no {index}; the sdist dropped the React app (see #111)."
    assets = [f for f in files if f.is_relative_to("maestro/web/app/assets")]
    if not assets:
        return f"{wheel_path} has {index} but nothing under maestro/web/app/assets/."
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_wheel_app.py WHEEL...", file=sys.stderr)
        return 2
    for wheel_path in argv:
        error = check(wheel_path)
        if error:
            print(error, file=sys.stderr)
            return 1
        print(f"{wheel_path}: React app present.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
