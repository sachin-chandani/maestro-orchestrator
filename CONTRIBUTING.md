# Contributing

Thanks for your interest! Issues and pull requests are welcome.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest && ruff check .
```

## Guidelines
- Open an issue before large changes so we can agree on the approach.
- Add tests for new behavior. Parser changes should come with example phrases in `tests/test_parser.py` or `tests/test_recurrence.py`.
- Keep the core (`parser`, `recurrence`, `store`, `scheduler`) free of network calls; integrations belong in channels.
- Never commit real phone numbers, tokens or personal data. Tests use fakes and made-up values.

## Adding a delivery channel
1. Implement a class with `send(item: Item) -> None` in `maestro/notifier.py`.
2. Add its settings to `Config` and `.env.example`.
3. Register a factory in `CHANNELS` that raises `ConfigError` naming any missing settings.
4. Test it with a fake opener; see `tests/test_channels.py`.
