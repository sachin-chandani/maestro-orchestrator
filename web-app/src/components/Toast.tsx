import { useEffect, useRef } from 'react'
import styles from './Toast.module.css'

export interface ToastProps {
  /** What just happened ("Done. Any pending SMS or call for it is cancelled."); null shows nothing. */
  message: string | null
  /** Called when the toast has been shown for `durationMs`. */
  onDismiss: () => void
  /** How long it stays (default 2.6 s, as in the design); a new message starts the time again. */
  durationMs?: number
}

/**
 * A short confirmation above the capture bar. The status region is always in the page, empty when
 * there's nothing to say, so a new message is announced politely without moving focus.
 */
export function Toast({ message, onDismiss, durationMs = 2600 }: ToastProps) {
  // The latest callback, so a parent re-rendering with a new function doesn't restart the timer.
  const dismiss = useRef(onDismiss)
  useEffect(() => {
    dismiss.current = onDismiss
  })
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => {
      dismiss.current()
    }, durationMs)
    return () => {
      clearTimeout(timer)
    }
  }, [message, durationMs])

  return (
    <div className={styles.region} role="status">
      {message && <div className={styles.toast}>{message}</div>}
    </div>
  )
}
