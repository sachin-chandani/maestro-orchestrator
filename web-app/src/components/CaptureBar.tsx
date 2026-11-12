import { useId, type Ref, type SyntheticEvent } from 'react'
import type { PreviewChip } from '../api/types.ts'
import { Chip } from './Chip.tsx'
import styles from './CaptureBar.module.css'

export interface CaptureBarProps {
  value: string
  onChange: (value: string) => void
  /** Called with the trimmed text when it isn't empty (Enter or the Add button). */
  onSubmit: (text: string) => void
  /** What Maestro understood so far; the screen fetches it (#104). Empty hides the row. */
  preview?: PreviewChip[]
  placeholder?: string
  /** Lets the screen focus the field (the "/" shortcut, #22). */
  inputRef?: Ref<HTMLInputElement>
}

/**
 * The always-on capture box: one field, an Add button and, while typing, the chips Maestro heard.
 * Presentational; keeping the text across tabs, debouncing the preview and queueing offline
 * captures belong to the screen (#104).
 */
export function CaptureBar({
  value,
  onChange,
  onSubmit,
  preview = [],
  placeholder = 'text me tomorrow at 9am to call the bank',
  inputRef,
}: CaptureBarProps) {
  const inputId = useId()
  const submit = (event: SyntheticEvent) => {
    event.preventDefault()
    const text = value.trim()
    if (text) onSubmit(text)
  }

  return (
    <form className={styles.bar} onSubmit={submit} aria-label="Capture">
      {/* Polite, so the preview is read after the person stops typing, not over every key. */}
      <div aria-live="polite">
        {preview.length > 0 && (
          <ul className={styles.preview} aria-label="Maestro hears">
            <li className={styles.hears} aria-hidden="true">
              Maestro hears:
            </li>
            {preview.map((c) => (
              <li key={`${c.kind}:${c.label}`}>
                <Chip kind={c.kind} label={c.label} />
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className={styles.row}>
        <label htmlFor={inputId} className="visually-hidden">
          Capture a reminder or note
        </label>
        <input
          id={inputId}
          ref={inputRef}
          className={styles.input}
          value={value}
          onChange={(e) => {
            onChange(e.target.value)
          }}
          placeholder={placeholder}
          autoComplete="off"
          enterKeyHint="send"
        />
        <button type="submit" className={styles.submit} aria-label="Add">
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
      </div>
    </form>
  )
}
