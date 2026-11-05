import { useCallback, useEffect, useState, type ReactNode } from 'react'
import type { MaestroClient } from './api/client.ts'
import { isApiError } from './api/errors.ts'
import type { Item } from './api/types.ts'
import styles from './App.module.css'
import { useClient, useConnection } from './app/clientContext.ts'
import { groupReminders, headerDate, laterLabel, nextTime } from './app/groups.ts'
import { CaptureBar } from './components/CaptureBar.tsx'
import { ConnectionPill } from './components/ConnectionPill.tsx'
import { EmptyState } from './components/EmptyState.tsx'
import { formatTime } from './components/format.ts'
import { ReminderCard } from './components/ReminderCard.tsx'
import { Toast } from './components/Toast.tsx'

type Load =
  | { status: 'loading' }
  | { status: 'ready'; items: Item[]; timeZone: string }
  | { status: 'error'; message: string }

export interface AppProps {
  /** Dev tools under the list, given a reload that shows loading first (development builds only). */
  devTools?: (reload: () => void) => ReactNode
}

function messageOf(error: unknown): string {
  return isApiError(error) ? error.message : 'Something went wrong'
}

async function fetchToday(client: MaestroClient): Promise<Load> {
  try {
    const [health, items] = await Promise.all([client.health(), client.listItems()])
    return { status: 'ready', items, timeZone: health.timezone }
  } catch (error) {
    return { status: 'error', message: messageOf(error) }
  }
}

/**
 * The app's one screen until #103–#107: reminders for today, tomorrow and later, with Done and
 * +1 hour, and the capture bar. Everything goes through the client from ClientProvider.
 */
export default function App({ devTools }: AppProps) {
  const { client, kind } = useClient()
  const connection = useConnection()
  const [load, setLoad] = useState<Load>({ status: 'loading' })
  const [busy, setBusy] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    void fetchToday(client).then((next) => {
      if (current) setLoad(next)
    })
    return () => {
      current = false
    }
  }, [client])

  /** Fetch again after a change; a failure keeps what's on screen rather than blanking it. */
  const refresh = useCallback(async () => {
    const next = await fetchToday(client)
    if (next.status === 'ready') setLoad(next)
  }, [client])

  const retry = () => {
    setLoad({ status: 'loading' })
    void fetchToday(client).then(setLoad)
  }

  const dismissToast = useCallback(() => {
    setToast(null)
  }, [])

  const act = async (item: Item, action: 'done' | 'snooze', timeZone: string) => {
    setBusy(item.id)
    try {
      if (action === 'done') {
        const after = await client.done(item.id)
        // A recurring reminder carries on: say when it's next (ADR 0009).
        setToast(
          after.status === 'done'
            ? 'Done. Any pending SMS or call for it is cancelled.'
            : `Done for this time. Next: ${after.when}.`,
        )
      } else {
        const after = await client.snooze(item.id, '1h')
        const back = nextTime(after)
        setToast(
          `Snoozed to ${back ? formatTime(back, timeZone) : 'later'}. Escalation restarts from then.`,
        )
      }
      await refresh()
    } catch (error) {
      setToast(`Couldn't ${action === 'done' ? 'mark it done' : 'snooze it'}: ${messageOf(error)}`)
    } finally {
      setBusy(null)
    }
  }

  const capture = async (text: string) => {
    setSaving(true)
    try {
      const item = await client.capture(text)
      setDraft('')
      setToast(item.kind === 'note' ? 'Saved as a note.' : `Reminder set for ${item.when}.`)
      await refresh()
    } catch (error) {
      setToast(`Couldn't save it: ${messageOf(error)}`) // the text stays in the box to try again
    } finally {
      setSaving(false)
    }
  }

  const now = new Date()
  const groups = load.status === 'ready' ? groupReminders(load.items, now, load.timeZone) : []
  const left = groups.find((g) => g.name === 'Today')?.items.length ?? 0

  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Today</h1>
          <ConnectionPill
            state={connection}
            serverName={kind === 'mock' ? 'Sample data' : 'Connected'}
          />
        </div>
        {load.status === 'ready' && (
          <p className={styles.summary}>
            {headerDate(now, load.timeZone)} · {left} left today
          </p>
        )}
      </header>

      <main className={styles.main}>
        {load.status === 'loading' && (
          <p className={styles.message} role="status">
            Loading reminders…
          </p>
        )}

        {load.status === 'error' && (
          <div className={styles.error} role="alert">
            <p className={styles.message}>Couldn't load your reminders: {load.message}</p>
            <button type="button" className={styles.retry} onClick={retry}>
              Try again
            </button>
          </div>
        )}

        {load.status === 'ready' && groups.length === 0 && (
          <EmptyState
            title="Nothing coming up"
            hint="Try “text me tomorrow at 9am to call the bank” below."
          />
        )}

        {load.status === 'ready' &&
          groups.map((group) => (
            <section
              key={group.name}
              className={styles.group}
              aria-labelledby={`group-${group.name}`}
            >
              <h2 className={styles.groupName} id={`group-${group.name}`}>
                {group.name}
              </h2>
              {group.items.map((item) => {
                const next = nextTime(item)
                return (
                  <ReminderCard
                    key={item.id}
                    item={item}
                    timeZone={load.timeZone}
                    timeLabel={
                      group.name === 'Later' && next ? laterLabel(next, load.timeZone) : undefined
                    }
                    busy={busy === item.id}
                    onDone={(i) => void act(i, 'done', load.timeZone)}
                    onSnooze={(i) => void act(i, 'snooze', load.timeZone)}
                  />
                )
              })}
            </section>
          ))}

        {devTools?.(retry)}
      </main>

      <footer className={styles.footer}>
        <Toast message={toast} onDismiss={dismissToast} />
        <CaptureBar
          value={draft}
          onChange={setDraft}
          onSubmit={(text) => {
            if (!saving) void capture(text)
          }}
        />
      </footer>
    </div>
  )
}
