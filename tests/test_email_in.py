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
        self.fail_store = False

    def add(self, raw: bytes) -> str:
        uid = str(len(self.messages) + 1)
        self.messages[uid] = raw
        return uid

    def login(self, user, password):
        self.logins.append((user, password))

    def select(self, mailbox="INBOX"):
        assert mailbox == "INBOX"

    def uid(self, command, *args):
        if command == "SEARCH":
            unseen = [u for u in self.messages if u not in self.seen]
            return "OK", [" ".join(unseen).encode()]
        if command == "FETCH":
            uid = args[0]
            raw = self.messages[uid]
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            if self.fail_store:
                raise ConnectionError("connection reset")
            self.seen.update(args[0].split(","))
            return "OK", []
        raise AssertionError(command)

    def logout(self):
        pass


def mail(subject="dentist next tuesday at 3pm", sender=OWNER, auth=("mx.example.com; dmarc=pass",),
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


def poller(box, require_dmarc=True):
    settings = EmailSettings(host="imap.example.com", user="maestro", password="pw",
                             allowed_senders=frozenset({OWNER}), require_dmarc=require_dmarc)
    return EmailPoller(Store(), settings, connect=lambda _: box)


def test_captures_and_marks_read(box):
    uid = box.add(mail())
    p = poller(box)
    items = p.poll_once()
    assert [i.text for i in items] == ["Dentist"]
    assert box.seen == {uid}
    assert box.logins == [("maestro", "pw"), ("maestro", "pw")]
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


def test_forged_sender_is_rejected(box):
    # The receiving server says DMARC failed; the sender added its own "pass" header further down.
    box.add(mail(auth=("mx.example.com; dmarc=fail header.from=example.com", "evil; dmarc=pass")))
    box.add(mail(auth=(), subject="no auth header at all"))
    assert poller(box).poll_once() == []


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
    with pytest.raises(ValueError, match="IMAP_PASSWORD, MAESTRO_EMAIL_FROM"):
        email_settings(Config(imap_host="imap.example.com", imap_user="maestro"))
    config = Config.from_env({"IMAP_HOST": "imap.example.com", "IMAP_USER": "maestro", "IMAP_PASSWORD": "pw",
                              "MAESTRO_EMAIL_FROM": "Me@Example.com, other@example.com",
                              "MAESTRO_EMAIL_REQUIRE_DMARC": "false"})
    settings = email_settings(config)
    assert settings.allowed_senders == {"me@example.com", "other@example.com"}
    assert settings.require_dmarc is False and settings.port == 993


def test_doctor(box, monkeypatch, tmp_path):
    from maestro import doctor, email_in

    config = Config(db_path=str(tmp_path / "m.db"), channels=["console"], api_token="t",
                    imap_host="imap.example.com", imap_user="maestro", imap_password="pw", email_from=[OWNER])
    lines = []
    doctor.run(config, out=lines.append)
    assert any(line.startswith("✓ email-in: maestro on imap.example.com") for line in lines)

    box.add(mail())
    monkeypatch.setattr(email_in, "_connect", lambda _: box)
    lines = []
    doctor.run(config, send_test=True, out=lines.append)
    assert "✓ email-in: connected to imap.example.com, 1 unread" in lines
    assert box.seen == set()  # doctor never marks mail read


def test_server_polls_the_inbox(box):
    pytest.importorskip("fastapi")
    import time

    from fastapi.testclient import TestClient

    from maestro.api import create_app

    box.add(mail())
    p = poller(box)
    with TestClient(create_app(p.store, email=p, email_interval=0.05)):
        for _ in range(100):
            if p.store.list():
                break
            time.sleep(0.02)
    assert [i.text for i in p.store.list()] == ["Dentist"]
