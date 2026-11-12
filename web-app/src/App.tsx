import { useCallback, useEffect, useState, type ReactNode } from 'react'
import type { MaestroClient } from './api/client.ts'
import { isApiError } from './api/errors.ts'
import type { Item } from './api/types.ts'
import styles from './App.module.css'
import { useClient, useConnection } from './app/clientContext.ts'
import { groupReminders, headerDate, laterLabel, nextTime } from './app/groups.ts'
import { captureMessage } from './app/captureMessage.ts'
import { useSequence } from './app/sequence.ts'
import { clearToken, loadToken, saveToken } from './app/session.ts'
import { CaptureBar } from './components/CaptureBar.tsx'
import { ConnectionPill } from './components/ConnectionPill.tsx'
import { EmptyState } from './components/EmptyState.tsx'
import { formatTime } from './components/format.ts'
import { ReminderCard } from './components/ReminderCard.tsx'
import { SignIn } from './components/SignIn.tsx'
import { Toast } from './components/Toast.tsx'

type Load =
  | { status: 'loading' }
  | { status: 'ready'; items: Item[]; timeZone: string }
  | { status: 'error'; message: string }

/** Something said above the capture bar; `id` tells apart two with the same words (#112). */
interface Notice {
  id: number
  text: string
}

/**
 * How often the list is fetched again while the page is visible, so the day's groups, the date and
 * "N left today" roll over at midnight and a reminder that went out shows as awaiting (#112, #108).
 * Every return to the tab fetches at once. Polling rather than server-sent events: the backend has
 * no streaming endpoint, and a reminder list changes on the scale of minutes.
 */
export const REFRESH_MS = 60_000

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
  const [busy, setBusy] = useState<ReadonlySet<number>>(new Set())
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState<Notice | null>(null)
  // Each fetch takes a number; a result that isn't from the latest fetch is dropped, so a slow
  // older response can't overwrite a newer one (#112).
  const fetches = useSequence()
  const notices = useSequence()

  const say = useCallback(
    (text: string) => {
      setToast({ id: notices.next(), text })
    },
    [notices],
  )

  useEffect(() => {
    const mine = fetches.next()
    void fetchToday(client).then((next) => {
      if (fetches.isLatest(mine)) setLoad(next)
    })
    return () => {
      fetches.next() // whatever is in flight is for a client that's gone
    }
  }, [client, fetches])

  /** Fetch again after a change or on a timer; a failure keeps what's on screen. */
  const refresh = useCallback(async () => {
    const mine = fetches.next()
    const next = await fetchToday(client)
    if (fetches.isLatest(mine) && next.status === 'ready') setLoad(next)
  }, [client, fetches])

  /** Load again through the loading state, so a failure shows in place of the list. */
  const retry = () => {
    const mine = fetches.next()
    setLoad({ status: 'loading' })
    void fetchToday(client).then((next) => {
      if (fetches.isLatest(mine)) setLoad(next)
    })
  }

  // Keep the list current over time: every REFRESH_MS while visible, and on every return to the tab.
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, REFRESH_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [refresh])

  const dismissToast = useCallback(() => {
    setToast(null)
  }, [])

  const setCardBusy = (id: number, isBusy: boolean) => {
    setBusy((current) => {
      const next = new Set(current)
      if (isBusy) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const act = async (item: Item, action: 'done' | 'snooze', timeZone: string) => {
    if (busy.has(item.id)) return // a second tap while the first is in flight does nothing
    setCardBusy(item.id, true)
    try {
      if (action === 'done') {
        const after = await client.done(item.id)
        // A recurring reminder carries on: say when it's next (ADR 0009).
        say(
          after.status === 'done'
            ? 'Done. Any pending SMS or call for it is cancelled.'
            : `Done for this time. Next: ${after.when}.`,
        )
      } else {
        const after = await client.snooze(item.id, '1h')
        const back = nextTime(after)
        say(
          `Snoozed to ${back ? formatTime(back, timeZone) : 'later'}. Escalation restarts from then.`,
        )
      }
      await refresh()
    } catch (error) {
      say(`Couldn't ${action === 'done' ? 'mark it done' : 'snooze it'}: ${messageOf(error)}`)
    } finally {
      setCardBusy(item.id, false)
    }
  }

  const capture = async (text: string) => {
    setSaving(true)
    // The box is cleared now, not after the save, so words typed during a slow save aren't lost
    // with it (#112). If the save fails and nothing new has been typed, the text comes back.
    setDraft('')
    try {
      say(captureMessage(await client.captureSplit(text)))
      await refresh()
    } catch (error) {
      setDraft((current) => (current.trim() ? current : text))
      say(`Couldn't save "${text}": ${messageOf(error)}`)
    } finally {
      setSaving(false)
    }
  }

  // Signed out (a 401: no token, a wrong one, or this device revoked): the stored token is no
  // good, so forget it and ask to sign in. Never on a network error, which keeps the token.
  const signedOut = connection.status === 'signed_out'
  // Whether this device had signed in before: a stored token at start, or a list on screen.
  const [hadToken] = useState(() => loadToken() !== null)
  const wasSignedIn = hadToken || load.status === 'ready'
  useEffect(() => {
    if (signedOut) clearToken()
  }, [signedOut])

  const signIn = async (masterToken: string, deviceName: string) => {
    const result = await client.registerDevice(masterToken, deviceName)
    saveToken(result.token)
    retry()
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
        {signedOut && (
          <SignIn
            onSignIn={signIn}
            reason={wasSignedIn ? 'This device was signed out.' : undefined}
          />
        )}

        {!signedOut && load.status === 'loading' && (
          <p className={styles.message} role="status">
            Loading reminders…
          </p>
        )}

        {!signedOut && load.status === 'error' && (
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
                    busy={busy.has(item.id)}
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
        <Toast message={toast?.text ?? null} messageKey={toast?.id} onDismiss={dismissToast} />
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
