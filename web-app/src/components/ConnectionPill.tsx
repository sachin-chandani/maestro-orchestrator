import type { ConnectionState } from '../api/types.ts'
import styles from './ConnectionPill.module.css'
import { connectionLabel } from './format.ts'

export interface ConnectionPillProps {
  state: ConnectionState
  /** What to call the server when connected (the design's "Home server"). */
  serverName?: string
}

/** The header pill saying whether the app can reach the server; announced when it changes. */
export function ConnectionPill({ state, serverName = 'Connected' }: ConnectionPillProps) {
  return (
    <span className={`${styles.pill ?? ''} ${styles[state.status] ?? ''}`} role="status">
      <svg className={styles.dot} width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
        <circle cx="4" cy="4" r="3.25" />
      </svg>
      {connectionLabel(state, serverName)}
    </span>
  )
}
