import { afterEach, describe, expect, it, vi } from 'vitest'
import { SUPPORT, type MaestroClient } from '../client.ts'
import { ApiError, isApiError } from '../errors.ts'
import type { ConnectionState, Item } from '../types.ts'
import { MockClient, failWith, type MockClientOptions } from './MockClient.ts'
import { STORAGE_KEY, type KeyValueStorage } from './store.ts'

const TZ = 'Asia/Kolkata'
// Thursday 5 November 2026, 14:00 in Kolkata.
const START = '2026-11-05T14:00:00+05:30'

function memoryStorage(): KeyValueStorage & { data: Map<string, string> } {
  const data = new Map<string, string>()
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  }
}

function setup(options: MockClientOptions = {}) {
  let now = new Date(START)
  const storage = memoryStorage()
  const client = new MockClient({ now: () => now, timeZone: TZ, storage, ...options })
  return {
    client,
    storage,
    setNow: (iso: string) => {
      now = new Date(iso)
    },
  }
}

const byText = (items: Item[], text: string) => {
  const item = items.find((i) => i.text === text)
  if (!item) throw new Error(`No item "${text}" in ${items.map((i) => i.text).join(', ')}`)
  return item
}

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('Expected the call to fail')
}

afterEach(() => {
  vi.useRealTimers()
})

describe('seed data', () => {
  it('places the sample reminders around now, in the order the backend lists them', async () => {
    const { client } = setup()
    const items = await client.listItems()

    expect(items.map((i) => [i.text, i.when])).toEqual([
      ['Call the bank about the card', 'Thu 05 Nov 13:20'],
      ['Stretch for ten minutes', 'Thu 05 Nov 14:20 (snoozed), then Fri 06 Nov 07:30, daily'],
      ['Pick up the kids from practice', 'Thu 05 Nov 15:30'],
      ['Water the plants', 'Thu 05 Nov 17:00, every Thursday'],
      ['Renew the car insurance', 'Thu 05 Nov 23:15'],
      ['Dentist', 'Fri 06 Nov 15:00'],
      ['Send the school trip form', 'Sun 08 Nov 09:00'],
      ['Put the bins out', 'Wed 11 Nov 08:30, every Wednesday'],
      ['Car service is due at 60,000 km', 'note'],
      ['The plumber the neighbours liked is called Alex; number is on the fridge', 'note'],
      ['Gift idea for Sam: the blue scarf from the Saturday market', 'note'],
    ])
  })

  it('moves with the clock, so it always looks current', async () => {
    const { client } = setup({ now: () => new Date('2027-02-10T09:00:00+05:30') })
    const kids = byText(await client.listItems(), 'Pick up the kids from practice')

    expect(kids.due_at).toBe('2027-02-10T10:30:00+05:30')
  })

  it('has every state the screens draw', async () => {
    const { client } = setup()
    const items = await client.listItems({ include_done: true })

    const bank = byText(items, 'Call the bank about the card')
    expect(bank).toMatchObject({ status: 'fired', awaiting_acknowledgement: true })
    expect(byText(items, 'Renew the car insurance').held_until).toBe('2026-11-06T07:00:00+05:30')
    expect(byText(items, 'Pick up the kids from practice')).toMatchObject({
      urgent: true,
      escalation_label: 'Call if not done in 15m',
    })
    expect(byText(items, 'Pick up the kids from practice').held_until).toBeUndefined()
    expect(byText(items, 'Water the plants')).toMatchObject({
      streak: 6,
      recurrence_label: 'every Thursday',
    })
    expect(byText(items, 'Dentist').lead_time_label).toBe('1 hour before')
    expect(byText(items, 'Book the car service').status).toBe('done')
    expect(items.filter((i) => i.kind === 'note').map((i) => i.source)).toEqual([
      'twilio_sms',
      'twilio_call',
      'telegram',
    ])
  })
  it.each(['Asia/Kolkata', 'America/New_York'])(
    'holds together whatever hour it is seeded at (%s, around a DST change)',
    async (timeZone) => {
      for (let hour = 0; hour < 24; hour++) {
        const now = new Date(Date.UTC(2026, 10, 1, hour, 7)) // 1 Nov 2026: New York leaves DST
        const client = new MockClient({ now: () => now, timeZone, storage: null })
        const items = await client.listItems({ include_done: true })

        expect(items).toHaveLength(12)
        for (const item of items) {
          if (item.snoozed_until && item.due_at) {
            expect(new Date(item.snoozed_until) < new Date(item.due_at), item.text).toBe(true)
          }
        }
        expect(items.filter((i) => i.kind === 'reminder' && i.status !== 'done').length).toBe(8)
      }
    },
  )
})

describe('items', () => {
  it('captures a reminder, with its channel', async () => {
    const { client } = setup()
    const item = await client.capture('text me tomorrow at 9am to call the bank')

    expect(item).toMatchObject({
      id: 13,
      text: 'Call the bank',
      kind: 'reminder',
      status: 'pending',
      due_at: '2026-11-06T09:00:00+05:30',
      channels: ['twilio_sms'],
      urgent: false,
      created_at: START,
      source_text: 'text me tomorrow at 9am to call the bank',
    })
    expect(await client.getItem(13)).toEqual(item)
  })

  it('captures untimed text as a note', async () => {
    const { client } = setup()
    expect(await client.capture('the wifi password is on the router')).toMatchObject({
      kind: 'note',
      due_at: null,
      when: 'note',
    })
  })

  it('captures an untimed thing to do as a task', async () => {
    const { client } = setup()
    expect(await client.capture('renew the passport')).toMatchObject({
      text: 'Renew the passport',
      kind: 'task',
      due_at: null,
      when: 'task',
    })
    expect(await client.capture('todo: sort out the garage')).toMatchObject({
      text: 'Sort out the garage',
      kind: 'task',
    })
    // a statement that starts like a to-do is still a note
    expect(await client.capture('call centre number is 0800 123 456')).toMatchObject({
      kind: 'note',
    })
  })

  it('captures a recurring reminder', async () => {
    const { client } = setup()
    expect(await client.capture('water the ferns every monday at 8am')).toMatchObject({
      text: 'Water the ferns',
      due_at: '2026-11-09T08:00:00+05:30',
      recurrence: 'weekly:0',
      recurrence_label: 'every Monday',
    })
  })

  it('refuses an empty capture', async () => {
    const { client } = setup()
    expect((await rejection(client.capture('   '))).kind).toBe('invalid')
  })

  it('says which item is missing', async () => {
    const { client } = setup()
    const error = await rejection(client.getItem(999))

    expect(error).toMatchObject({ kind: 'not_found', status: 404, message: 'No item #999' })
  })

  it('filters like GET /items', async () => {
    const { client } = setup()

    const all = await client.listItems()
    expect(all.some((i) => i.status === 'done')).toBe(false)
    expect((await client.listItems({ include_done: true })).length).toBe(all.length + 1)
    // A search covers done items too, newest first.
    // Substring match, case-insensitive, like the backend's LIKE: "car" would also find "card".
    expect((await client.listItems({ q: 'car service' })).map((i) => i.text)).toEqual([
      'Book the car service',
      'Car service is due at 60,000 km',
    ])
    expect((await client.listItems({ unacknowledged: true })).map((i) => i.text)).toEqual([
      'Call the bank about the card',
    ])
  })

  it('marks a one-off reminder done', async () => {
    const { client } = setup()
    const bank = byText(await client.listItems(), 'Call the bank about the card')
    const done = await client.done(bank.id)

    expect(done).toMatchObject({ status: 'done', awaiting_acknowledgement: false })
    expect((await client.listItems()).map((i) => i.id)).not.toContain(bank.id)
    expect(await client.listItems({ unacknowledged: true })).toEqual([])
  })

  it('snoozes a one-off reminder to a new time', async () => {
    const { client } = setup()
    const kids = byText(await client.listItems(), 'Pick up the kids from practice')

    expect(await client.snooze(kids.id, '10m')).toMatchObject({
      status: 'pending',
      due_at: '2026-11-05T14:10:00+05:30',
      snoozed_until: null,
    })
  })

  it('makes a snoozed note a reminder, as the backend does', async () => {
    const { client } = setup()
    const note = await client.capture('the wifi password is on the router')

    expect(await client.snooze(note.id, '2h')).toMatchObject({
      kind: 'reminder',
      due_at: '2026-11-05T16:00:00+05:30',
    })
  })

  it('refuses a snooze it cannot read, with the backend message, and changes nothing', async () => {
    const { client } = setup()
    const kids = byText(await client.listItems(), 'Pick up the kids from practice')
    const error = await rejection(client.snooze(kids.id, 'xx'))

    expect(error).toMatchObject({
      kind: 'invalid',
      status: 422,
      message: "Can't read duration 'xx'; try 10m, 2h or 1d",
    })
    expect(await client.getItem(kids.id)).toEqual(kids)
  })

  it('deletes an item', async () => {
    const { client } = setup()
    await client.deleteItem(1)

    expect((await rejection(client.getItem(1))).kind).toBe('not_found')
    expect((await rejection(client.deleteItem(1))).kind).toBe('not_found')
  })

  it('searches notes only, newest first', async () => {
    const { client } = setup()

    expect((await client.searchNotes('')).map((i) => i.text)).toEqual([
      'Gift idea for Sam: the blue scarf from the Saturday market',
      'The plumber the neighbours liked is called Alex; number is on the fridge',
      'Car service is due at 60,000 km',
    ])
    expect((await client.searchNotes('CAR SERVICE')).map((i) => i.text)).toEqual([
      'Car service is due at 60,000 km',
    ])
  })

  it('hands out copies, so callers cannot change its data', async () => {
    const { client } = setup()
    const item = await client.getItem(2)
    item.text = 'changed'

    expect((await client.getItem(2)).text).toBe('Pick up the kids from practice')
  })
})

describe('recurring reminders (ADR 0009)', () => {
  const WATER = 3

  it('done before it goes out skips this occurrence; the series continues', async () => {
    const { client } = setup()

    expect(await client.done(WATER)).toMatchObject({
      status: 'pending',
      due_at: '2026-11-12T17:00:00+05:30',
      streak: 6,
    })
  })

  it('done after it went out answers that occurrence, without skipping the next', async () => {
    const { client, setNow } = setup()
    setNow('2026-11-05T17:05:00+05:30')
    expect(await client.getItem(WATER)).toMatchObject({
      awaiting_acknowledgement: true,
      due_at: '2026-11-12T17:00:00+05:30',
    })

    expect(await client.done(WATER)).toMatchObject({
      awaiting_acknowledgement: false,
      due_at: '2026-11-12T17:00:00+05:30',
    })
  })

  it('snooze re-sends one occurrence and keeps the series on its own time', async () => {
    const { client, setNow } = setup()
    setNow('2026-11-05T17:05:00+05:30')

    expect(await client.snooze(WATER, '1h')).toMatchObject({
      snoozed_until: '2026-11-05T18:05:00+05:30',
      due_at: '2026-11-12T17:00:00+05:30',
      awaiting_acknowledgement: false,
    })

    setNow('2026-11-05T18:06:00+05:30')
    expect(await client.getItem(WATER)).toMatchObject({
      snoozed_until: null,
      due_at: '2026-11-12T17:00:00+05:30',
      awaiting_acknowledgement: true,
    })
  })

  it('done on a snoozed series drops only the re-send', async () => {
    const { client } = setup()
    const stretch = byText(await client.listItems(), 'Stretch for ten minutes')

    expect(await client.done(stretch.id)).toMatchObject({
      snoozed_until: null,
      due_at: '2026-11-06T07:30:00+05:30',
    })
  })

  it('stop ends the whole series', async () => {
    const { client } = setup()

    expect(await client.stop(WATER)).toMatchObject({ status: 'done', snoozed_until: null })
    expect((await client.listItems()).map((i) => i.id)).not.toContain(WATER)
  })

  it('sends one catch-up notice after being away, not one per missed occurrence', async () => {
    const { client, setNow } = setup()
    setNow('2026-11-20T12:00:00+05:30') // two Thursdays later

    expect(await client.getItem(WATER)).toMatchObject({
      due_at: '2026-11-26T17:00:00+05:30',
      awaiting_acknowledgement: true,
    })
  })
})

describe('quiet hours', () => {
  const INSURANCE = 4

  it('hold a reminder that is not urgent until they end, then send it once', async () => {
    const { client, setNow } = setup()

    setNow('2026-11-05T23:20:00+05:30')
    expect(await client.getItem(INSURANCE)).toMatchObject({
      status: 'pending',
      awaiting_acknowledgement: false,
      held_until: '2026-11-06T07:00:00+05:30',
    })

    setNow('2026-11-06T07:00:00+05:30')
    const sent = await client.getItem(INSURANCE)
    expect(sent).toMatchObject({ status: 'fired', awaiting_acknowledgement: true })
    expect(sent.held_until).toBeUndefined()
  })

  it('let urgent reminders through', async () => {
    const { client, setNow } = setup()
    const insulin = await client.capture('!take insulin at 11pm')
    expect(insulin).toMatchObject({ urgent: true, text: 'Take insulin' })
    expect(insulin.held_until).toBeUndefined()

    setNow('2026-11-05T23:00:00+05:30')
    expect(await client.getItem(insulin.id)).toMatchObject({ awaiting_acknowledgement: true })
  })

  it('can be turned off', async () => {
    const { client } = setup({ quietHours: null })
    expect((await client.getItem(INSURANCE)).held_until).toBeUndefined()
  })
})

describe('persistence', () => {
  it('survives a reload', async () => {
    const { client, storage } = setup()
    const captured = await client.capture('buy stamps tomorrow at 10am')

    const reloaded = new MockClient({ now: () => new Date(START), timeZone: TZ, storage })
    expect(await reloaded.getItem(captured.id)).toEqual(captured)
  })

  it('saves under a versioned key', async () => {
    const { client, storage } = setup()
    await client.capture('buy stamps tomorrow at 10am')

    expect(STORAGE_KEY).toBe('maestro.mock.v1')
    const saved = JSON.parse(storage.data.get(STORAGE_KEY) ?? '{}') as { version?: number }
    expect(saved.version).toBe(1)
  })

  it.each([
    ['another version', JSON.stringify({ version: 0, state: { items: [], nextId: 1 } })],
    ['unreadable data', '{not json'],
    ['the wrong shape', JSON.stringify({ version: 1, state: { items: 'nope' } })],
  ])('starts again from the seed when it finds %s', async (_, raw) => {
    const storage = memoryStorage()
    storage.data.set(STORAGE_KEY, raw)
    const client = new MockClient({ now: () => new Date(START), timeZone: TZ, storage })

    expect(await client.listItems({ include_done: true })).toHaveLength(12)
  })

  it('resets to the sample data', async () => {
    const { client, storage } = setup()
    const captured = await client.capture('buy stamps tomorrow at 10am')
    await client.deleteItem(1)

    client.reset()
    expect((await rejection(client.getItem(captured.id))).kind).toBe('not_found')
    expect((await client.getItem(1)).text).toBe('Call the bank about the card')

    const reloaded = new MockClient({ now: () => new Date(START), timeZone: TZ, storage })
    expect(await reloaded.listItems({ include_done: true })).toHaveLength(12)
  })

  it('keeps working when storage is unavailable or refuses to save', async () => {
    const full: KeyValueStorage = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException('Quota exceeded', 'QuotaExceededError')
      },
      removeItem: () => undefined,
    }
    for (const storage of [null, full]) {
      const client = new MockClient({ now: () => new Date(START), timeZone: TZ, storage })
      const item = await client.capture('buy stamps tomorrow at 10am')
      expect(await client.getItem(item.id)).toEqual(item)
    }
  })
})

describe('latency', () => {
  it('waits before settling', async () => {
    vi.useFakeTimers()
    const { client } = setup({ latencyMs: 300 })
    let settled = false
    const call = client.getItem(1).then(() => (settled = true))

    await vi.advanceTimersByTimeAsync(299)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await call
    expect(settled).toBe(true)
  })

  it('can be changed while running', async () => {
    vi.useFakeTimers()
    const { client } = setup()
    client.setLatency(1000)
    let settled = false
    void client.health().then(() => (settled = true))

    await vi.advanceTimersByTimeAsync(999)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(settled).toBe(true)
  })
})

describe('injected failures', () => {
  it('fail only the chosen methods, the chosen number of times, and change nothing', async () => {
    const { client } = setup({ failure: failWith('server', { methods: ['done'], times: 1 }) })

    const error = await rejection(client.done(1))
    expect(error).toMatchObject({ kind: 'server', status: 500 })
    expect((await client.getItem(1)).status).toBe('fired')

    expect((await client.done(1)).status).toBe('done')
  })

  it('can be switched on and off while running', async () => {
    const { client } = setup()
    client.setFailure(failWith('offline'))
    expect((await rejection(client.listItems())).kind).toBe('offline')

    client.setFailure(null)
    expect((await client.listItems()).length).toBeGreaterThan(0)
  })

  it('drive the connection state, as real failures will', async () => {
    const { client } = setup()
    const seen: ConnectionState[] = []
    const unsubscribe = client.onConnectionChange((state) => seen.push(state))

    client.setFailure(failWith('unauthorized', { times: 1 }))
    await rejection(client.listItems())
    expect(client.connection()).toEqual({ status: 'signed_out' })

    client.setFailure(failWith('rate_limited', { times: 1, retryAfterSeconds: 30 }))
    await rejection(client.listItems())
    client.setFailure(failWith('offline', { times: 1 }))
    await rejection(client.listItems())
    client.setFailure(failWith('not_found', { times: 1 })) // not about the connection
    await rejection(client.listItems())

    await client.listItems()
    expect(seen).toEqual([
      { status: 'signed_out' },
      { status: 'rate_limited', retry_after_seconds: 30 },
      { status: 'offline', queued: 0 },
      { status: 'connected' },
    ])

    unsubscribe()
    client.setFailure(failWith('offline', { times: 1 }))
    await rejection(client.listItems())
    expect(seen).toHaveLength(4)
  })
})

describe('endpoints the backend does not have yet', () => {
  // Called through the interface, as screens do.
  const calls: Partial<Record<keyof MaestroClient, (c: MaestroClient) => Promise<unknown>>> = {
    editItem: (c) => c.editItem(1, { text: 'x' }),
    parse: (c) => c.parse('call mom'),
    listLists: (c) => c.listLists(),
    createList: (c) => c.createList('Shopping'),
    addToList: (c) => c.addToList(1, ['milk']),
    setChecked: (c) => c.setChecked(1, 1, true),
    clearChecked: (c) => c.clearChecked(1),
    pair: (c) => c.pair({ server_url: 'https://m.example', code: '123456', device_name: 'Phone' }),
    listDevices: (c) => c.listDevices(),
    revokeDevice: (c) => c.revokeDevice(1),
    getSettings: (c) => c.getSettings(),
    updateSettings: (c) => c.updateSettings({ quiet_hours: null }),
    sendTest: (c) => c.sendTest('telegram'),
  }

  it('covers every method SUPPORT marks as not built', () => {
    const none = Object.entries(SUPPORT).filter(([, s]) => s.level === 'none')
    expect(Object.keys(calls).sort()).toEqual(none.map(([m]) => m).sort())
  })

  it.each(Object.entries(calls))('%s is unsupported and names its issue', async (method, call) => {
    const { client } = setup()
    const support = SUPPORT[method as keyof MaestroClient]
    const error = await rejection(call(client))

    expect(isApiError(error, 'unsupported')).toBe(true)
    expect(error.message).toContain(`#${String('issue' in support ? support.issue : '')}`)
  })
})
