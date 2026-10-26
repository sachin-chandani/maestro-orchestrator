"""#9: email-in over IMAP, tested against an in-memory fake mailbox."""

from email.message import EmailMessage

import pytest

from maestro.config import Config, email_settings
from maestro.email_in import EmailPoller, EmailSettings, capture_text
from maestro.observability import METRICS
from maestro.store import Store

OWNER = "me@example.com"


class FakeMailbox:
    """Just enough IMAP: UID SEARCH UNSEEN, UID FETCH (BODY.PEEK[]), UID STORE +FLAGS (\\Seen)."""

    def __init__(self):
        self.messages: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self.logins = []
        self.fail_store = False      # raise mid-STORE, like a dropped connection
        self.read_only = False       # STORE answers NO
        self.wrong_password = False
        self.fetches = 0
        self.closed = 0

    def add(self, raw: bytes) -> str:
        uid = str(len(self.messages) + 1)
        self.messages[uid] = raw
        return uid

    def login(self, user, password):
        if self.wrong_password:
            raise ConnectionError("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logins.append((user, password))

    def select(self, mailbox="INBOX"):
        if mailbox != "INBOX":
            return "NO", [b"no such folder"]
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            unseen = [u for u in self.messages if u not in self.seen]
            return "OK", [" ".join(unseen).encode()]
        if command == "FETCH":
            self.fetches += 1
            uid = args[0]
            raw = self.messages[uid]
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            if self.fail_store:
                raise ConnectionError("connection reset")
            if self.read_only:
                return "NO", [b"read-only folder"]
            self.seen.update(args[0].split(","))
            return "OK", []
        raise AssertionError(command)

    def logout(self):
        if self.wrong_password:
            raise OSError("not logged in")
        self.closed += 1

    def shutdown(self):
        self.closed += 1


PROVIDER = "mx.example.com"


def mail(subject="dentist next tuesday at 3pm", sender=OWNER, auth=(f"{PROVIDER}; dmarc=pass",),
         body="", message_id=None):
    message = EmailMessage()
    for value in auth:
        message["Authentication-Results"] = value  # added in order: the first one is topmost
    message["From"] = f"Me <{sender}>"
    message["To"] = "maestro@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id or f"<{abs(hash((subject, sender, auth, body)))}@example.com>"
    message.set_content(body or "sent from my phone")
    return bytes(message)


@pytest.fixture
def box():
    return FakeMailbox()


def poller(box, require_dmarc=True, folder="INBOX"):
    settings = EmailSettings(host="imap.example.com", user="maestro", password="pw", folder=folder,
                             allowed_senders=frozenset({OWNER}), require_dmarc=require_dmarc,
                             authserv_id=PROVIDER, poll_seconds=10)
    return EmailPoller(Store(), settings, connect=lambda _: box)


def test_captures_and_marks_read(box):
    uid = box.add(mail())
    p = poller(box)
    items = p.poll_once()
    assert [i.text for i in items] == ["Dentist"]
    assert box.seen == {uid}
    assert box.logins == [("maestro", "pw")]  # one session per poll
    assert METRICS.get("maestro_captures_total", source="email") == 1
    assert p.poll_once() == []  # nothing unread left


def test_fetch_does_not_mark_messages_read(box):
    box.add(mail())
    assert len(poller(box).fetch()) == 1
    assert box.seen == set()


@pytest.mark.parametrize("subject, body, expected", [
    ("Re: call mom at 5pm", "", "Call mom"),
    ("Fwd: FW: Dentist appointment confirmation", "", "Dentist appointment confirmation"),
    ("", "\n\n  pay rent on the 1st of every month\nthanks", "Pay rent"),
])
def test_subject_or_first_body_line(box, subject, body, expected):
    box.add(mail(subject=subject, body=body))
    assert [i.text for i in poller(box).poll_once()] == [expected]


def test_unknown_senders_are_ignored_but_marked_read(box):
    uid = box.add(mail(sender="stranger@example.net"))
    assert poller(box).poll_once() == []
    assert box.seen == {uid}


@pytest.mark.parametrize("auth", [
    # Our provider says DMARC failed; the sender added its own "pass" further down, even with our id.
    (f"{PROVIDER}; dmarc=fail header.from=example.com", "evil; dmarc=pass", f"{PROVIDER}; dmarc=pass"),
    # Provider added nothing: a forged header from another server is topmost.
    ("evil.example.net; dmarc=pass",),
    (),
])
def test_forged_senders_are_rejected(box, auth):
    box.add(mail(auth=auth))
    assert poller(box).poll_once() == []


def test_only_our_providers_header_counts_wherever_it_is(box):
    # An internal relay added its own header above the provider's: still accepted.
    box.add(mail(auth=("relay.internal; none", f"{PROVIDER}; dkim=pass; dmarc=pass")))
    assert len(poller(box).poll_once()) == 1


def test_dmarc_can_be_disabled_for_private_mail_servers(box):
    box.add(mail(auth=()))
    assert len(poller(box, require_dmarc=False).poll_once()) == 1


def test_a_message_is_captured_once_even_if_marking_read_fails(box):
    box.add(mail(message_id="<once@example.com>"))
    p = poller(box)
    box.fail_store = True
    with pytest.raises(ConnectionError):
        p.poll_once()
    box.fail_store = False
    p.poll_once()  # the same message comes back unread...
    assert len(p.store.list()) == 1  # ...but is not captured twice


def test_a_message_that_keeps_failing_is_given_up_on(box, monkeypatch, caplog):
    import logging

    uid = box.add(mail())
    p = poller(box)

    def broken(*args):
        raise LookupError("unknown encoding: x-unknown")

    monkeypatch.setattr(p.store, "add_once", broken)
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            p.poll_once()
    assert box.seen == {uid}  # not retried forever
    assert METRICS.get("maestro_email_given_up_total") == 1
    assert "Giving up on email" in caplog.text


def test_messages_without_message_id_are_deduplicated_by_content(box):
    raw = mail()
    raw = b"\n".join(line for line in raw.split(b"\n") if not line.startswith(b"Message-ID"))
    box.add(raw)
    p = poller(box)
    box.fail_store = True
    with pytest.raises(ConnectionError):
        p.poll_once()
    box.fail_store = False
    p.poll_once()
    assert len(p.store.list()) == 1


def test_mailbox_errors_are_clear_and_close_the_connection(box):
    with pytest.raises(Exception, match="cannot open folder 'Reminders'"):
        poller(box, folder="Reminders").fetch()
    box.add(mail())
    box.read_only = True
    with pytest.raises(Exception, match="could not mark messages read"):
        poller(box).poll_once()
    box.read_only = False
    box.wrong_password = True
    closed = box.closed
    with pytest.raises(ConnectionError):
        poller(box).fetch()
    assert box.closed == closed + 1  # socket closed even though login failed


def test_failures_leave_the_message_unread_for_retry(box, monkeypatch):
    uid = box.add(mail())
    p = poller(box)
    original = p.store.add_once
    def locked(*args):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(p.store, "add_once", locked)
    assert p.poll_once() == []
    assert box.seen == set()  # not lost
    monkeypatch.setattr(p.store, "add_once", original)
    assert [i.text for i in p.poll_once()] == ["Dentist"]
    assert box.seen == {uid}


def test_capture_text_is_bounded():
    message = EmailMessage()
    message["Subject"] = "x" * 5_000
    assert len(capture_text(message)) == 500


def test_settings_from_config():
    assert email_settings(Config()) is None
    base = {"IMAP_HOST": "imap.example.com", "IMAP_USER": "maestro", "IMAP_PASSWORD": "pw",
            "MAESTRO_EMAIL_FROM": "Me@Example.com, other@example.com"}
    with pytest.raises(ValueError, match="IMAP_PASSWORD, MAESTRO_EMAIL_FROM"):
        email_settings(Config(imap_host="imap.example.com", imap_user="maestro"))
    with pytest.raises(ValueError, match="MAESTRO_EMAIL_AUTHSERV_ID"):
        email_settings(Config.from_env(base))  # DMARC on by default needs the provider's id
    with pytest.raises(ValueError, match="at least 10"):
        email_settings(Config.from_env({**base, "MAESTRO_EMAIL_AUTHSERV_ID": PROVIDER,
                                        "MAESTRO_EMAIL_POLL_SECONDS": "0"}))
    settings = email_settings(Config.from_env({**base, "MAESTRO_EMAIL_REQUIRE_DMARC": "false"}))
    assert settings.allowed_senders == {"me@example.com", "other@example.com"}
    assert settings.require_dmarc is False and settings.port == 993 and settings.poll_seconds == 60


def test_doctor(box, monkeypatch, tmp_path):
    from maestro import doctor, email_in

    config = Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                    imap_host="imap.example.com", imap_user="maestro", imap_password="pw", email_from=[OWNER],
                    email_authserv_id=PROVIDER)
    lines = []
    doctor.run(config, out=lines.append)
    assert any(line.startswith("✓ email-in: maestro on imap.example.com") for line in lines)

    box.add(mail())
    monkeypatch.setattr(email_in, "_connect", lambda _: box)
    lines = []
    doctor.run(config, send_test=True, out=lines.append)
    assert "✓ email-in: connected to imap.example.com, 1 unread" in lines
    assert box.seen == set()  # doctor never marks mail read...
    assert box.fetches == 0  # ...and only counts, without downloading anything


def test_background_worker_never_blocks_the_caller(box):
    import time

    from maestro.email_in import EmailWorker

    box.add(mail())
    worker = EmailWorker(poller(box))
    started = time.monotonic()
    assert worker.process() == []  # nothing fetched yet: returns at once
    assert time.monotonic() - started < 0.1
    worker.start()
    try:
        items = []
        for _ in range(200):
            items = worker.process()
            if items:
                break
            time.sleep(0.01)
        assert [i.text for i in items] == ["Dentist"]
        for _ in range(200):  # the worker marks it read after the hand-back
            if box.seen:
                break
            time.sleep(0.01)
        assert box.seen == {"1"}
    finally:
        worker.stop()


def test_server_polls_the_inbox(box):
    pytest.importorskip("fastapi")
    import time

    from fastapi.testclient import TestClient

    from maestro.api import create_app
    from maestro.email_in import EmailWorker

    box.add(mail())
    p = poller(box)
    with TestClient(create_app(p.store, email=EmailWorker(p))):
        for _ in range(200):
            if p.store.list():
                break
            time.sleep(0.02)
    assert [i.text for i in p.store.list()] == ["Dentist"]


def test_messages_without_text_are_ignored_and_marked_read(box):
    uid = box.add(mail(subject="", body=" \n "))
    assert poller(box).poll_once() == []
    assert box.seen == {uid}


def test_failed_search_is_an_error(box, monkeypatch):
    real = box.uid

    def busy_search(command, *args):
        return ("NO", [b"busy"]) if command == "SEARCH" else real(command, *args)

    monkeypatch.setattr(box, "uid", busy_search)
    with pytest.raises(Exception, match="SEARCH failed"):
        poller(box).fetch()


def test_worker_keeps_running_after_a_failed_poll(box, caplog):
    import logging
    import time

    from maestro.email_in import EmailWorker

    box.wrong_password = True
    worker = EmailWorker(poller(box))
    with caplog.at_level(logging.ERROR):
        worker.start()
        try:
            for _ in range(200):
                if "Email polling failed" in caplog.text:
                    break
                time.sleep(0.01)
            assert worker._thread.is_alive()  # logged and carried on
        finally:
            worker.stop()
    assert "Email polling failed" in caplog.text


def test_unprocessed_mail_stays_unread(box, caplog):
    import logging

    from maestro.email_in import EmailWorker

    box.add(mail())
    worker = EmailWorker(poller(box), handoff_timeout=0.05)
    with caplog.at_level(logging.WARNING):
        worker._cycle()  # nobody calls process(): the hand-over times out
    assert box.seen == set()
    assert "stays unread for the next poll" in caplog.text


def test_doctor_email_failures(box, monkeypatch, tmp_path):
    from maestro import doctor, email_in

    def run(**overrides):
        fields = dict(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                      imap_host="imap.example.com", imap_user="maestro", imap_password="pw",
                      email_from=[OWNER], email_authserv_id=PROVIDER)
        lines = []
        doctor.run(Config(**{**fields, **overrides}), send_test=True, out=lines.append)
        return [line for line in lines if "email-in" in line]

    assert run(email_authserv_id=None)[0].startswith("✗ email-in: email-in needs MAESTRO_EMAIL_AUTHSERV_ID")
    monkeypatch.setattr(email_in, "_connect", lambda _: box)
    lines = run(email_require_dmarc=False, email_authserv_id=None)
    assert lines[0].startswith("! email-in: MAESTRO_EMAIL_REQUIRE_DMARC=false")
    box.wrong_password = True
    assert run()[0].startswith("✗ email-in: IMAP login/fetch failed: ConnectionError")


def test_cli_run_starts_and_stops_the_email_worker(tmp_path, monkeypatch, capsys):
    from maestro import cli, email_in

    box = FakeMailbox()
    box.add(mail())
    monkeypatch.setattr(email_in, "_connect", lambda _: box)
    env = {"MAESTRO_CHANNELS": "console", "IMAP_HOST": "imap.example.com", "IMAP_USER": "maestro",
           "IMAP_PASSWORD": "pw", "MAESTRO_EMAIL_FROM": OWNER, "MAESTRO_EMAIL_AUTHSERV_ID": PROVIDER,
           "MAESTRO_EMAIL_POLL_SECONDS": "10"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def two_ticks(store, notifier, interval, wait, escalation=None):
        import time

        for _ in range(200):  # give the worker time to fetch, then let wait() capture
            wait(0)
            if store.list():
                break
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_forever", two_ticks)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    db = str(tmp_path / "m.db")
    assert cli.main(["--db", db, "run"]) == 0
    from maestro.store import Store

    with Store(db) as store:
        assert [i.text for i in store.list()] == ["Dentist"]

    monkeypatch.delenv("MAESTRO_EMAIL_AUTHSERV_ID")
    assert cli.main(["--db", db, "run"]) == 2
    assert "MAESTRO_EMAIL_AUTHSERV_ID" in capsys.readouterr().err
