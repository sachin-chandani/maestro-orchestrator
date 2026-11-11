# 0017: A capture splits per reading — sure parts save at once, unsure ones wait with a named gap

**Status:** Accepted (#126)

## Context
ADR 0016 holds a whole message back when the rules doubt any part of it. One sentence can hold
several things, and Maestro is sure about some and not others: "book the flights and remind me to
pack before" should save the booking, not wait on the half it cannot read. Holding everything is
also the wrong default for the phone app (#22, #104), which needs a screen of specific questions,
not a queue of yes/no prompts.

The sharper problem is the one the old behaviour hides. A reminder that silently does not fire is
the worst thing this system can do — worse than the wrong-time guess it would replace. Any change
to how unsure captures are handled is measured against that.

## Decision

- **Each reading is judged on its own.** `maestro.confidence.gap` grades one proposed action and
  names what it is missing: `when`, `what` or `channel` (`GAPS`). `assess` still grades the whole
  message, because that is what `MAESTRO_INTERPRETER=auto` and `MAESTRO_CONFIRM=unsure` ask. A
  doubt is a reason to ask a model; a gap is a reason to ask the person.

- **A gap never takes a time away.** A reading that already has a usable time has no `when` gap,
  whatever the message was doubted for. "Work from 9am to 5pm" is doubted — the rules read 9am and
  left 5pm — and still saves and fires at 9am, exactly as before. Only something to do, with no
  time at all, in a message that named one the rules cannot read ("pick up the kids before") is
  held back. Notes are never held back: a note is a fact to keep and never wanted a time, so "the
  best time to call dad is after dinner" is read right and left alone. Two tests hold this: no
  corpus phrase the rules read correctly is ever held back, and no phrase that produced a time is
  held back even among the ones they read wrong.

- **A needs-a-look entry is an item, not a captures row.** `items.gap` names what it lacks; NULL
  means nothing does. A gapped item is saved, searchable, listed and linked to its capture, and is
  never scheduled — `Store.due` filters `gap IS NULL` — and never expires. A `captures` row was
  rejected because expiry is load-bearing there (ADR 0016) and a question about a real thing
  someone said should not quietly time out; an item also gets listing, search and undo for free.

- **An unconfigured channel is not a gap.** ADR 0011 already saves the item, delivers it on the
  default channels and tells the person. Turning that into a question would stop a reminder with a
  perfectly good time from going out, which is the one thing this ADR will not do. `channel` stays
  in `GAPS`, and fill, dismiss, the API and the wording all handle it, for an interpreter that can
  tell a required channel from a preferred one; the rules cannot, so they never open one.

- **One undo token per capture.** Every split capture writes one `captures` row, created already
  `applied`, with `undo_until` ten minutes out. Its id is the token, on every channel and in the
  API. `captures.undo` then works unchanged: it deletes exactly the items that capture added, an
  answer given since included — it is the same item. The row is written before the items and the
  actions are applied under a dedupe key derived from its id, so a retry (a webhook, or a crash
  halfway through) finishes what the first attempt started and saves nothing twice.

- **Fills are matched conservatively.** The invariant first: **a reply can only ever complete a
  gapped item, never touch a scheduled reminder.** There is no code path from a reply to a complete
  item. On top of that:
  - **Shape.** The whole message must be nothing but a time (`parser.time_phrase_only`): "5pm",
    "tomorrow 9am", "every monday". "Call dad at 5pm" says what to do, so it is a capture.
  - **Window.** A bare time answers only a question from the last 30 minutes
    (`MAESTRO_GAP_REPLY_MINUTES`). The *item* never expires; only the shortcut does, because a
    stray "5pm" the next morning is far likelier to be a capture than an answer.
  - **Exactly one.** With one recent question, it is filled. With two or more, nothing is filled
    and nothing is saved: Maestro quotes both ids and asks which. Declining to save loses little —
    a bare time is a degenerate capture with no "what" of its own.
  - **Naming always works.** `#12 5pm` has no window and reaches any gap, with the ownership check
    `captures.target` uses: same channel, same address. A number that is not the sender's own gap
    reaches nothing and says nothing about whether it exists; the text is captured as ever
    (ADR 0005).
  - Filling is a check-and-set on `gap IS NOT NULL`, so two answers racing leave one winner.

- **`MAESTRO_CONFIRM=split` is the new default.** `unsure` keeps ADR 0016's hold-the-whole-message
  behaviour; `never` applies everything at once, as Maestro did before either existed. The default
  changed because, with the gap rule above, the upgrade is a strict improvement: everything that
  fired before still fires at the same time, and things that never fired are now visible and
  answerable instead of silently mis-saved.

## Consequences
- SQLite schema 13 and PostgreSQL schema 6: one nullable `items.gap` column, no index — `due` is
  already served by `idx_items_due`, where `gap IS NULL` matches almost every row, and the list of
  things awaiting an answer is only ever as long as a person's patience. Both are migration-tested
  from the previous schema, and both tests assert that an upgrade invents no question and silences
  no reminder.
- `POST /items` keeps its shape for existing clients and answers with the first item, which may be
  one with a `gap`. `POST /capture` is the new call and reports `{added, needs_look, undo}`.
  `GET /needs-look`, `POST /needs-look/{id}` and `DELETE /needs-look/{id}` answer the questions.
- `maestro_capture_split_total{result}` counts `saved`, `needs_look`, `filled`, `dismissed` and
  `ambiguous`.
- `maestro add` prints the undo line only when a capture did something unexpected — saved several
  things, or left a question. One plain reminder prints what it always did, and its number, which
  `maestro rm` takes.
- Email-in and voice still apply directly, as under ADR 0016: neither has a reply loop a question
  could be answered in, and "reply with a time" is nonsense read out during a phone call. Their
  captures are therefore never held back — a voice capture the rules misread is saved untimed, as
  it was before. `TwilioHandler.read_and_save` splits whenever it is given confirmation settings,
  so wiring voice in is one argument at the call site if that turns out to be wanted; it is left
  out deliberately rather than by omission.
- ADR 0016's default is superseded; a note there says so.
