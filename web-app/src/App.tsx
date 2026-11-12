import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { MaestroClient } from './api/client.ts'
import { isApiError } from './api/errors.ts'
import type { Feed, Item } from './api/types.ts'
import styles from './App.module.css'
import { captureMessage } from './app/captureMessage.ts'
import { useClient, useConnection } from './app/clientContext.ts'
import { groupFeed } from './app/feedGroups.ts'
import { headerDate, nextTime } from './app/groups.ts'
import { useSequence } from './app/sequence.ts'
import { clearToken, loadToken, saveToken } from './app/session.ts'
import { SHORTCUT_LIST, useShortcuts } from './app/useShortcuts.ts'
import { CaptureBar } from './components/CaptureBar.tsx'
import { ConnectionPill } from './components/ConnectionPill.tsx'
import { rowId, rowsOf } from './app/rows.ts'
import { FeedList } from './components/FeedList.tsx'
import { formatTime } from './components/format.ts'
import { SignIn } from './components/SignIn.tsx'
import { Toast } from './components/Toast.tsx'

type Load =
  | { status: 'loading' }
  | { status: 'ready'; feed: Feed; timeZone: string }
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

async function fetchFeed(client: MaestroClient): Promise<Load> {
  try {
    const feed = await client.feed()
    return { status: 'ready', feed, timeZone: feed.timezone }
  } catch (error) {
    return { status: 'error', message: messageOf(error) }
  }
}

/**
 * The app's one screen (#22, ADR 0017): the list — Needs a look, Next up, Today, Tomorrow, This
 * week, Anytime — with Done and +1 hour on each row, answering or dismissing what needs a look,
 * and the capture bar. Everything goes through the client from ClientProvider.
 */
export default function App({ devTools }: AppProps) {
  const { client, kind } = useClient()
  const connection = useConnection()
  const [load, setLoad] = useState<Load>({ status: 'loading' })
  const [busy, setBusy] = useState<ReadonlySet<number>>(new Set())
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState<Notice | null>(null)
  const [weekOpen, setWeekOpen] = useState(false)
  const [help, setHelp] = useState(false)
  // Regrouped on every refresh and at midnight: `now` is state so a tick can move the day on.
  const [now, setNow] = useState(() => new Date())
  const captureRef = useRef<HTMLInputElement>(null)
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
    void fetchFeed(client).then((next) => {
      if (fetches.isLatest(mine)) {
        setLoad(next)
        setNow(new Date())
      }
    })
    return () => {
      fetches.next() // whatever is in flight is for a client that's gone
    }
  }, [client, fetches])

  /** Fetch again after a change or on a timer; a failure keeps what's on screen. */
  const refresh = useCallback(async () => {
    const mine = fetches.next()
    const next = await fetchFeed(client)
    if (fetches.isLatest(mine)) {
      if (next.status === 'ready') setLoad(next)
      setNow(new Date()) // the groups move on even when the fetch failed
    }
  }, [client, fetches])

  /** Load again through the loading state, so a failure shows in place of the list. */
  const retry = () => {
    const mine = fetches.next()
    setLoad({ status: 'loading' })
    void fetchFeed(client).then((next) => {
      if (fetches.isLatest(mine)) {
        setLoad(next)
        setNow(new Date())
      }
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

  /** Answer what a reading needs (#126). Throws so the card can show why an answer didn't take. */
  const answer = async (item: Item, phrase: string) => {
    setCardBusy(item.id, true)
    try {
      const filled = await client.fillGap(item.id, phrase)
      say(
        filled.due_at
          ? `Set for ${filled.when}.`
          : filled.kind === 'task'
            ? 'Kept under Anytime.'
            : 'Saved.',
      )
      await refresh()
    } finally {
      setCardBusy(item.id, false)
    }
  }

  const dismiss = async (item: Item) => {
    if (busy.has(item.id)) return
    setCardBusy(item.id, true)
    try {
      await client.dismissGap(item.id)
      say(`Dismissed "${item.text}".`)
      await refresh()
    } catch (error) {
      say(`Couldn't dismiss it: ${messageOf(error)}`)
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

  const ready = load.status === 'ready' ? load : null
  const groups = useMemo(
    () => (ready ? groupFeed(ready.feed, now, ready.timeZone) : null),
    [ready, now],
  )

  // Keyboard (#22): j/k walk the rows, d/s act on the focused one, a answers it, / captures.
  const rows = useMemo(() => (groups ? rowsOf(groups, weekOpen) : []), [groups, weekOpen])
  const [cursor, setCursor] = useState<string | null>(null)
  const move = useCallback(
    (step: 1 | -1) => {
      if (rows.length === 0) return
      const at = rows.findIndex((row) => rowId(row) === cursor)
      const next = rows[Math.min(rows.length - 1, Math.max(0, at + step))] ?? rows[0]
      if (!next) return
      const id = rowId(next)
      setCursor(id)
      document.getElementById(id)?.focus()
    },
    [rows, cursor],
  )
  const focused = rows.find((row) => rowId(row) === cursor) ?? null
  const shortcuts = useMemo(
    () => ({
      capture: () => captureRef.current?.focus(),
      down: () => {
        move(1)
      },
      up: () => {
        move(-1)
      },
      done: () => {
        if (!ready || !focused) return
        if (focused.kind === 'occurrence' && focused.occurrence.acts)
          void act(focused.occurrence.item, 'done', ready.timeZone)
        else if (focused.kind === 'task') void act(focused.item, 'done', ready.timeZone)
      },
      snooze: () => {
        if (!ready || !focused) return
        if (focused.kind === 'occurrence' && focused.occurrence.acts)
          void act(focused.occurrence.item, 'snooze', ready.timeZone)
        else if (focused.kind === 'task') void act(focused.item, 'snooze', ready.timeZone)
      },
      answer: () => {
        if (focused?.kind !== 'needs') return
        document
          .getElementById(rowId(focused))
          ?.querySelector<HTMLButtonElement>('[data-shortcut="answer"]')
          ?.click()
      },
      help: () => {
        setHelp((open) => !open)
      },
      escape: () => {
        setHelp(false)
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- act closes over state read at call time
    [move, focused, ready],
  )
  useShortcuts(shortcuts, !signedOut)

  const pillName = kind === 'mock' ? 'Sample data' : 'Connected'

  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>{ready ? headerDate(now, ready.timeZone) : 'Maestro'}</h1>
          <ConnectionPill state={connection} serverName={pillName} />
        </div>
        {groups && (
          <p className={styles.summary}>
            {groups.leftToday} left today
            {groups.needsLook.length > 0 &&
              ` · ${String(groups.needsLook.length)} need${groups.needsLook.length === 1 ? 's' : ''} a look`}
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

        {!signedOut && ready && groups && (
          <FeedList
            groups={groups}
            timeZone={ready.timeZone}
            busy={busy}
            weekOpen={weekOpen}
            onToggleWeek={() => {
              setWeekOpen((open) => !open)
            }}
            onDone={(i) => void act(i, 'done', ready.timeZone)}
            onSnooze={(i) => void act(i, 'snooze', ready.timeZone)}
            onAnswer={answer}
            onDismiss={(i) => void dismiss(i)}
          />
        )}

        {devTools?.(retry)}
      </main>

      {help && (
        <div className={styles.shortcuts} role="dialog" aria-label="Keyboard shortcuts">
          <h2 className={styles.shortcutsTitle}>Keyboard shortcuts</h2>
          <dl className={styles.shortcutList}>
            {SHORTCUT_LIST.map(([key, what]) => (
              <div key={key} className={styles.shortcut}>
                <dt className={styles.shortcutKey}>
                  <kbd>{key}</kbd>
                </dt>
                <dd className={styles.shortcutWhat}>{what}</dd>
              </div>
            ))}
          </dl>
          <button
            type="button"
            className={styles.helpButton}
            onClick={() => {
              setHelp(false)
            }}
          >
            Close
          </button>
        </div>
      )}

      <footer className={styles.footer}>
        <Toast message={toast?.text ?? null} messageKey={toast?.id} onDismiss={dismissToast} />
        <CaptureBar
          value={draft}
          onChange={setDraft}
          onSubmit={(text) => {
            if (!saving) void capture(text)
          }}
          inputRef={captureRef}
        />
      </footer>
    </div>
  )
}
