import { useEffect, useId, useRef, useState, type SyntheticEvent } from 'react'
import type { GapSuggestions, Item } from '../api/types.ts'
import { dayLabel } from '../app/feedGroups.ts'
import { formatTime, GAP_WORDS } from './format.ts'
import styles from './NeedsLookCard.module.css'
import card from './ReminderCard.module.css'

export interface NeedsLookCardProps {
  item: Item
  /** The server's timezone, for the suggested times. */
  timeZone: string
  /**
   * Answer what the item is missing: a phrase, or a suggestion's exact ISO time. Rejects with an
   * ApiError when it doesn't answer; the card shows the message and keeps the phrase. Resolves
   * once the item has moved on.
   */
  onAnswer: (item: Item, phrase: string) => Promise<void>
  /** Three suggested times for a `when` gap (#128); the card asks when the sheet opens. */
  loadSuggestions: (item: Item) => Promise<GapSuggestions>
  /** No time: keep it as it is, under Anytime (#128). */
  onKeep: (item: Item) => void
  onDismiss: (item: Item) => void
  busy?: boolean
  /** Lets the screen reach and focus the row (keyboard j/k, #22). */
  id?: string
  /** Just added by a capture (#104). */
  highlighted?: boolean
}

/**
 * A reading saved with a gap (#126, ADR 0017), at the top of the list: what you said, the gap as
 * a dashed chip, and the ways to settle it. Answer opens a sheet in the card (#128): what you
 * said, three suggested times with a reason each, Pick a time (a phrase), No time (keep it under
 * Anytime), and Dismiss. One tap saves it and it moves into the list at its time.
 */
export function NeedsLookCard({
  item,
  timeZone,
  onAnswer,
  loadSuggestions,
  onKeep,
  onDismiss,
  busy = false,
  id,
  highlighted = false,
}: NeedsLookCardProps) {
  const textId = useId()
  const fieldId = useId()
  const gap = item.gap ?? 'when'
  const words = GAP_WORDS[gap]
  const [open, setOpen] = useState(false)
  // null while loading; a when-gap gets times, any other gap an empty list and just the field.
  const [suggested, setSuggested] = useState<GapSuggestions | null>(null)
  const [typing, setTyping] = useState(false)
  const [phrase, setPhrase] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const field = useRef<HTMLInputElement>(null)
  // The field appears because the person asked for it, so it takes focus (#87).
  useEffect(() => {
    if (open && typing) field.current?.focus()
  }, [open, typing])
  useEffect(() => {
    if (!open || suggested !== null) return
    let current = true
    loadSuggestions(item)
      .then((got) => {
        if (!current) return
        setSuggested(got)
        if (got.suggestions.length === 0) setTyping(true) // nothing to offer: straight to the field
      })
      .catch(() => {
        if (!current) return
        setSuggested({ said: item.text, suggestions: [] }) // suggestions are a nicety, never a wall
        setTyping(true)
      })
    return () => {
      current = false
    }
  }, [open, suggested, loadSuggestions, item])

  const answer = async (value: string) => {
    if (saving) return
    setSaving(true)
    setError(null)
    try {
      await onAnswer(item, value)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Couldn't read that")
    } finally {
      setSaving(false)
    }
  }

  const submit = (event: SyntheticEvent) => {
    event.preventDefault()
    const typed = phrase.trim()
    if (typed) void answer(typed)
  }

  const close = () => {
    setOpen(false)
    setTyping(false)
    setError(null)
  }

  return (
    <article
      className={`${card.card ?? ''} ${styles.card ?? ''} ${highlighted ? (card.justAdded ?? '') : ''}`}
      aria-labelledby={textId}
      id={id}
      tabIndex={-1}
    >
      <div className={card.body}>
        <span className={`${card.time ?? ''} ${styles.ask ?? ''}`}>{words.ask}</span>
        <div className={card.main}>
          <p className={card.text} id={textId}>
            {item.text}
          </p>
          {open && suggested ? (
            <p className={card.state}>You said “{suggested.said}”</p>
          ) : (
            item.source_text !== item.text && (
              <p className={card.state}>You said “{item.source_text}”</p>
            )
          )}
          <ul className={card.chips} aria-label="Details">
            <li>
              <span className={styles.gap} data-gap={gap}>
                {words.chip}
              </span>
            </li>
          </ul>
        </div>
      </div>

      {open && suggested === null && (
        <p className={card.state} role="status">
          Working out some times…
        </p>
      )}

      {open && suggested && suggested.suggestions.length > 0 && !typing && (
        <ul className={styles.suggestions} aria-label="Suggested times">
          {suggested.suggestions.map((s) => (
            <li key={s.at}>
              <button
                type="button"
                className={styles.suggestion}
                onClick={() => void answer(s.at)}
                disabled={saving || busy}
                aria-describedby={textId}
              >
                <span className={styles.when}>
                  {dayLabel(s.at, timeZone)} {formatTime(s.at, timeZone)}
                </span>
                <span className={styles.reason}>{s.reason}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {open && typing && (
        <form className={styles.answer} onSubmit={submit}>
          <label htmlFor={fieldId} className="visually-hidden">
            {words.ask}
          </label>
          <input
            id={fieldId}
            className={styles.input}
            value={phrase}
            onChange={(e) => {
              setPhrase(e.target.value)
            }}
            placeholder={words.example}
            autoComplete="off"
            enterKeyHint="done"
            aria-describedby={error ? `${fieldId}-error` : undefined}
            aria-invalid={error ? true : undefined}
            ref={field}
          />
        </form>
      )}

      {error && (
        <p className={styles.error} id={`${fieldId}-error`} role="alert">
          {error}
        </p>
      )}

      {open ? (
        <div className={card.actions}>
          <button
            type="button"
            className={`${card.button ?? ''} ${card.snooze ?? ''}`}
            onClick={close}
            disabled={saving}
          >
            Cancel
          </button>
          {!typing && suggested && (
            <button
              type="button"
              className={`${card.button ?? ''} ${card.snooze ?? ''}`}
              onClick={() => {
                setTyping(true)
              }}
              disabled={saving || busy}
            >
              Pick a time…
            </button>
          )}
          {gap === 'when' && (
            <button
              type="button"
              className={`${card.button ?? ''} ${card.snooze ?? ''}`}
              onClick={() => {
                onKeep(item)
              }}
              disabled={saving || busy}
              aria-describedby={textId}
            >
              No time · keep it
            </button>
          )}
          {typing && (
            <button
              type="button"
              className={`${card.button ?? ''} ${card.primary ?? ''}`}
              onClick={submit}
              disabled={saving || busy || !phrase.trim()}
              aria-describedby={textId}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          )}
        </div>
      ) : (
        <div className={card.actions}>
          <button
            type="button"
            className={`${card.button ?? ''} ${card.snooze ?? ''}`}
            onClick={() => {
              onDismiss(item)
            }}
            disabled={busy}
            aria-describedby={textId}
          >
            Dismiss
          </button>
          <button
            type="button"
            className={`${card.button ?? ''} ${card.primary ?? ''}`}
            onClick={() => {
              setOpen(true)
            }}
            disabled={busy}
            aria-describedby={textId}
            data-shortcut="answer"
          >
            Answer…
          </button>
        </div>
      )}
    </article>
  )
}
