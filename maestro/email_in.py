"""Email-in: send or forward an email to a dedicated inbox and it is captured.

Maestro polls the inbox over IMAP (IMAP_HOST, IMAP_USER, IMAP_PASSWORD; IMAP_FOLDER defaults to
INBOX). The subject is captured ("Re:"/"Fwd:" stripped); if it's empty, the first line of the body.

Who may capture: only senders in MAESTRO_EMAIL_FROM, and, because the From header is trivially
forged, only when the receiving mail provider reports `dmarc=pass` in the **topmost**
Authentication-Results header (the one it added; lower ones can be written by the sender). Set
MAESTRO_EMAIL_REQUIRE_DMARC=false only for a private mail server that doesn't add that header.

Each message is captured once per Message-ID, so a failure between saving and marking it read can't
create duplicates. Network work (fetch, mark read) and store work (parse, save) are separate steps so
the server can run the first in a thread and the second on its event loop (ADR 0003).
"""

import email
import email.policy
import imaplib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol

from maestro.models import Item
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.store import Store

log = logging.getLogger(__name__)

_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw|wg)\s*:\s*)+", re.I)
MAX_TEXT = 500


class Mailbox(Protocol):
    """The slice of imaplib.IMAP4 that the poller uses (so tests can pass a fake)."""

    def login(self, user: str, password: str) -> object: ...
    def select(self, mailbox: str = ...) -> object: ...
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


def _connect(settings: EmailSettings) -> Mailbox:
    return imaplib.IMAP4_SSL(settings.host, settings.port, timeout=30)


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


def capture_text(message: EmailMessage) -> str:
    """The subject without reply/forward prefixes, or the first non-empty line of the plain body."""
    subject = _REPLY_PREFIX_RE.sub("", str(message.get("Subject", ""))).strip()
    if subject:
        return subject[:MAX_TEXT]
    body = message.get_body(preferencelist=("plain",))
    content = body.get_content() if body is not None else ""
    for line in str(content).splitlines():
        if line.strip():
            return line.strip()[:MAX_TEXT]
    return ""


def is_authorised(message: EmailMessage, allowed: frozenset[str], require_dmarc: bool) -> tuple[bool, str]:
    """(allowed, reason). Only the topmost Authentication-Results header is trusted."""
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    if sender not in allowed:
        return False, f"sender {sender or '(none)'} is not in MAESTRO_EMAIL_FROM"
    if require_dmarc:
        results = message.get_all("Authentication-Results") or []
        topmost = str(results[0]).lower() if results else ""
        if not re.search(r"\bdmarc=pass\b", topmost):
            return False, "no dmarc=pass from the receiving server (From may be forged)"
    return True, ""


class EmailPoller:
    def __init__(self, store: Store, settings: EmailSettings,
                 connect: Callable[[EmailSettings], Mailbox] | None = None):
        self.store = store
        self.settings = settings
        self.connect = connect or _connect  # looked up at construction, so it can be swapped in tests

    # --- network (safe in a worker thread) ------------------------------------------------------

    def fetch(self) -> list[Fetched]:
        """Unread messages, without marking them read."""
        box = self._open()
        try:
            status, data = box.uid("SEARCH", "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return []
            fetched = []
            for uid in data[0].split():
                uid_text = uid.decode() if isinstance(uid, bytes) else str(uid)
                status, parts = box.uid("FETCH", uid_text, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
                if status == "OK" and isinstance(raw, bytes):
                    fetched.append(Fetched(uid_text, raw))
            return fetched
        finally:
            box.logout()

    def mark_read(self, uids: list[str]) -> None:
        if not uids:
            return
        box = self._open()
        try:
            box.uid("STORE", ",".join(uids), "+FLAGS", r"(\Seen)")
        finally:
            box.logout()

    def _open(self) -> Mailbox:
        box = self.connect(self.settings)
        box.login(self.settings.user, self.settings.password)
        box.select(self.settings.folder)
        return box

    # --- store work (event loop thread) --------------------------------------------------------

    def handle(self, fetched: Fetched) -> Outcome:
        """Capture one message if allowed. Never raises: a failure is logged and the message is left
        unread for the next poll."""
        try:
            message = email.message_from_bytes(fetched.raw, policy=email.policy.default)
            settings = self.settings
            allowed, reason = is_authorised(message, settings.allowed_senders, settings.require_dmarc)
            if not allowed:
                log.warning("Ignoring email %s: %s", fetched.uid, reason)
                return Outcome(None, done=True)
            text = capture_text(message)
            if not text:
                log.info("Ignoring email %s: no subject or text", fetched.uid)
                return Outcome(None, done=True)
            message_id = str(message.get("Message-ID", "")).strip() or f"uid:{fetched.uid}"
            item, created = self.store.add_once("email", message_id, parse(text, tz=self.store.tz))
            if created:
                METRICS.inc("maestro_captures_total", source="email")
            return Outcome(item, done=True)
        except Exception:
            log.exception("Failed to handle email %s; leaving it unread to retry", fetched.uid)
            return Outcome(None, done=False)

    def handle_all(self, fetched: list[Fetched]) -> tuple[list[Item], list[str]]:
        """Captured items, and the uids that can be marked read."""
        outcomes = [(f.uid, self.handle(f)) for f in fetched]
        return [o.item for _, o in outcomes if o.item], [uid for uid, o in outcomes if o.done]

    def poll_once(self) -> list[Item]:
        """Fetch, capture and mark read: for `maestro run`, which has no event loop."""
        items, done = self.handle_all(self.fetch())
        self.mark_read(done)
        return items
