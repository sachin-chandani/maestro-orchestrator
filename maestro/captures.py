"""Confirm before acting on an unsure interpretation, with undo (#117, ADR 0016).

When confirmation is on (`MAESTRO_CONFIRM=unsure`; the default is `never`, so nothing changes
unless asked for) and the rules are unsure of a capture (`maestro.confidence`) or one message
asked for several things, the interpretation is stored as a *capture awaiting confirmation* —
its validated actions as JSON — instead of being applied. Nothing about it reaches `items`, so
the scheduler can never deliver something nobody agreed to.

The person answers in the channel they used: Telegram buttons, SMS "YES"/"NO", `maestro confirm`,
or the API. Confirming goes claim-then-act: one atomic check-and-set decides the winner
(`Store.confirm_capture`), then the actions are applied with a dedupe key derived from the
capture id, so a double tap or a webhook retry never creates two reminders. A confirmed capture
can be undone for ten minutes, which deletes exactly the items it created. Unanswered captures
expire (default 24 hours, or at the proposed due time if that is sooner) and stay listed —
visible, never applied.

A crash between the claim and the apply leaves a capture marked applied with no items; the
confirmation reply is only sent after both, so nothing acknowledged is lost. That ordering is
deliberate: the other order could save items for a capture that a concurrent cancel or expiry
had already decided against.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from maestro import actions as actions_
from maestro.actions import Action, Applied, apply
from maestro.confidence import Confidence
from maestro.interpret import _stamped
from maestro.models import Capture, CaptureStatus, Item
from maestro.observability import METRICS
from maestro.storage import Store

log = logging.getLogger(__name__)

DEFAULT_TTL = timedelta(hours=24)
UNDO_WINDOW = timedelta(minutes=10)
# The dedupe namespace for applying a confirmed capture. Stable across releases, so a confirm
# retried over an upgrade is still recognised (see maestro.actions.apply).
SOURCE = "capture"

# A reply that answers a confirmation: "yes", "no 12", "undo". Only consulted when the sender
# actually has a capture the word could answer, so "yes" with nothing pending stays a capture
# (ADR 0005: anything that isn't exactly a command is captured).
REPLY_RE = re.compile(r"^(?P<verb>yes|no|confirm|cancel|undo)\s*#?(?P<id>\d+)?$", re.I)
_VERBS = {"yes": "yes", "confirm": "yes", "no": "no", "cancel": "no", "undo": "undo"}


@dataclass(frozen=True)
class Confirmation:
    """A deployment's confirmation settings: present means on (`maestro.config.confirmation`)."""

    ttl: timedelta = DEFAULT_TTL


class Outcome(StrEnum):
    """What answering a capture did, for the channel to word."""

    APPLIED = "applied"  # confirmed here: the items now exist
    CANCELLED = "cancelled"  # cancelled here
    UNDONE = "undone"  # undone here: the items are gone
    ALREADY = "already"  # someone (or a double tap) decided it first; nothing done twice
    EXPIRED = "expired"  # too late: it expired unanswered
    TOO_LATE = "too_late"  # the undo window has passed
    MISSING = "missing"  # no such capture, or not this sender's


def needs_confirmation(read: list[Action], confidence: Confidence) -> bool:
    """Whether this interpretation should be confirmed rather than applied: the rules were unsure,
    or one message asked for several things (the riskiest reading, whoever produced it)."""
    return bool(confidence.doubts) or len(read) > 1


def propose(store: Store, read: list[Action], confidence: Confidence, source: str, address: str,
            text: str, ttl: timedelta = DEFAULT_TTL, dedupe_key: str | None = None,
            now: datetime | None = None) -> tuple[Capture, bool]:
    """Store an interpretation as a capture awaiting confirmation, once per provider message.

    It expires after `ttl`, or at the earliest proposed due time if that is sooner: an unanswered
    "check the oven in 1 hour" past the hour is noise, not a reminder.
    """
    now = now or datetime.now(store.tz).replace(microsecond=0)
    due = [due_at for a in read if (due_at := getattr(a, "due_at", None)) is not None]
    expires_at = min([now + ttl, *due])
    interpreter = next((name for a in read if (name := getattr(a, "interpreter", None))), None)
    capture = Capture(
        status=CaptureStatus.PENDING,
        actions=json.dumps([a.to_json() for a in read]),
        source=source, address=address, source_text=text,
        interpreter=interpreter, doubts=",".join(confidence.doubts),
        created_at=now, expires_at=expires_at,
    )
    capture, created = store.add_capture(capture, dedupe_key)
    if created:
        METRICS.inc("maestro_captures_confirmation_total", outcome="proposed")
    return capture, created


def proposed_actions(capture: Capture) -> list[Action]:
    """The stored proposal as actions, re-stamped with the interpreter that read it. The stamp
    lives in its own column because it is not part of an action's JSON form (#116)."""
    read = actions_.actions_from_json(json.loads(capture.actions))
    return _stamped(read, capture.interpreter) if capture.interpreter else read


def proposed_lines(capture: Capture) -> list[str]:
    """What confirming would save, one line per item, worded like a saved item but with no number."""
    from maestro import commands

    lines = []
    for action in proposed_actions(capture):
        if not isinstance(action, actions_.CreateReminder | actions_.CreateTask | actions_.CreateNote):
            continue  # pragma: no cover - only create actions are ever stored in a capture
        item = action.to_item(capture.created_at)
        icon = commands.KIND_ICONS.get(item.kind, "📝")
        via = f" [via {', '.join(item.channels)}]" if item.channels else ""
        lines.append(f"{icon} {item.text} ({commands.when(item)}){via}")
    return lines


def _why_not(store: Store, capture: Capture | None, now: datetime) -> Outcome:
    """Why a check-and-set lost, read back from the row's state."""
    if capture is None:
        return Outcome.MISSING
    if capture.status == CaptureStatus.PENDING and capture.expires_at <= now:
        store.expire_captures(now)  # answer first, tidy after; the CAS already said no
        return Outcome.EXPIRED
    return {CaptureStatus.EXPIRED: Outcome.EXPIRED,
            CaptureStatus.APPLIED: Outcome.ALREADY,
            CaptureStatus.CANCELLED: Outcome.ALREADY,
            CaptureStatus.UNDONE: Outcome.ALREADY}.get(capture.status, Outcome.MISSING)


def confirm(store: Store, capture_id: int, via: str,
            requestable: frozenset[str] | None = None,
            now: datetime | None = None) -> tuple[Outcome, list[Item]]:
    """Apply a pending capture, exactly once.

    Claim-then-act: the atomic claim (`Store.confirm_capture`) decides the winner, then the
    actions are applied with the capture's own dedupe key. A double tap loses the claim and gets
    ALREADY with the items the winner saved; it never applies anything itself.
    """
    now = now or datetime.now(store.tz).replace(microsecond=0)
    if not store.confirm_capture(capture_id, via, now, now + UNDO_WINDOW):
        outcome = _why_not(store, store.get_capture(capture_id), now)
        items = store.items_for_capture(capture_id) if outcome == Outcome.ALREADY else []
        return outcome, items
    capture = store.get_capture(capture_id)
    assert capture is not None  # the claim just updated it
    results: list[Applied] = apply(store, proposed_actions(capture), source=SOURCE,
                                   dedupe_key=f"capture:{capture_id}", requestable=requestable)
    items = [r.saved for r in results]
    store.link_capture(capture_id, [item.id for item in items if item.id is not None])
    # maestro_captures_total is the channel's to count: it knows its own label ("sms", not
    # "twilio_sms") and counts a direct capture in the same place.
    METRICS.inc("maestro_captures_confirmation_total", outcome="confirmed")
    log.info("Capture %d confirmed via %s: %d item(s) saved", capture_id, via, len(items))
    return Outcome.APPLIED, items


def cancel(store: Store, capture_id: int, via: str, now: datetime | None = None) -> Outcome:
    """Mark a pending capture cancelled. Nothing was saved, so there is nothing else to do."""
    now = now or datetime.now(store.tz).replace(microsecond=0)
    if not store.cancel_capture(capture_id, via, now):
        return _why_not(store, store.get_capture(capture_id), now)
    METRICS.inc("maestro_captures_confirmation_total", outcome="cancelled")
    log.info("Capture %d cancelled via %s", capture_id, via)
    return Outcome.CANCELLED


def undo(store: Store, capture_id: int, via: str,
         now: datetime | None = None) -> tuple[Outcome, int]:
    """Take back a confirmed capture inside its undo window: delete exactly the items it created
    (deliveries and acknowledgements included), with how many were removed.

    An occurrence that already fired inside the window stays sent — a message can't be unsent —
    but nothing of the item remains to fire again.
    """
    now = now or datetime.now(store.tz).replace(microsecond=0)
    if not store.undo_capture(capture_id, via, now):
        capture = store.get_capture(capture_id)
        if capture is not None and capture.status == CaptureStatus.APPLIED:
            return Outcome.TOO_LATE, 0  # applied, but the ten minutes have passed
        return _why_not(store, capture, now), 0
    removed = 0
    for item in store.items_for_capture(capture_id):
        if item.id is not None and store.delete(item.id):
            removed += 1
    METRICS.inc("maestro_captures_confirmation_total", outcome="undone")
    log.info("Capture %d undone via %s: %d item(s) removed", capture_id, via, removed)
    return Outcome.UNDONE, removed


def expire(store: Store, now: datetime | None = None) -> int:
    """The sweep: mark overdue pending captures expired so lists and metrics say so. Correctness
    doesn't depend on it running — confirming checks the clock itself."""
    now = now or datetime.now(store.tz).replace(microsecond=0)
    expired = store.expire_captures(now)
    for _ in range(expired):
        METRICS.inc("maestro_captures_confirmation_total", outcome="expired")
    if expired:
        log.info("%d unanswered capture(s) expired", expired)
    return expired


# --- matching a reply to the capture it answers -------------------------------------------------


def match_reply(text: str) -> tuple[str, int | None] | None:
    """("yes" | "no" | "undo", capture id or None) when the text is a confirmation answer."""
    if m := REPLY_RE.match(text.strip()):
        return _VERBS[m["verb"].lower()], int(m["id"]) if m["id"] else None
    return None


def target(store: Store, source: str, address: str, verb: str,
           capture_id: int | None, now: datetime | None = None) -> Capture | None:
    """The capture a reply answers, or None (in which case the text is captured as usual).

    A named id must belong to this sender on this channel: a guessed number can't confirm
    someone else's capture. A bare word means the sender's most recent pending capture — the
    question always quotes its id, so an older one can be named — or, for undo, their most
    recently applied capture still inside its window.
    """
    now = now or datetime.now(store.tz).replace(microsecond=0)
    if capture_id is not None:
        capture = store.get_capture(capture_id)
        if capture is not None and (capture.source, capture.address) == (source, address):
            return capture
        return None
    wanted = CaptureStatus.APPLIED if verb == "undo" else CaptureStatus.PENDING
    for capture in store.captures(status=str(wanted), source=source, address=address):
        if verb == "undo":
            if capture.undo_until is not None and capture.undo_until > now:
                return capture
        elif capture.expires_at > now:
            return capture
    return None


def answer(store: Store, capture: Capture, verb: str, via: str,
           requestable: frozenset[str] | None = None,
           now: datetime | None = None) -> tuple[Outcome, list[Item], int]:
    """Carry out a matched reply: (outcome, items confirmed, items removed by undo)."""
    assert capture.id is not None
    if verb == "yes":
        outcome, items = confirm(store, capture.id, via, requestable=requestable, now=now)
        return outcome, items, 0
    if verb == "no":
        return cancel(store, capture.id, via, now=now), [], 0
    outcome, removed = undo(store, capture.id, via, now=now)
    return outcome, [], removed


def worded(outcome: Outcome, capture: Capture, items: list[Item], removed: int,
           undo_hint: str = "") -> str:
    """One sentence for the channel to send back, whatever the outcome."""
    from maestro import commands

    if outcome == Outcome.APPLIED:
        return f"{commands.saved_lines(items)}{undo_hint}"
    if outcome == Outcome.CANCELLED:
        return "Cancelled — nothing was saved. Send it again in other words if you still want it."
    if outcome == Outcome.UNDONE:
        return f"Undone: removed {removed} item{'' if removed == 1 else 's'}."
    if outcome == Outcome.ALREADY:
        return (f"Already saved: {commands.saved_lines(items)}" if items
                else f"Already decided (capture #{capture.id} is {capture.status}).")
    if outcome == Outcome.EXPIRED:
        return f"Too late — capture #{capture.id} expired unanswered. Send it again to save it."
    if outcome == Outcome.TOO_LATE:
        return "The undo window has passed; use done, stop or rm instead."
    return f"No capture #{capture.id} here."


def line(capture: Capture) -> str:
    """One listing line for `maestro list --proposals`."""
    what = "; ".join(proposed_lines(capture))
    when = (f"expires {capture.expires_at:%a %d %b %H:%M}" if capture.status == CaptureStatus.PENDING
            else str(capture.status))
    return f"{capture.id:>4}  {what} — {when}"


def question(capture: Capture) -> str:
    """How a channel asks: what would be saved, and that it expires."""
    lines = "\n".join(proposed_lines(capture))
    return (f"Did you mean:\n{lines}\n"
            f"(capture #{capture.id}, expires {capture.expires_at:%a %d %b %H:%M})")

