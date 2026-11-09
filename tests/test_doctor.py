import pytest

from maestro import doctor
from maestro.cli import main
from maestro.config import Config
from maestro.notifier import CHANNELS
from maestro.store import MIGRATIONS


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item.text)


class Broken:
    def send(self, item):
        raise ConnectionError("401 Unauthorized")


def run(config, send_test=False):
    lines = []
    ok = doctor.run(config, send_test=send_test, out=lines.append)
    return ok, lines


def test_healthy_config(tmp_path):
    ok, lines = run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t"))
    assert ok
    assert lines[0] == "✓ timezone: America/New_York (system default)"
    assert lines[1].startswith("✓ database: ")
    assert f"schema {len(MIGRATIONS)}, wal journal, 0 items" in lines[1]
    assert "✓ API token: set" in lines
    assert "✓ channel console: configured" in lines
    assert lines[-1].startswith("0 failed, 0 warnings. Run with --send-test")


def test_reports_every_problem_and_fails(tmp_path):
    config = Config(db_path=str(tmp_path / "m.db"), channels=["console", "pager", "twilio_sms"],
                    timezone="Mars/Base", telegram_bot_token="123:abc")
    ok, lines = run(config)
    assert not ok
    assert lines[0].startswith("✗ timezone: Unknown timezone 'Mars/Base'")
    assert any(line.startswith("✗ database:") for line in lines)
    assert "! API token: not set: fine on localhost, required before exposing the server" in lines
    assert any(line.startswith("! telegram bot: TELEGRAM_CHAT_ID not set") for line in lines)
    assert any(line.startswith("✗ channel pager: unknown") for line in lines)
    twilio = "✗ channel twilio_sms: twilio_sms channel needs TWILIO_ACCOUNT_SID"
    assert any(line.startswith(twilio) for line in lines)
    assert lines[-1].startswith("4 failed, 2 warnings.")


def test_send_test_delivers_on_each_channel(tmp_path, monkeypatch):
    good = Recorder()
    monkeypatch.setitem(CHANNELS, "good", lambda _: good)
    monkeypatch.setitem(CHANNELS, "bad", lambda _: Broken())
    ok, lines = run(Config(db_path=str(tmp_path / "m.db"), channels=["good", "bad"], api_token="t"),
                    send_test=True)
    assert not ok
    assert "✓ send good: delivered" in lines
    assert "✗ send bad: ConnectionError: 401 Unauthorized" in lines
    assert good.sent == ["Maestro test message: if you can read this, the channel works"]
    assert lines[-1] == "1 failed, 0 warnings."


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    assert main(["--db", str(tmp_path / "m.db"), "doctor"]) == 0
    assert "✓ channel console: configured" in capsys.readouterr().out

    monkeypatch.setenv("MAESTRO_CHANNELS", "pager")
    assert main(["--db", str(tmp_path / "m.db"), "doctor"]) == 1


def test_bad_timezone_does_not_crash_the_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_TIMEZONE", "Nowhere/Land")
    monkeypatch.setenv("MAESTRO_CHANNELS", "console")
    assert main(["--db", str(tmp_path / "m.db"), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ timezone" in out and "✗ database" in out


def test_database_errors_after_opening_are_reported_not_raised(tmp_path, monkeypatch):
    import sqlite3

    from maestro.store import Store

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "counts", locked)
    ok, lines = run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t"))
    assert not ok
    assert lines[1].endswith("database is locked")


@pytest.mark.parametrize("path", ["/proc/definitely/not/writable/m.db"])
def test_unwritable_database(path):
    ok, lines = run(Config(db_path=path, channels=["console"], api_token="t"))
    assert not ok
    assert lines[1].startswith(f"✗ database: {path}")


def test_an_unreachable_backend_is_reported_without_its_password():
    # Port 1 on loopback refuses at once: no name to resolve, nothing on the network.
    url = "postgresql://maestro:hunter2@127.0.0.1:1/maestro"
    ok, lines = run(Config(db_path=url, channels=["console"], api_token="t"))
    assert not ok
    assert lines[1].startswith("\u2717 database: postgresql://maestro:***@127.0.0.1:1/maestro: ")
    assert "hunter2" not in lines[1]  # it says which database, never the password
    assert "Traceback" not in lines[1]


def test_an_unsupported_backend_fails_the_check():
    ok, lines = run(Config(db_path="mysql://db/maestro", channels=["console"], api_token="t"))
    assert not ok
    assert lines[1].startswith("\u2717 database: Unsupported database URL")


def test_both_database_settings_set_warns_which_one_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB_URL", f"sqlite://{tmp_path}/m.db")
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "ignored.db"))
    config = Config.from_env()
    config.channels, config.api_token = ["console"], "t"
    ok, lines = run(config)
    assert ok  # a warning, not a failure
    assert "! database: MAESTRO_DB_URL and MAESTRO_DB are both set; MAESTRO_DB is ignored" in lines
    assert not (tmp_path / "ignored.db").exists()


def test_database_not_in_wal_is_a_warning(tmp_path, monkeypatch):
    from maestro.store import Store

    monkeypatch.setattr(Store, "_enable_wal", lambda self: "delete")
    monkeypatch.setattr(Store, "journal_mode", property(lambda self: "delete"))
    ok, lines = run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t"))
    assert ok  # a warning, not a failure
    assert lines[1].startswith("! database:") and "not in WAL mode" in lines[1]


# --- the interpreter and the model (#115) -------------------------------------------------------


def model_config(tmp_path, **extra):
    settings = {"interpreter": "model", "model_provider": "anthropic",
                "model": "claude-sonnet-5-5", "model_api_key": "sk-fake-key-for-tests-only"}
    return Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                  **{**settings, **extra})


def line_for(lines, name):
    return next(line for line in lines if line.split(":")[0].endswith(name))


def test_the_rules_are_reported_as_the_default_and_nothing_is_contacted(tmp_path):
    ok, lines = run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t"))
    assert ok
    assert "rules (offline; no model is contacted)" in line_for(lines, "interpreter")
    assert not any("model privacy" in line for line in lines)
    assert "--check-model" not in "\n".join(lines)  # nothing to test


def test_a_hosted_provider_is_reported_with_a_plain_privacy_warning(tmp_path):
    ok, lines = run(model_config(tmp_path))
    assert ok  # a warning, not a failure: it's a choice, not a mistake
    interpreter = line_for(lines, "interpreter")
    assert "claude-sonnet-5-5" in interpreter and "anthropic" in interpreter
    assert "the rules answer if it fails" in interpreter
    assert "the text of every capture is sent to anthropic" in line_for(lines, "model privacy")
    assert "sk-fake-key-for-tests-only" not in "\n".join(lines)  # the key is never printed
    assert "Run with --check-model" in lines[-1]


def test_a_model_on_your_own_network_is_not_warned_about(tmp_path):
    ok, lines = run(model_config(tmp_path, model_provider="openai-compatible", model="llama3.2",
                                 model_api_key=None, model_base_url="http://localhost:11434/v1"))
    assert ok
    assert "captures stay in" in line_for(lines, "model privacy")
    assert "http://localhost:11434/v1" in line_for(lines, "interpreter")


def test_a_half_configured_model_fails_the_check(tmp_path):
    ok, lines = run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                           interpreter="model", model_provider="anthropic"))
    assert not ok
    assert "needs MAESTRO_MODEL" in line_for(lines, "interpreter")


def test_a_model_with_no_key_fails_the_check_before_a_deploy(tmp_path):
    """Caught here rather than at the first capture, which is what doctor is for."""
    ok, lines = run(model_config(tmp_path, model_api_key=None))
    assert not ok
    assert "needs MAESTRO_MODEL_API_KEY or ANTHROPIC_API_KEY" in line_for(lines, "interpreter")


def test_a_long_timeout_is_flagged_when_twilio_can_call_in(tmp_path):
    ok, lines = run(model_config(tmp_path, model_timeout_seconds=30.0,
                                 twilio_auth_token="tok", sms_to="+15551112222"))
    assert ok
    assert "Twilio gives a webhook about 15s" in line_for(lines, "model timeout")
    # without inbound Twilio the same timeout is nobody's problem
    _, quiet = run(model_config(tmp_path, model_timeout_seconds=30.0))
    assert not any("model timeout" in line for line in quiet)


# --- --check-model: the one live call, faked here ------------------------------------------------


def check_model(tmp_path, reply, monkeypatch, **extra):
    """Run doctor --check-model with a provider transport that answers with `reply`."""
    import json

    from maestro import model as model_module

    def transport(url, headers, body, timeout):
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply).encode()

    monkeypatch.setattr(model_module, "http_transport", transport)
    lines = []
    ok = doctor.run(model_config(tmp_path, **extra), out=lines.append, check_model=True)
    return ok, lines


def anthropic_reply(actions):
    return {"content": [{"type": "tool_use", "name": "propose_actions", "input": {"actions": actions}}]}


def test_check_model_sends_the_test_phrase_and_shows_what_it_proposed(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from maestro.tz import get_timezone

    due = (datetime.now(get_timezone()) + timedelta(days=1)).replace(hour=15, minute=0, second=0,
                                                                     microsecond=0)
    ok, lines = check_model(tmp_path, anthropic_reply(
        [{"type": "create_reminder", "text": "Dentist", "due_at": due.isoformat()}]), monkeypatch)
    assert ok
    test = line_for(lines, "model test")
    assert doctor.TEST_PHRASE in test and '"type": "create_reminder"' in test and "Dentist" in test


def test_check_model_reports_a_provider_failure_as_a_failure(tmp_path, monkeypatch):
    from maestro.model import ProviderError

    ok, lines = check_model(tmp_path, ProviderError("http", "HTTP 401 from the model provider"),
                            monkeypatch)
    assert not ok
    assert "HTTP 401" in line_for(lines, "model test")


def test_check_model_warns_when_the_answer_would_not_be_used(tmp_path, monkeypatch):
    """A model that answers but fails validation is a warning: captures still work, via the rules."""
    ok, lines = check_model(tmp_path, anthropic_reply(
        [{"type": "create_reminder", "text": "Dentist", "due_at": "tomorrow at 3"}]), monkeypatch)
    assert ok  # captures are never lost, so this is not a failure
    test = line_for(lines, "model test")
    assert "wouldn't use it" in test and "fall back to the rules" in test


def test_check_model_does_nothing_when_the_rules_are_in_use(tmp_path, monkeypatch):
    lines = []
    assert doctor.run(Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t"),
                      out=lines.append, check_model=True)
    assert not any("model test" in line for line in lines)


def test_the_cli_passes_check_model_through(tmp_path, monkeypatch):
    seen = {}

    def fake_run(config, send_test=False, out=print, check_model=False):
        seen.update(send_test=send_test, check_model=check_model)
        return True

    monkeypatch.setattr(doctor, "run", fake_run)
    assert main(["--db", str(tmp_path / "m.db"), "doctor", "--check-model"]) == 0
    assert seen == {"send_test": False, "check_model": True}
