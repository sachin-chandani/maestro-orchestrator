# Contributing

Thanks for your interest! Issues and pull requests are welcome.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
ruff check . && mypy && pytest --cov
```

## Guidelines
- Open an issue before large changes so we can agree on the approach.
- Add tests for new behavior. Parser changes should come with example phrases in `tests/test_parser.py` or `tests/test_recurrence.py`.
- Keep the core (`parser`, `recurrence`, `store`, `scheduler`) free of network calls; integrations belong in channels.
- Never commit real phone numbers, tokens or personal data. Tests use fakes and made-up values.

## Releasing
1. Move the `[Unreleased]` notes in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading.
2. Set `__version__` in `maestro/__init__.py` (the only place the version lives).
3. Merge to `main`, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

The Release workflow checks the tag matches the version, runs the tests, builds the wheel, creates the GitHub release from the changelog section, and pushes a multi-arch image to `ghcr.io/sachin-chandani/maestro-orchestrator`. Publishing to PyPI runs once trusted publishing is set up and the `PYPI_PUBLISH` repository variable is `true`.

## Adding a delivery channel
1. Implement a class with `send(item: Item) -> None` in `maestro/notifier.py`.
2. Add its settings to `Config` and `.env.example`.
3. Register a factory in `CHANNELS` that raises `ConfigError` naming any missing settings.
4. Test it with a fake opener; see `tests/test_channels.py`.
