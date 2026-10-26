"""#17: reminder emails over SMTP, and acting on them by replying."""

import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from maestro import notifier as notifier_module
from maestro.config import Config
from maestro.models import Item, Kind, Status
from maestro.notifier import CHANNELS, ConfigError, EmailNotifier, MultiNotifier, reminder_message_id
from maestro.scheduler import fire_due
from maestro.store import Store

CONFIG = dict(smtp_host="smtp.example.com", smtp_from="maestro@example.com", email_to="me@example.com",
              smtp_user="maestro", smtp_password="pw")


class FakeSMTP:
    def __init__(self, fail_quit=False):
        self.logins, self.sent, self.closed = [], [], False
        self.fail_quit = fail_quit

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        if self.fail_quit:
            raise smtplib.SMTPServerDisconnected("gone")
        self.closed = True

    def close(self):
        self.closed = True


def item(due=datetime(2026, 10, 27, 9, 0, tzinfo=UTC)):
    return Item(id=7, text="Call mom", kind=Kind.REMINDER, created_at=due, due_at=due)


def test_reminder_email(monkeypatch):
    smtp = FakeSMTP()
    EmailNotifier(Config(**CONFIG, public_url="https://maestro.example.com", imap_host="imap.example.com"),
                  connect=lambda _: smtp).send(item())
    (message,) = smtp.sent
    assert message["Subject"] == "Reminder: Call mom"
    assert (message["From"], message["To"]) == ("maestro@example.com", "me@example.com")
    assert message["Message-ID"] == "<reminder-7-1793091600@maestro>"
    body = message.get_content()
    assert "Reply 'done', or 'snooze 1h'" in body and "https://maestro.example.com/" in body
    assert smtp.logins == [("maestro", "pw")] and smtp.closed


def test_no_reply_hint_without_email_in_and_no_login_without_credentials():
    smtp = FakeSMTP()
    anonymous = Config(**{**CONFIG, "smtp_user": None, "smtp_password": None})
    EmailNotifier(anonymous, connect=lambda _: smtp).send(item())
    assert "Reply" not in smtp.sent[0].get_content()
    assert smtp.logins == []


def test_connection_is_closed_even_if_quit_fails():
    smtp = FakeSMTP(fail_quit=True)
    EmailNotifier(Config(**CONFIG), connect=lambda _: smtp).send(item())
    assert smtp.closed


@pytest.mark.parametrize("overrides, error", [
    ({"smtp_host": None}, "email channel needs SMTP_HOST"),
    ({"email_to": None}, "MAESTRO_EMAIL_TO"),
    ({"smtp_security": "tls1.0"}, "SMTP_SECURITY must be starttls, ssl or none"),
    ({"smtp_security": "none"}, "clear text"),
])
def test_channel_config_errors(overrides, error):
    with pytest.raises(ConfigError, match=error):
        CHANNELS["email"](Config(**{**CONFIG, **overrides}))


@pytest.mark.parametrize("security, expected", [
    ("ssl", "SMTP_SSL"), ("starttls", "SMTP+starttls"), ("none", "SMTP"),
])
def test_connection_security(monkeypatch, security, expected):
    opened = []

    class Plain:
        def __init__(self, host, port, timeout):
            opened.append("SMTP")

        def starttls(self, context):
            opened[-1] += "+starttls"

    class Secure:
        def __init__(self, host, port, timeout, context):
            opened.append("SMTP_SSL")

    monkeypatch.setattr(notifier_module.smtplib, "SMTP", Plain)
    monkeypatch.setattr(notifier_module.smtplib, "SMTP_SSL", Secure)
    notifier_module._smtp(Config(**CONFIG, smtp_security=security))
    assert opened == [expected]


# --- replying to a reminder email (with email-in) ----------------------------------------------

def reply(to: Item, body: str, message_id="<reply-1@example.com>") -> bytes:
    message = EmailMessage()
    message["Authentication-Results"] = "mx.example.com; dmarc=pass"
    message["From"] = "me@example.com"
    message["Subject"] = f"Re: Reminder: {to.text}"
    message["Message-ID"] = message_id
    message["In-Reply-To"] = reminder_message_id(to)
    message["References"] = reminder_message_id(to)
    message.set_content(body)
    return bytes(message)


@pytest.fixture
def setup():
    """A reminder delivered by email a moment ago, and an email-in poller to receive replies."""
    from maestro.email_in import EmailPoller, EmailSettings

    store = Store()
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(minutes=1)
    reminder = store.add(Item(text="Call mom", kind=Kind.REMINDER, created_at=due, due_at=due))
    smtp = FakeSMTP()
    email = EmailNotifier(Config(**CONFIG), connect=lambda _: smtp)
    fire_due(store, MultiNotifier({"email": email}), now=now)
    assert smtp.sent[0]["Message-ID"] == reminder_message_id(reminder)  # what a reply will quote
    settings = EmailSettings(host="imap", user="u", password="p", authserv_id="mx.example.com",
                             allowed_senders=frozenset({"me@example.com"}))
    return store, EmailPoller(store, settings), reminder


def handle(poller, raw):
    from maestro.email_in import Fetched

    return poller.handle(Fetched("1", raw))


def test_replying_done_completes_that_reminder(setup):
    store, poller, sent_as = setup
    body = "Done\n\nOn Mon, 27 Oct 2026 at 09:00, Maestro wrote:\n> Call mom"
    outcome = handle(poller, reply(sent_as, body))
    assert outcome.done
    assert store.get(sent_as.id).status == Status.DONE
    via = store.conn.execute("SELECT via FROM acknowledgements").fetchone()[0]
    assert via == "email"
    assert len(store.list(include_done=True)) == 1  # the reply wasn't captured as a new note


def test_replying_snooze_acts_once_even_if_the_reply_is_seen_again(setup):
    store, poller, sent_as = setup
    raw = reply(sent_as, "snooze 1h")
    handle(poller, raw)
    first = store.get(sent_as.id).due_at
    handle(poller, raw)  # e.g. marking it read failed, so it's fetched again
    assert store.get(sent_as.id).due_at == first


def test_other_replies_are_ignored_not_captured(setup):
    store, poller, sent_as = setup
    assert handle(poller, reply(sent_as, "thanks!\n\n> Call mom")).done
    assert len(store.list(include_done=True)) == 1
    assert store.get(sent_as.id).status != Status.DONE
