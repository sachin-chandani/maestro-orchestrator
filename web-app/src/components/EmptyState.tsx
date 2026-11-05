import type { ReactNode } from 'react'
import styles from './EmptyState.module.css'

export interface EmptyStateProps {
  /** "Nothing left today" */
  title: string
  /** Teaches the capture phrase: "Try “add oat milk to the shopping list” below." */
  hint?: ReactNode
}

/** What a screen shows when it has nothing to list, and how to fill it. */
export function EmptyState({ title, hint }: EmptyStateProps) {
  return (
    <div className={styles.empty}>
      <p className={styles.title}>{title}</p>
      {hint && <p className={styles.hint}>{hint}</p>}
    </div>
  )
}
