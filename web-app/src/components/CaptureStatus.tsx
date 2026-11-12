import { useEffect, useRef, useState } from 'react'
import styles from './CaptureStatus.module.css'
import toast from './Toast.module.css'

/** How long the line stays up. The capture is saved either way; this is just the window on screen. */
export const SHOW_MS = 10_000

export interface CaptureStatusProps {
  /** What the capture did: "Added 2. 'Call the plumber' needs a time." */
  message: string
  /** Undo is offered while this is set; null when the capture saved nothing to take back. */
  undoId: number | null
  /** Called on Undo. The parent clears the line (or explains) when it settles. */
  onUndo: (undoId: number) => void
  /** Called when the 10 seconds are up and the line should go. */
  onDismiss: () => void
  /** While the undo request is in flight. */
  busy?: boolean
}

/**
 * The capture status line (#104, ADR 0017): one sentence saying what was saved, with Undo and a
 * countdown, in the toast's place above the capture bar. The reminders are already saved and in
 * the list — the server never waits for these 10 seconds — so letting the line lapse costs
 * nothing, and Undo removes exactly what this capture added.
 */
export function CaptureStatus({
  message,
  undoId,
  onUndo,
  onDismiss,
  busy = false,
}: CaptureStatusProps) {
  const [secondsLeft, setSecondsLeft] = useState(SHOW_MS / 1000)
  // The latest callback, so a parent re-render doesn't restart the countdown (as Toast does).
  const dismiss = useRef(onDismiss)
  useEffect(() => {
    dismiss.current = onDismiss
  })
  // Mounted per capture (the parent keys this component by capture), so the countdown starts
  // fresh with the component. One tick per second, counted rather than clocked: a second's drift
  // doesn't matter — the server owns the real 10-minute window — and ticks are deterministic.
  useEffect(() => {
    let left = SHOW_MS / 1000
    const timer = setInterval(() => {
      left -= 1
      if (left <= 0) {
        clearInterval(timer)
        dismiss.current()
      } else {
        setSecondsLeft(left)
      }
    }, 1000)
    return () => {
      clearInterval(timer)
    }
  }, [])

  return (
    <div className={toast.region} role="status">
      <div className={`${toast.toast ?? ''} ${styles.line ?? ''}`}>
        <span className={styles.message}>{message}</span>
        {undoId !== null && (
          <button
            type="button"
            className={styles.undo}
            onClick={() => {
              onUndo(undoId)
            }}
            disabled={busy}
          >
            Undo · {secondsLeft}s
          </button>
        )}
      </div>
    </div>
  )
}
