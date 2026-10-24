import logging
import os

import pytest

from maestro import cli
from maestro.cli import main
from maestro.config import load_dotenv
from maestro.store import Store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.chdir(tmp_path)  # keep load_dotenv away from any real .env
    return str(tmp_path / "m.db")


def run(db, *args):
    return main(["--db", db, *args])


def test_list_search_and_empty_states(db, capsys):
    assert run(db, "list") == 0
    assert "Nothing to remember yet." in capsys.readouterr().out
    assert run(db, "search", "milk") == 0
    assert "No matches." in capsys.readouterr().out

    run(db, "add", "buy", "milk", "every", "monday", "at", "9am")
    run(db, "add", "remember", "the", "gate", "code", "is", "4321")
    capsys.readouterr()
    assert run(db, "list") == 0
    out = capsys.readouterr().out
    assert "Buy milk (every Monday)" in out and "The gate code is 4321" in out
    assert run(db, "search", "gate") == 0
    assert "4321" in capsys.readouterr().out


def test_done_rm_snooze(db, capsys):
    run(db, "add", "call", "mom", "tomorrow", "at", "5pm")
    assert run(db, "snooze", "1", "2h") == 0
    assert "Snoozed:" in capsys.readouterr().out
    assert run(db, "snooze", "1", "soon") == 2
    assert "Can't read duration" in capsys.readouterr().err
    assert run(db, "done", "1") == 0
    assert run(db, "list", "--all") == 0
    assert "[done]" in capsys.readouterr().out
    assert run(db, "rm", "1") == 0
    assert run(db, "rm", "1") == 1
    assert "No item #1" in capsys.readouterr().err


def test_bad_timezone_is_a_config_error(db, monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_TIMEZONE", "Mars/Base")
    assert run(db, "list") == 2
    assert "Config error: Unknown timezone" in capsys.readouterr().err


def test_run_once_fires_due_reminders(db, capsys):
    with Store(db) as store:
        from maestro.parser import parse

        store.add(parse("check the oven in 1 minute"))
        store.conn.execute("UPDATE items SET due_at = '2000-01-01T00:00:00+00:00'")
        store.conn.commit()
    assert run(db, "run", "--once") == 0
    assert "Reminder #1: Check the oven" in capsys.readouterr().out


def test_run_rejects_bad_channel_and_log_config(db, monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_CHANNELS", "pager")
    assert run(db, "run", "--once") == 2
    assert "Unknown channel(s): pager" in capsys.readouterr().err
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    monkeypatch.setenv("MAESTRO_LOG_FORMAT", "xml")
    assert run(db, "run", "--once") == 2
    assert "MAESTRO_LOG_FORMAT" in capsys.readouterr().err


def test_run_forever_stops_cleanly_on_ctrl_c(db, monkeypatch, capsys):
    def interrupted(store, notifier, interval, wait):
        assert interval == 5
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_forever", interrupted)
    assert run(db, "run", "--interval", "5") == 0
    assert "Watching for due reminders every 5s via console" in capsys.readouterr().out


def test_serve_wires_the_app(db, monkeypatch):
    pytest.importorskip("fastapi")
    import uvicorn

    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("MAESTRO_SMS_TO", "+15552223333")
    assert run(db, "serve", "--port", "9999") == 0
    assert started["port"] == 9999 and started["host"] == "127.0.0.1"
    assert started["log_config"] is None
    assert any(getattr(r, "path", None) == "/webhooks/twilio/sms" for r in started["app"].routes)


def test_serve_refuses_public_bind_without_token(db, capsys):
    pytest.importorskip("fastapi")
    assert run(db, "serve", "--host", "0.0.0.0") == 2
    assert "without MAESTRO_API_TOKEN" in capsys.readouterr().err


def test_telegram_bot_is_built_from_config(db, monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    config = cli.Config.from_env()
    with Store(db) as store, caplog.at_level(logging.WARNING):
        bot = cli._build_bot(config, store)
    assert bot is not None and bot.chat_id is None
    assert "TELEGRAM_CHAT_ID is not set" in caplog.text
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert cli._build_bot(cli.Config.from_env(), None) is None


def test_bot_wait_falls_back_to_sleeping_when_polling_fails(monkeypatch):
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)

    class Broken:
        def poll_once(self, timeout):
            raise ConnectionError("telegram down")

    cli._bot_wait(Broken())(7)
    assert slept == [7]


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nMAESTRO_TEST_A=1\nMAESTRO_TEST_B = 'quoted value'\nnot a pair\n\n"
                   "MAESTRO_TEST_C=from-file\n")
    monkeypatch.setenv("MAESTRO_TEST_C", "from-env")
    for key in ("MAESTRO_TEST_A", "MAESTRO_TEST_B"):
        # setenv first so monkeypatch records the variable as absent and removes it on teardown,
        # even if an assertion below fails.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    load_dotenv(env)
    assert os.environ["MAESTRO_TEST_A"] == "1"
    assert os.environ["MAESTRO_TEST_B"] == "quoted value"
    assert os.environ["MAESTRO_TEST_C"] == "from-env"  # real environment wins
    load_dotenv(tmp_path / "missing.env")  # no error
