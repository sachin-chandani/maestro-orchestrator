"""Email-in: send or forward an email to a dedicated inbox and it is captured.

Maestro polls the inbox over IMAP (IMAP_HOST, IMAP_USER, IMAP_PASSWORD; IMAP_FOLDER defaults to
INBOX). The subject is captured ("Re:"/"Fwd:" stripped); if it's empty, the first line of the body.

Who may capture: only senders in MAESTRO_EMAIL_FROM, and, because the From header is trivially
forged, only when an Authentication-Results header from **your provider** (its authserv-id, set in
MAESTRO_EMAIL_AUTHSERV_ID, e.g. mx.google.com) reports `dmarc=pass`. Headers from any other server
are ignored: a sender can write those (ADR 0008). Set MAESTRO_EMAIL_REQUIRE_DMARC=false only for a
private mail server that doesn't add the header.

Each message is captured once (per Message-ID, or a hash of the message when it has none), so a
failure between saving and marking it read can't create duplicates. A message that keeps failing is
given up on after MAX_FAILURES polls. IMAP runs in a background worker (EmailWorker) so a slow mail
server never delays the scheduler; captures happen on the thread that owns the store (ADR 0003).
"""

import email
import email.policy
import hashlib
import html
import imaplib
import logging
import queue
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol

from maestro import commands
from maestro.models import Item
from maestro.notifier import REMINDER_MESSAGE_ID_RE
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.store import Store

log = logging.getLogger(__name__)

_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw|wg)\s*:\s*)+", re.I)
MAX_TEXT = 500


class Mailbox(Protocol):
    """The slice of imaplib.IMAP4 that the poller uses (so tests can pass a fake)."""

    def login(self, user: str, password: str) -> object: ...
    def select(self, mailbox: str = ...) -> tuple[str, list]: ...
    def uid(self, command: str, *args: str) -> tuple[str, list]: ...
    def logout(self) -> object: ...


@dataclass
class EmailSettings:
    host: str
    user: str
    password: str
    allowed_senders: frozenset[str]
    folder: str = "INBOX"
    port: int = 993
    require_dmarc: bool = True
    authserv_id: str | None = None  # your provider's id in Authentication-Results, e.g. mx.google.com
    poll_seconds: int = 60


MAX_FAILURES = 3  # polls a message may fail before it's given up on (marked read, logged)
IMAP_TIMEOUT = 20


class MailboxError(RuntimeError):
    pass


def _connect(settings: EmailSettings) -> Mailbox:
    return imaplib.IMAP4_SSL(settings.host, settings.port, timeout=IMAP_TIMEOUT)


@dataclass
class Fetched:
    uid: str
    raw: bytes


@dataclass
class Outcome:
    item: Item | None
    # True when the message is dealt with (captured, or deliberately ignored) and can be marked read.
    # False on an unexpected failure, so it stays unread and is retried on the next poll.
    done: bool


def body_lines(message: EmailMessage) -> list[str]:
    """Non-empty lines of the plain-text body, or of the HTML body with tags removed when there is no
    plain part (some mobile clients send HTML only)."""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return []
    content = str(body.get_content())
    if body.get_content_type() == "text/html":
        content = re.sub(r"(?i)<br\s*/?>|</(?:p|div|blockquote)>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", "", content))
    return [line.strip() for line in content.splitlines() if line.strip()]


def capture_text(message: EmailMessage) -> str:
    """The subject without reply/forward prefixes, or the first line of the body."""
    subject = _REPLY_PREFIX_RE.sub("", str(message.get("Subject", ""))).strip()
    if subject:
        return subject[:MAX_TEXT]
    lines = body_lines(message)
    return lines[0][:MAX_TEXT] if lines else ""


def _authserv_id(header: str) -> str:
    return header.split(";", 1)[0].strip().lower()


def is_authorised(message: EmailMessage, allowed: frozenset[str], require_dmarc: bool,
                  authserv_id: str | None = None) -> tuple[bool, str]:
    """(allowed, reason).

    The trusted Authentication-Results header is the topmost one written by your provider
    (`authserv_id`). Your provider adds it above everything else, so a header the sender forged with
    the same id sits below it and is never the first match; headers with other ids are ignored.
    """
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    if sender not in allowed:
        return False, f"sender {sender or '(none)'} is not in MAESTRO_EMAIL_FROM"
    if require_dmarc:
        headers = [str(h) for h in message.get_all("Authentication-Results") or []]
        ours = next((h for h in headers if _authserv_id(h) == (authserv_id or "").lower()), None)
        if ours is None:
            return False, f"no Authentication-Results from {authserv_id} (From may be forged)"
        if not re.search(r"\bdmarc=pass\b", ours.lower()):
            return False, f"{authserv_id} did not report dmarc=pass (From may be forged)"
    return True, ""


def first_reply_line(message: EmailMessage) -> str:
    """The first line someone typed in a reply: skips quoted ('>') lines, stops at the usual
    'On ... wrote:' quote header."""
    for line in body_lines(message):
        if line.startswith(">"):
            continue
        if re.match(r"^On .+wrote:$", line):
            break
        return line
    return ""


def dedupe_key(message: EmailMessage, raw: bytes) -> str:
    """Message-ID, or a hash of the message: IMAP UIDs are reused across folders and mailbox rebuilds."""
    message_id = str(message.get("Message-ID", "")).strip()
    return message_id or f"sha256:{hashlib.sha256(raw).hexdigest()}"


class EmailPoller:
    def __init__(self, store: Store, settings: EmailSettings,
                 connect: Callable[[EmailSettings], Mailbox] | None = None):
        self.store = store
        self.settings = settings
        self.connect = connect or _connect  # looked up at construction, so it can be swapped in tests
        self.failures: dict[str, int] = {}  # by message hash: how many polls each has failed

    # --- network (any thread) -----------------------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Mailbox]:
        """A logged-in, selected mailbox; always logged out (or at least closed) afterwards."""
        box = self.connect(self.settings)
        try:
            box.login(self.settings.user, self.settings.password)
            status, data = box.select(self.settings.folder)
            if status != "OK":
                raise MailboxError(f"cannot open folder {self.settings.folder!r}: {data!r}")
            yield box
        finally:
            try:
                box.logout()
            except Exception:  # e.g. login failed: close the socket anyway
                shutdown = getattr(box, "shutdown", None)
                if shutdown:
                    shutdown()

    @staticmethod
    def unseen_uids(box: Mailbox) -> list[str]:
        status, data = box.uid("SEARCH", "UNSEEN")
        if status != "OK":
            raise MailboxError(f"SEARCH failed: {data!r}")
        if not data or not data[0]:
            return []
        return [u.decode() if isinstance(u, bytes) else str(u) for u in data[0].split()]

    def fetch_from(self, box: Mailbox) -> list[Fetched]:
        """Unread messages, without marking them read (BODY.PEEK)."""
        fetched = []
        for uid in self.unseen_uids(box):
            status, parts = box.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
            if status == "OK" and isinstance(raw, bytes):
                fetched.append(Fetched(uid, raw))
        return fetched

    @staticmethod
    def mark_read_in(box: Mailbox, uids: list[str]) -> None:
        if not uids:
            return
        status, data = box.uid("STORE", ",".join(uids), "+FLAGS", r"(\Seen)")
        if status != "OK":
            raise MailboxError(f"could not mark messages read (is the folder read-only?): {data!r}")

    def count_unread(self) -> int:
        with self.session() as box:
            return len(self.unseen_uids(box))

    def fetch(self) -> list[Fetched]:
        with self.session() as box:
            return self.fetch_from(box)

    def mark_read(self, uids: list[str]) -> None:
        with self.session() as box:
            self.mark_read_in(box, uids)

    # --- store work (event loop thread) --------------------------------------------------------

    def handle(self, fetched: Fetched) -> Outcome:
        """Capture one message if allowed. Never raises: a failure is logged and the message is left
        unread for the next poll."""
        try:
            message = email.message_from_bytes(fetched.raw, policy=email.policy.default)
            settings = self.settings
            allowed, reason = is_authorised(message, settings.allowed_senders, settings.require_dmarc,
                                            settings.authserv_id)
            if not allowed:
                log.warning("Ignoring email %s: %s", fetched.uid, reason)
                return Outcome(None, done=True)
            if reply := REMINDER_MESSAGE_ID_RE.search(
                f"{message.get('In-Reply-To', '')} {message.get('References', '')}"
            ):
                occurrence = datetime.fromtimestamp(int(reply[2]), UTC)
                key = dedupe_key(message, fetched.raw)
                return self._reply(message, int(reply[1]), occurrence, key, fetched.uid)
            text = capture_text(message)
            if not text:
                log.info("Ignoring email %s: no subject or text", fetched.uid)
                return Outcome(None, done=True)
            item, created = self.store.add_once("email", dedupe_key(message, fetched.raw),
                                                parse(text, tz=self.store.tz))
            if created:
                METRICS.inc("maestro_captures_total", source="email")
            return Outcome(item, done=True)
        except Exception:
            key = hashlib.sha256(fetched.raw).hexdigest()
            self.failures[key] = self.failures.get(key, 0) + 1
            if self.failures[key] >= MAX_FAILURES:
                log.exception("Giving up on email %s after %d failures; marking it read", fetched.uid,
                              MAX_FAILURES)
                METRICS.inc("maestro_email_given_up_total")
                return Outcome(None, done=True)
            log.exception("Failed to handle email %s; leaving it unread to retry", fetched.uid)
            return Outcome(None, done=False)

    def _reply(self, message: EmailMessage, item_id: int, occurrence: datetime, key: str,
               uid: str) -> Outcome:
        """A reply to a reminder email: its first line is a command for that reminder ('done',
        'snooze 1h'). Anything else is ignored rather than captured as a new item.

        Only acts if the email was about the item's latest delivered occurrence: replying 'done' on
        Wednesday to Monday's email of a daily reminder must not end the series."""
        command = commands.parse_command(first_reply_line(message))
        if command is None or command.action not in ("done", "snooze"):
            log.info("Ignoring reply %s to reminder #%s: no done/snooze on the first line", uid, item_id)
            return Outcome(None, done=True)
        if command.item_id is not None and command.item_id != item_id:
            log.warning("Ignoring reply %s: it answers reminder #%s but says %s %s", uid, item_id,
                        command.action, command.item_id)
            return Outcome(None, done=True)
        if self.store.is_claimed("email-reply", key):
            return Outcome(None, done=True)  # already handled on an earlier poll
        latest = self.store.latest_delivered_occurrence(item_id)
        if latest is None or int(latest.timestamp()) != int(occurrence.timestamp()):
            log.info("Ignoring reply %s: it is about an earlier occurrence of reminder #%s", uid, item_id)
            return Outcome(None, done=True)
        command.item_id = item_id
        result = commands.run(self.store, command, channel="email")
        self.store.claim_inbound("email-reply", key, item_id)  # only after the action succeeded
        log.info("Email reply %s on reminder #%s: %s", uid, item_id, result)
        return Outcome(self.store.get(item_id), done=True)

    def handle_all(self, fetched: list[Fetched]) -> tuple[list[Item], list[str]]:
        """Captured items, and the uids that can be marked read."""
        outcomes = [(f.uid, self.handle(f)) for f in fetched]
        return [o.item for _, o in outcomes if o.item], [uid for uid, o in outcomes if o.done]

    def poll_once(self) -> list[Item]:
        """Fetch, capture and mark read in one session, on the calling thread."""
        with self.session() as box:
            items, done = self.handle_all(self.fetch_from(box))
            self.mark_read_in(box, done)
        return items


class EmailWorker:
    """Runs IMAP in a background thread so a slow or unreachable mail server never delays the
    scheduler. The thread that owns the store calls `process()` regularly; it never blocks.

    Each cycle uses one IMAP session: fetch unread, hand them over, wait for `process()` to say
    which are done, mark those read.
    """

    def __init__(self, poller: EmailPoller, handoff_timeout: float = 120.0):
        self.poller = poller
        self.handoff_timeout = handoff_timeout
        self._fetched: queue.Queue[list[Fetched]] = queue.Queue(maxsize=1)
        self._done: queue.Queue[list[str]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="maestro-email", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception:
                log.exception("Email polling failed; retrying in %ss", self.poller.settings.poll_seconds)
            self._stop.wait(self.poller.settings.poll_seconds)

    def _cycle(self) -> None:
        with self.poller.session() as box:
            fetched = self.poller.fetch_from(box)
            if not fetched:
                return
            self._fetched.put(fetched)
            try:
                done = self._done.get(timeout=self.handoff_timeout)
            except queue.Empty:
                log.warning("Fetched mail was not processed in time; it stays unread for the next poll")
                return
            self.poller.mark_read_in(box, done)

    def process(self) -> list[Item]:
        """Capture whatever the worker has fetched. Call from the thread that owns the store."""
        try:
            fetched = self._fetched.get_nowait()
        except queue.Empty:
            return []
        items, done = self.poller.handle_all(fetched)
        self._done.put(done)
        return items
