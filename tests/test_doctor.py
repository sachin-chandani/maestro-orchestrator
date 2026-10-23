import pytest

from maestro import doctor
from maestro.cli import main
from maestro.config import Config
from maestro.notifier import CHANNELS


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
    assert lines[1].startswith("✓ database: ") and "schema 4, 0 items" in lines[1]
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


@pytest.mark.parametrize("path", ["/proc/definitely/not/writable/m.db"])
def test_unwritable_database(path):
    ok, lines = run(Config(db_path=path, channels=["console"], api_token="t"))
    assert not ok
    assert lines[1].startswith(f"✗ database: {path}")
