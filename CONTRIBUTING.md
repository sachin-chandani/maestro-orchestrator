# Contributing

Thanks for your interest! Issues and pull requests are welcome.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
ruff check . && mypy && pytest
```

The PostgreSQL tests need a server. Without one they skip, and everything else still runs — but
the coverage gate (`pytest --cov`) assumes the backend was exercised, so run it with a server, as
CI does:

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_USER=maestro -e POSTGRES_PASSWORD=maestro \
  -e POSTGRES_DB=maestro_test --name maestro-pg postgres:16
export MAESTRO_TEST_POSTGRES_URL=postgresql://maestro:maestro@127.0.0.1:5432/maestro_test
pytest -q   # the conformance suite now runs against both backends
```

```bash
pytest --cov   # 96%+ with both backends covered
```

Give it a database of its own: the tests empty every table between runs. CI starts the same
service, so the backend is covered on every pull request.

## Guidelines
- Open an issue before large changes so we can agree on the approach.
- Add tests for new behavior. Parser changes should come with example phrases in `tests/test_parser.py` or `tests/test_recurrence.py`.
- Keep the core (`parser`, `recurrence`, `storage`, `scheduler`) free of network calls; integrations belong in channels.
- Storage changes go through the interface in `maestro/storage/base.py`, and behaviour belongs in `tests/test_storage_conformance.py`, which runs against every backend. Adding a column means adding it to both backends' migrations.
- Never commit real phone numbers, tokens or personal data. Tests use fakes and made-up values.

## Releasing
1. Move the `[Unreleased]` notes in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading.
2. Set `__version__` in `maestro/__init__.py` (the only place the version lives).
3. Merge to `main`, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

The Release workflow checks the tag matches the version, runs the tests, builds the wheel, creates the GitHub release from the changelog section, and pushes a multi-arch image to `ghcr.io/sachin-chandani/maestro-orchestrator`. Publishing to PyPI runs once trusted publishing is set up and the `PYPI_PUBLISH` repository variable is `true`.

## Adding a storage backend
1. Subclass `maestro.storage.base.Store` and implement every method; the docstrings there are the contract.
2. Register its URL scheme in `maestro/storage/factory.py`, importing the driver only when asked for.
3. Add it to `BACKENDS` in `tests/test_storage_conformance.py` and make all of it pass.
4. Add what only a real server can show — connecting, migrating, restarts, locking between processes — as `tests/test_storage_postgres.py` does.

## Adding a delivery channel
1. Implement a class with `send(item: Item) -> None` in `maestro/notifier.py`.
2. Add its settings to `Config` and `.env.example`.
3. Register a factory in `CHANNELS` that raises `ConfigError` naming any missing settings.
4. Test it with a fake opener; see `tests/test_channels.py`.
