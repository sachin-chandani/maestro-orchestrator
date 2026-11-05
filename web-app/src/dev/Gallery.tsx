import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ChipKind, ConnectionState, Item, PreviewChip } from '../api/types.ts'
import { MockClient } from '../api/mock/MockClient.ts'
import { CaptureBar } from '../components/CaptureBar.tsx'
import { Chip } from '../components/Chip.tsx'
import { ConnectionPill } from '../components/ConnectionPill.tsx'
import { EmptyState } from '../components/EmptyState.tsx'
import { ReminderCard } from '../components/ReminderCard.tsx'
import { TabBar } from '../components/TabBar.tsx'
import { Toast } from '../components/Toast.tsx'
import styles from './Gallery.module.css'

// Dev only (`npm run dev`, then /app/?gallery): every core component (#102) in every state, on the
// mock's sample data, for checking by eye at 360px and desktop width, in light and dark. Not in
// production builds (see main.tsx).

const KINDS: [ChipKind, string][] = [
  ['time', 'Tomorrow'],
  ['lead', '1 hour before'],
  ['recurrence', 'Every week'],
  ['channel', 'Text me'],
  ['urgent', 'Urgent'],
  ['escalation', 'Call if not done in 15m'],
  ['streak', '6-week streak'],
  ['list', 'List: shopping'],
  ['note', 'Note, no time'],
]

const PREVIEW: PreviewChip[] = [
  { kind: 'time', label: 'Tomorrow' },
  { kind: 'time', label: '9:00 am' },
  { kind: 'channel', label: 'Text me' },
]

const CONNECTIONS: ConnectionState[] = [
  { status: 'connected' },
  { status: 'offline', queued: 2 },
  { status: 'signed_out' },
  { status: 'rate_limited', retry_after_seconds: 30 },
]

const TABS = [
  { id: 'today', label: 'Today' },
  { id: 'lists', label: 'Lists' },
  { id: 'notes', label: 'Notes' },
] as const

export default function Gallery() {
  // In memory only, so the gallery never touches the app's saved mock data.
  const client = useMemo(() => new MockClient({ storage: null }), [])
  const [items, setItems] = useState<Item[]>([])
  const [timeZone, setTimeZone] = useState('UTC')
  const [draft, setDraft] = useState('')
  const [toast, setToast] = useState<string | null>(null)
  const [tab, setTab] = useState<(typeof TABS)[number]['id']>('today')
  const [busy, setBusy] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    setItems(await client.listItems({ include_done: true }))
  }, [client])

  useEffect(() => {
    void client.health().then((h) => {
      setTimeZone(h.timezone)
    })
    void client.listItems({ include_done: true }).then(setItems)
  }, [client])

  const act = async (item: Item, action: 'done' | 'snooze') => {
    setBusy(item.id)
    try {
      const after =
        action === 'done' ? await client.done(item.id) : await client.snooze(item.id, '1h')
      setToast(
        action === 'done'
          ? 'Done. Any pending SMS or call for it is cancelled.'
          : `Snoozed to ${after.when}. Escalation restarts from then.`,
      )
      await refresh()
    } finally {
      setBusy(null)
    }
  }

  const reminders = items.filter((i) => i.kind === 'reminder')
  const dismiss = useCallback(() => {
    setToast(null)
  }, [])

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Components</h1>
          <ConnectionPill state={{ status: 'connected' }} serverName="Home server" />
        </div>
        <p className={styles.summary}>Dev gallery · mock data · {timeZone}</p>
      </header>

      <main>
        <section className={styles.section} aria-labelledby="g-chips">
          <h2 className={styles.heading} id="g-chips">
            Chip
          </h2>
          <div className={styles.row}>
            {KINDS.map(([kind, label]) => (
              <Chip key={kind} kind={kind} label={label} />
            ))}
          </div>
        </section>

        <section className={styles.section} aria-labelledby="g-pill">
          <h2 className={styles.heading} id="g-pill">
            ConnectionPill
          </h2>
          <div className={styles.row}>
            {CONNECTIONS.map((state) => (
              <ConnectionPill key={state.status} state={state} serverName="Home server" />
            ))}
          </div>
        </section>

        <section className={styles.section} aria-labelledby="g-cards">
          <h2 className={styles.heading} id="g-cards">
            ReminderCard
          </h2>
          {reminders.map((item) => (
            <ReminderCard
              key={item.id}
              item={item}
              timeZone={timeZone}
              busy={busy === item.id}
              onDone={(i) => void act(i, 'done')}
              onSnooze={(i) => void act(i, 'snooze')}
            />
          ))}
          <div className={styles.row}>
            <button
              type="button"
              className={styles.button}
              onClick={() => {
                client.reset()
                void refresh()
              }}
            >
              Reset sample data
            </button>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="g-empty">
          <h2 className={styles.heading} id="g-empty">
            EmptyState
          </h2>
          <EmptyState title="No lists yet" hint="Try “add oat milk to the shopping list” below." />
        </section>

        <section className={styles.section} aria-labelledby="g-toast">
          <h2 className={styles.heading} id="g-toast">
            Toast
          </h2>
          <div className={styles.row}>
            <button
              type="button"
              className={styles.button}
              onClick={() => {
                setToast('Reminder set. It will reach you even if this app is closed.')
              }}
            >
              Show a toast
            </button>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="g-bars">
          <h2 className={styles.heading} id="g-bars">
            CaptureBar and TabBar
          </h2>
          <div className={styles.frame}>
            <Toast message={toast} onDismiss={dismiss} />
            <CaptureBar
              value={draft}
              onChange={setDraft}
              onSubmit={(text) => {
                setToast(`Would capture “${text}”`)
                setDraft('')
              }}
              preview={draft ? PREVIEW : []}
            />
            <TabBar tabs={TABS} current={tab} onSelect={setTab} />
          </div>
        </section>
      </main>
    </div>
  )
}
