"""#17: reminder emails over SMTP, and acting on them by replying."""

import re
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
    assert re.fullmatch(r"<reminder-7-1793091600-[0-9a-f]{8}@maestro>", message["Message-ID"])
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
    assert smtp.sent[0]["Message-ID"].startswith(f"<reminder-{reminder.id}-")  # what a reply will quote
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


def test_each_send_gets_a_new_message_id():
    # Escalation re-sends the same occurrence; providers drop a repeated Message-ID.
    smtp = FakeSMTP()
    sender = EmailNotifier(Config(**CONFIG), connect=lambda _: smtp)
    sender.send(item())
    sender.send(item())
    assert smtp.sent[0]["Message-ID"] != smtp.sent[1]["Message-ID"]


def test_multi_line_reminders_can_be_emailed():
    smtp = FakeSMTP()
    EmailNotifier(Config(**CONFIG), connect=lambda _: smtp).send(
        Item(id=1, text="Buy\nmilk\r\n at 5pm", kind=Kind.REMINDER, created_at=datetime.now(UTC)))
    assert smtp.sent[0]["Subject"] == "Reminder: Buy milk at 5pm"


def test_default_port_follows_security():
    assert notifier_module.smtp_port(Config(**CONFIG, smtp_security="ssl")) == 465
    assert notifier_module.smtp_port(Config(**CONFIG)) == 587
    assert notifier_module.smtp_port(Config(**CONFIG, smtp_security="ssl", smtp_port=2465)) == 2465
    assert Config.from_env({"SMTP_SECURITY": "ssl"}).smtp_port is None


def test_failed_starttls_closes_the_socket(monkeypatch):
    closed = []

    class NoTls:
        def __init__(self, host, port, timeout):
            pass

        def starttls(self, context):
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(notifier_module.smtplib, "SMTP", NoTls)
    with pytest.raises(smtplib.SMTPNotSupportedError):
        notifier_module._smtp(Config(**CONFIG))
    assert closed == [True]


def test_half_set_credentials_are_a_config_error():
    with pytest.raises(ConfigError, match="both SMTP_USER and SMTP_PASSWORD"):
        CHANNELS["email"](Config(**{**CONFIG, "smtp_password": None}))


def test_replies_about_an_earlier_occurrence_are_ignored(setup):
    from datetime import timedelta as td

    store, poller, sent_as = setup
    # The reminder has been delivered again since (e.g. a daily one): an old email's reply is stale.
    later = store.get(sent_as.id).due_at + td(days=1)
    store.reschedule(sent_as.id, later)
    fire_due(store, MultiNotifier({"push": FakePush()}), now=later)
    handle(poller, reply(sent_as, "done"))
    assert store.get(sent_as.id).status != Status.DONE


class FakePush:
    def send(self, item):
        pass


def test_a_number_that_disagrees_with_the_reply_is_refused(setup):
    store, poller, sent_as = setup
    other = store.add(Item(text="Pay rent", kind=Kind.REMINDER, created_at=datetime.now(UTC)))
    handle(poller, reply(sent_as, f"done {other.id}"))
    assert store.get(sent_as.id).status != Status.DONE
    assert store.get(other.id).status != Status.DONE


def test_a_failed_action_is_retried_not_lost(setup, monkeypatch):
    import sqlite3

    store, poller, sent_as = setup
    raw = reply(sent_as, "done")
    real = store.complete

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "complete", locked)
    assert not handle(poller, raw).done  # stays unread
    monkeypatch.setattr(store, "complete", real)
    assert handle(poller, raw).done  # the retry acts
    assert store.get(sent_as.id).status == Status.DONE


def test_html_only_replies_work(setup):
    store, poller, sent_as = setup
    message = EmailMessage()
    message["Authentication-Results"] = "mx.example.com; dmarc=pass"
    message["From"] = "me@example.com"
    message["Subject"] = "Re: Reminder: Call mom"
    message["In-Reply-To"] = reminder_message_id(sent_as)
    message.set_content("<div>Done</div><blockquote>Call mom</blockquote>", subtype="html")
    handle(poller, bytes(message))
    assert store.get(sent_as.id).status == Status.DONE
