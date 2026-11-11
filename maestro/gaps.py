"""Save what is sure, ask about the rest (#126, ADR 0017).

`MAESTRO_CONFIRM=split` — the default — applies the parts of a capture Maestro can act on straight
away and keeps the parts it cannot as **needs a look**: items saved with a `gap` naming what they
are missing (`when`, `what` or `channel`). A gapped item is visible in every list and search,
carries the capture it came from, and is never scheduled, never delivered and never expires. One
message asking for three things therefore saves the two that were clear instead of holding all
three back, which is what `MAESTRO_CONFIRM=unsure` (#117, ADR 0016) does.

Every split capture writes one `captures` row, already applied, whose id is the undo token: for ten
minutes it removes exactly the items that capture added, on any channel. The row is written before
the items, so a crash between the two leaves a capture with nothing attached rather than items
nobody can take back.

A gap is filled by answering. A bare time ("5pm") fills the sender's one recent `when` gap; naming
the item ("#12 5pm") always works, whatever its gap and however old. Anything else is captured as
ever (ADR 0005). `maestro.confidence.gap` decides what counts as a gap, and deliberately never
opens one for a reading that already has a usable time: nothing that used to fire stops firing.
"""

import dataclasses
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from maestro import actions as actions_
from maestro import confidence as confidence_
from maestro.actions import Action, Applied, apply
from maestro.captures import SOURCE, UNDO_WINDOW
from maestro.confidence import Confidence
from maestro.interpret import Context, read_answer, read_time_phrase
from maestro.models import Capture, CaptureStatus, Item
from maestro.notifier import apply_channel_choice
from maestro.observability import METRICS
from maestro.storage import Store

log = logging.getLogger(__name__)

# How long a bare time can answer the last question asked. The *item* never expires — it stays
# listed until it is filled or dismissed — but the shortcut does, because a stray "5pm" the next
# morning is far more likely to be a capture than an answer. Naming the item has no window at all,
# so nothing is ever out of reach.
GAP_REPLY_WINDOW = timedelta(minutes=30)

# "#12 5pm", "fill 12 tomorrow 9am". The "#" or the word "fill" is required: a bare "12 5pm" would
# make "3 eggs and milk" look like an answer to item 3.
_FILL_RE = re.compile(r"^(?:fill\s+#?|#)(?P<id>\d{1,9})\s+(?P<phrase>.+)$", re.I | re.S)
# "dismiss", "dismiss #12": drop a reading nobody wants to complete.
_DISMISS_RE = re.compile(r"^dismiss\s*#?(?P<id>\d{1,9})?$", re.I)

# What each gap is missing, as the question a channel asks.
WANTED = {"when": "a time", "what": "a description", "channel": "a channel"}


# --- splitting a capture ----------------------------------------------------------------------


@dataclass
class Split:
    """What one capture did: everything it saved, and the undo token covering all of it."""

    # Every item saved, in the order the capture asked for them, so a channel reporting "the first
    # one" reports the same one however the parts were judged.
    saved: list[Item] = field(default_factory=list)
    capture: Capture | None = None  # the undo token; None for a direct capture, which has none
    dropped: list[str] = field(default_factory=list)  # channels asked for that aren't configured
    created: bool = True  # False when this provider message had already been captured

    @property
    def added(self) -> list[Item]:
        """What can be acted on now: saved, and scheduled if it has a time."""
        return [item for item in self.saved if item.gap is None]

    @property
    def needs_look(self) -> list[Item]:
        """What is saved but waiting for an answer: visible, never scheduled."""
        return [item for item in self.saved if item.gap is not None]

    @property
    def undo_id(self) -> int | None:
        return self.capture.id if self.capture else None

    @property
    def undo_until(self) -> datetime | None:
        return self.capture.undo_until if self.capture else None


def applied(results: Sequence[Applied]) -> Split:
    """What a direct capture saved, as a Split, so every channel reports one shape whatever
    `MAESTRO_CONFIRM` says. There is no undo token: a direct capture has never had one."""
    return Split(saved=[r.saved for r in results],
                 dropped=sorted({c for r in results for c in r.dropped}),
                 created=all(r.created for r in results))


def _graded(action: Action, requestable: frozenset[str] | None,
            now: datetime) -> tuple[Action, list[str]]:
    """One action with its gap recorded on it, and the channels that had to be dropped.

    The item is built here only to be judged — `apply` builds the one that is actually saved, from
    the same action, so the two agree. Done, snooze and stop carry no gap: they name an item that
    already exists, so there is nothing missing to ask about.
    """
    if not isinstance(action, actions_.CreateReminder | actions_.CreateTask | actions_.CreateNote):
        return action, []
    item = action.to_item(now)
    dropped = apply_channel_choice(item, requestable) if requestable is not None else []
    which = confidence_.gap(item, dropped)
    if which is None:
        return action, dropped
    return dataclasses.replace(action, gap=which), dropped


def split_apply(store: Store, read: list[Action], confidence: Confidence, source: str,
                address: str, text: str, dedupe_key: str | None = None,
                requestable: frozenset[str] | None = None,
                now: datetime | None = None) -> Split:
    """Apply what is sure and save the rest as needing a look, once per provider message.

    The capture row comes first and is already applied, so its undo window covers everything that
    follows. The actions are then applied with a dedupe key derived from the capture id, so a
    webhook retry — or a retry after a crash halfway through — saves nothing twice and finishes
    what the first attempt started.
    """
    now = now or datetime.now(store.tz).replace(microsecond=0)
    graded = [_graded(action, requestable, now) for action in read]
    capture = Capture(
        status=CaptureStatus.APPLIED,
        actions=json.dumps([action.to_json() for action, _ in graded]),
        source=source, address=address, source_text=text,
        interpreter=next((name for a in read if (name := getattr(a, "interpreter", None))), None),
        doubts=",".join(confidence.doubts),
        created_at=now,
        # Nothing here waits to be answered, so there is no expiry to speak of; the column is not
        # nullable, and the undo deadline is the only moment that means anything for this row.
        expires_at=now + UNDO_WINDOW,
        undo_until=now + UNDO_WINDOW, decided_at=now, decided_via=source,
    )
    capture, created = store.add_capture(capture, dedupe_key)
    if not created:
        log.info("%s retried a split capture; finishing the one already recorded (#%s)",
                 source, capture.id)
    results = apply(store, [action for action, _ in graded], source=SOURCE,
                    dedupe_key=f"capture:{capture.id}", requestable=requestable)
    # Only the actions that save something leave an item behind; a done, snooze or stop in the same
    # message is carried out by `apply` and has nothing to list or to take back.
    items = [r.item for r in results if r.item is not None]
    assert capture.id is not None  # add_capture always returns a stored capture
    store.link_capture(capture.id, [item.id for item in items if item.id is not None])
    for item in items:  # so what the caller hands on says which capture can take it back
        item.capture_id = capture.id
    split = Split(
        saved=items, capture=capture, created=created,
        dropped=sorted({channel for _, dropped in graded for channel in dropped}),
    )
    if created:  # a retry has already been counted; counting it again would double every metric
        for _ in split.added:
            METRICS.inc("maestro_capture_split_total", result="saved")
        for _ in split.needs_look:
            METRICS.inc("maestro_capture_split_total", result="needs_look")
        if split.needs_look:
            log.info("Capture %d saved %d item(s) and left %d needing a look (%s)", capture.id,
                     len(split.added), len(split.needs_look),
                     ", ".join(sorted({str(i.gap) for i in split.needs_look})))
    return split


# --- answering a question about a gap -----------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """A reply that answers a question about a gap, once it is matched to what it answers."""

    verb: str  # "fill" or "dismiss"
    item: Item | None = None  # the one it answers
    phrase: str = ""  # what to fill the gap with
    choices: tuple[Item, ...] = ()  # several it could answer: the sender has to name one


def _owned(store: Store, source: str, address: str, item_id: int) -> Item | None:
    """The gapped item with this id, if it belongs to this sender on this channel.

    A guessed number must not reach someone else's capture, and must not say whether it exists:
    both cases come back as None, and the text is then captured like any other (ADR 0005).
    """
    return next((i for i in store.needs_look(source, address) if i.id == item_id), None)


def match_reply(store: Store, source: str, address: str, text: str,
                now: datetime | None = None, window: timedelta | None = None) -> Answer | None:
    """The gap a reply answers, or None — in which case the text is captured as usual.

    Only consulted when the sender actually has a gap the reply could answer, the same rule
    confirmations follow (ADR 0016). Three forms, in order: naming an item, dismissing one, and a
    message that is nothing but a time.
    """
    now = now or datetime.now(store.tz).replace(microsecond=0)
    window = window if window is not None else GAP_REPLY_WINDOW
    text = text.strip()

    if m := _FILL_RE.match(text):
        # Naming an item always works: an explicit id is unambiguous, so no window applies.
        if (item := _owned(store, source, address, int(m["id"]))) is not None:
            return Answer("fill", item, phrase=m["phrase"].strip())
        return None

    if m := _DISMISS_RE.match(text):
        if m["id"] is not None:
            item = _owned(store, source, address, int(m["id"]))
            return Answer("dismiss", item) if item else None
        pending = store.needs_look(source, address)
        if not pending:
            return None
        return Answer("dismiss", pending[0]) if len(pending) == 1 else Answer("dismiss",
                                                                             choices=tuple(pending))

    # A bare time, and only a bare time: "call dad at 5pm" says what to do, so it is a capture.
    if read_time_phrase(text, Context(tz=store.tz, now=now)) is None:
        return None
    recent = [i for i in store.needs_look(source, address, since=now - window)
              if i.gap == "when"]
    if not recent:
        return None  # nothing it could answer: captured as usual, and the gap is left untouched
    if len(recent) > 1:
        # Two things could be meant, so nothing is filled and nothing is saved: the sender picks.
        return Answer("fill", phrase=text, choices=tuple(recent))
    return Answer("fill", recent[0], phrase=text)


def fill(store: Store, item: Item, phrase: str, requestable: frozenset[str] | None = None,
         now: datetime | None = None) -> Item | None:
    """Fill one item's gap from `phrase`, returning it as it now stands, or None if the phrase
    doesn't answer the question (an unreadable time, a channel this deployment hasn't got).

    What the phrase has to be depends on the gap: a time for `when`, channel names for `channel`,
    and for `what` the words the capture never had.
    """
    assert item.id is not None
    if item.gap == "when":
        read = read_time_phrase(phrase, Context(tz=store.tz, now=now))
        if read is None or read.due_at is None:
            return None
        filled = store.fill_gap(item.id, due_at=read.due_at, recurrence=read.recurrence)
    elif item.gap == "channel":
        wanted = [c for c in re.split(r"[,\s]+", phrase.strip().lower()) if c]
        if not wanted or (requestable is not None and any(c not in requestable for c in wanted)):
            return None
        filled = store.fill_gap(item.id, channels=wanted)
    else:  # "what": the words the capture never had, read as a capture in their own right
        # Read whole, not just for a time: someone answering "what did you mean?" with "call the
        # dentist at 5pm" means a reminder at 5pm, so the time has to come out of the text and
        # into the item, exactly as it would have on the way in. Reading only the time would leave
        # a note that says "at 5pm" and never fires, which is the one outcome ADR 0017 exists to
        # prevent.
        read = read_answer(phrase, Context(tz=store.tz, now=now))
        if read is None:
            return None
        filled = store.fill_gap(item.id, due_at=read.due_at, recurrence=read.recurrence,
                                text=read.text, kind=read.kind)
    if not filled:
        return None  # someone else filled or dismissed it first; theirs stands
    METRICS.inc("maestro_capture_split_total", result="filled")
    log.info("Item %d's %s gap filled", item.id, item.gap)
    return store.get(item.id)


def dismiss(store: Store, item: Item) -> bool:
    """Drop a reading nobody wants to complete. False when it is already gone."""
    assert item.id is not None
    if not store.delete(item.id):
        return False
    METRICS.inc("maestro_capture_split_total", result="dismissed")
    log.info("Item %d dismissed unanswered", item.id)
    return True


def answer(store: Store, reply: Answer, requestable: frozenset[str] | None = None,
           now: datetime | None = None) -> tuple[str, Item | None]:
    """Carry out a matched reply and say what happened, for the channel to send back."""
    from maestro import commands

    if reply.choices:
        METRICS.inc("maestro_capture_split_total", result="ambiguous")
        which = ", ".join(f"#{i.id} {i.text}" for i in reply.choices)
        return (f"Which one? {which}. "
                f"Reply '#{reply.choices[0].id} {reply.phrase or 'dismiss'}'.", None)
    if reply.item is None:  # pragma: no cover - match_reply only returns an item or choices
        return "There is nothing waiting for an answer.", None
    if reply.verb == "dismiss":
        if not dismiss(store, reply.item):
            return f"No item #{reply.item.id} needing a look.", None
        return f"Dismissed: {reply.item.text}.", None
    filled = fill(store, reply.item, reply.phrase, requestable, now)
    if filled is None:
        wanted = WANTED.get(str(reply.item.gap), "an answer")
        return (f"#{reply.item.id} {reply.item.text} still needs {wanted}; "
                f"I couldn't read {wanted} in that.", None)
    return f"Scheduled {commands.line(filled)}.", filled


# --- what a channel says -------------------------------------------------------------------------


def asked(needs_look: Sequence[Item]) -> str:
    """The question a channel asks about what needs a look, or "" when nothing does."""
    if not needs_look:
        return ""
    if len(needs_look) == 1:
        item = needs_look[0]
        wanted = WANTED.get(str(item.gap), "an answer")
        reply = "with one" if item.gap == "when" else f"with {wanted}"
        return f"'{item.text}' needs {wanted}: reply {reply} (#{item.id})."
    lines = "; ".join(f"#{i.id} {i.text} needs {WANTED.get(str(i.gap), 'an answer')}"
                      for i in needs_look)
    return f"{len(needs_look)} need a look: {lines}."


def worded(split: Split, undo_hint: str = "") -> str:
    """What one split capture did, in one message: what was added, then what still needs a look."""
    from maestro import commands

    parts = []
    if split.added:
        parts.append(f"Added {len(split.added)}." if split.needs_look
                     else commands.saved_lines(split.added) + ".")
    if question := asked(split.needs_look):
        parts.append(question)
    if not parts:  # pragma: no cover - a capture always reads as at least one action
        parts.append("Nothing was saved.")
    if split.dropped:
        kept = {c for i in split.saved for c in (i.channels or [])}
        parts.append(f"({', '.join(split.dropped)} isn't configured; "
                     f"using {', '.join(sorted(kept)) if kept else 'the default channels'}.)")
    return " ".join(parts) + undo_hint


def line(item: Item) -> str:
    """One listing line for an item that needs a look."""
    from maestro import commands

    icon = commands.KIND_ICONS.get(item.kind, "📝")
    return f"{icon} #{item.id} {item.text} — needs {WANTED.get(str(item.gap), 'an answer')}"
