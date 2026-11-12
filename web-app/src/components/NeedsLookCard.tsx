import { useEffect, useId, useRef, useState, type SyntheticEvent } from 'react'
import type { Item } from '../api/types.ts'
import { GAP_WORDS } from './format.ts'
import styles from './NeedsLookCard.module.css'
import card from './ReminderCard.module.css'

export interface NeedsLookCardProps {
  item: Item
  /**
   * Answer what the item is missing. Rejects with an ApiError when the phrase doesn't answer it;
   * the card shows the message and keeps the phrase. Resolves once the item has moved on.
   */
  onAnswer: (item: Item, phrase: string) => Promise<void>
  onDismiss: (item: Item) => void
  busy?: boolean
  /** Lets the screen reach and focus the row (keyboard j/k, #22). */
  id?: string
}

/**
 * A reading saved with a gap (#126, ADR 0017), at the top of the list: what you said, the gap as a
 * dashed chip, and the two ways to settle it. Answer reveals one field, so the gap is filled in
 * place with one tap and a phrase; #128's suggested times will sit behind the same button.
 */
export function NeedsLookCard({ item, onAnswer, onDismiss, busy = false, id }: NeedsLookCardProps) {
  const textId = useId()
  const fieldId = useId()
  const gap = item.gap ?? 'when'
  const words = GAP_WORDS[gap]
  const [open, setOpen] = useState(false)
  const [phrase, setPhrase] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const field = useRef<HTMLInputElement>(null)
  // The field appears because the person asked for it, so it takes focus (#87).
  useEffect(() => {
    if (open) field.current?.focus()
  }, [open])

  const submit = async (event: SyntheticEvent) => {
    event.preventDefault()
    const answer = phrase.trim()
    if (!answer || saving) return
    setSaving(true)
    setError(null)
    try {
      await onAnswer(item, answer)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Couldn't read that")
    } finally {
      setSaving(false)
    }
  }

  return (
    <article
      className={`${card.card ?? ''} ${styles.card ?? ''}`}
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
          {item.source_text !== item.text && (
            <p className={card.state}>You said “{item.source_text}”</p>
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
      {open ? (
        <form className={styles.answer} onSubmit={(e) => void submit(e)}>
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
          {error && (
            <p className={styles.error} id={`${fieldId}-error`} role="alert">
              {error}
            </p>
          )}
          <div className={card.actions}>
            <button
              type="button"
              className={`${card.button ?? ''} ${card.snooze ?? ''}`}
              onClick={() => {
                setOpen(false)
                setError(null)
              }}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className={`${card.button ?? ''} ${card.primary ?? ''}`}
              disabled={saving || busy || !phrase.trim()}
              aria-describedby={textId}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </form>
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
