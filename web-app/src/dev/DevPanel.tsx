import { useId, useState } from 'react'
import type { ApiErrorKind } from '../api/errors.ts'
import { MockClient, failWith } from '../api/mock/MockClient.ts'
import { useClient } from '../app/clientContext.ts'
import { DEV_CLIENT_KEY, type ClientKind } from '../app/createClient.ts'
import styles from './DevPanel.module.css'

// Development builds only (main.tsx leaves it out of production): which client the app runs on and,
// on the mock, reset, latency and injected errors, for trying loading, empty and failure states.

const LATENCIES = [0, 300, 1000, 3000] as const

const FAILURES: [ApiErrorKind | 'none', string][] = [
  ['none', 'Nothing fails'],
  ['offline', 'Server unreachable'],
  ['server', 'Server error (500)'],
  ['unauthorized', 'Signed out (401)'],
  ['rate_limited', 'Too many attempts (429)'],
]

export interface DevPanelProps {
  /** Load the screen again, through its loading state, so delays and failures show. */
  reload: () => void
  /** Switch clients: saves the choice and reloads the page (default). */
  switchTo?: (kind: ClientKind) => void
}

/** Empties the sample data, to see the empty state; Reset brings it back. */
async function removeAll(mock: MockClient): Promise<void> {
  for (const item of await mock.listItems({ include_done: true })) {
    await mock.deleteItem(item.id)
  }
}

function saveAndReload(kind: ClientKind): void {
  try {
    localStorage.setItem(DEV_CLIENT_KEY, kind)
  } catch {
    // storage blocked: the page reloads on the default client
  }
  location.reload()
}

export default function DevPanel({ reload, switchTo = saveAndReload }: DevPanelProps) {
  const { client, kind } = useClient()
  const mock = client instanceof MockClient ? client : null
  const [latency, setLatency] = useState(0)
  const [failure, setFailure] = useState<ApiErrorKind | 'none'>('none')
  const [once, setOnce] = useState(false)
  const latencyId = useId()
  const failureId = useId()

  const applyFailure = (kind: ApiErrorKind | 'none', onlyNext: boolean) => {
    setFailure(kind)
    setOnce(onlyNext)
    mock?.setFailure(
      kind === 'none'
        ? null
        : failWith(kind, { times: onlyNext ? 1 : undefined, retryAfterSeconds: 30 }),
    )
  }

  return (
    <details className={styles.panel}>
      <summary className={styles.summary}>Dev tools</summary>
      <div className={styles.body}>
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Data from</legend>
          {(
            [
              ['mock', 'Sample data (MockClient)'],
              ['http', 'Server (HttpClient; use npm run dev:api)'],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className={styles.option}>
              <input
                type="radio"
                name="dev-client"
                value={value}
                checked={kind === value}
                onChange={() => {
                  switchTo(value)
                }}
              />
              {label}
            </label>
          ))}
        </fieldset>

        <button type="button" className={styles.button} onClick={reload}>
          Reload screen
        </button>

        {mock ? (
          <>
            <div className={styles.fieldset}>
              <button
                type="button"
                className={styles.button}
                onClick={() => {
                  mock.reset()
                  reload()
                }}
              >
                Reset sample data
              </button>
              <button
                type="button"
                className={styles.button}
                onClick={() => {
                  void removeAll(mock).then(reload)
                }}
              >
                Remove all items
              </button>
            </div>

            <label className={styles.field} htmlFor={latencyId}>
              Delay every call
              <select
                id={latencyId}
                className={styles.select}
                value={latency}
                onChange={(e) => {
                  const ms = Number(e.target.value)
                  setLatency(ms)
                  mock.setLatency(ms)
                }}
              >
                {LATENCIES.map((ms) => (
                  <option key={ms} value={ms}>
                    {ms === 0 ? 'No delay' : `${String(ms)} ms`}
                  </option>
                ))}
              </select>
            </label>

            <label className={styles.field} htmlFor={failureId}>
              Make calls fail
              <select
                id={failureId}
                className={styles.select}
                value={failure}
                onChange={(e) => {
                  applyFailure(e.target.value as ApiErrorKind | 'none', once)
                }}
              >
                {FAILURES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>

            <label className={styles.option}>
              <input
                type="checkbox"
                checked={once}
                onChange={(e) => {
                  applyFailure(failure, e.target.checked)
                }}
              />
              Only the next call
            </label>
          </>
        ) : (
          <p className={styles.note}>Reset, delay and failures apply to sample data only.</p>
        )}
      </div>
    </details>
  )
}
