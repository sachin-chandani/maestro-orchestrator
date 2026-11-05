import { useId } from 'react'
import type { Item } from '../api/types.ts'
import { Chip } from './Chip.tsx'
import { formatTime, itemChips } from './format.ts'
import styles from './ReminderCard.module.css'

export interface ReminderCardProps {
  item: Item
  /** The server's timezone; times are shown in it. */
  timeZone: string
  /** What to show in the time column; defaults to the next send time, "17:45". */
  timeLabel?: string
  onDone: (item: Item) => void
  onSnooze: (item: Item) => void
  /** The snooze button's text (default "+1 hour"); the screen decides the duration. */
  snoozeLabel?: string
  /** While the screen waits on done or snooze, the buttons are disabled. */
  busy?: boolean
}

/** A reminder as the design draws it: time, text, what's going on with it, chips, Snooze and Done. */
export function ReminderCard({
  item,
  timeZone,
  timeLabel,
  onDone,
  onSnooze,
  snoozeLabel = '+1 hour',
  busy = false,
}: ReminderCardProps) {
  const textId = useId()
  const done = item.status === 'done'
  const next = item.snoozed_until ?? item.due_at
  const time = timeLabel ?? (next ? formatTime(next, timeZone) : '')
  const state = stateLine(item, timeZone)
  const chips = itemChips(item)
  const classes = [styles.card, item.urgent && !done ? styles.urgent : '', done ? styles.done : '']

  return (
    <article className={classes.join(' ')} aria-labelledby={textId}>
      <div className={styles.body}>
        <span className={styles.time}>{time}</span>
        <div className={styles.main}>
          <p className={styles.text} id={textId}>
            {item.text}
          </p>
          {state && <p className={styles.state}>{state}</p>}
          {chips.length > 0 && (
            <ul className={styles.chips} aria-label="Details">
              {chips.map((c) => (
                <li key={`${c.kind}:${c.label}`}>
                  <Chip kind={c.kind} label={c.label} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {!done && (
        <div className={styles.actions}>
          <button
            type="button"
            className={`${styles.button ?? ''} ${styles.snooze ?? ''}`}
            onClick={() => {
              onSnooze(item)
            }}
            disabled={busy}
            aria-describedby={textId}
          >
            <SnoozeIcon />
            {snoozeLabel}
          </button>
          <button
            type="button"
            className={`${styles.button ?? ''} ${styles.primary ?? ''}`}
            onClick={() => {
              onDone(item)
            }}
            disabled={busy}
            aria-describedby={textId}
          >
            <DoneIcon />
            Done
          </button>
        </div>
      )}
    </article>
  )
}

/** The state that changes what the reminder will do next, in words. */
function stateLine(item: Item, timeZone: string): string | null {
  if (item.status === 'done') return 'Done'
  if (item.awaiting_acknowledgement) return 'Sent · waiting for Done or Snooze'
  if (item.held_until)
    return `Held for quiet hours · goes out at ${formatTime(item.held_until, timeZone)}`
  if (item.snoozed_until) return `Snoozed to ${formatTime(item.snoozed_until, timeZone)}`
  return null
}

function SnoozeIcon() {
  return (
    <svg
      className={styles.icon}
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="13" r="8" />
      <path d="M12 9v4l2 2M5 3 2 6M19 3l3 3" />
    </svg>
  )
}

function DoneIcon() {
  return (
    <svg
      className={styles.icon}
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 12l5 5L19 7" />
    </svg>
  )
}
