import type { ChipKind } from '../api/types.ts'
import styles from './Chip.module.css'

export interface ChipProps {
  kind: ChipKind
  label: string
}

/**
 * A small label under a reminder or in the capture preview. The words carry the meaning; colour
 * only groups the kinds (time and lead blue, urgent and escalation orange, streak green, list teal,
 * the rest grey).
 */
export function Chip({ kind, label }: ChipProps) {
  return (
    <span className={`${styles.chip ?? ''} ${styles[kind] ?? ''}`} data-kind={kind}>
      {label}
    </span>
  )
}
