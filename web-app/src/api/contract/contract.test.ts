// Contract tests (#101): the same scenarios against every MaestroClient, so a screen sees the same
// behaviour whichever one it is given. HttpClient runs against fakeServer (FastAPI's answers, no
// network); running it against a real `maestro serve` is left to #108.

import { describe, expect, it } from 'vitest'
import { SUPPORT, type MaestroClient } from '../client.ts'
import { ApiError } from '../errors.ts'
import { HttpClient } from '../http/HttpClient.ts'
import { MockClient, failWith } from '../mock/MockClient.ts'
import { fakeServer } from './fakeServer.ts'

const TZ = 'Asia/Kolkata'
const START = '2026-11-05T14:00:00+05:30'

const mock = () => new MockClient({ now: () => new Date(START), timeZone: TZ, storage: null })

const clients: [string, () => MaestroClient][] = [
  ['MockClient', mock],
  [
    'HttpClient',
    () => new HttpClient({ token: 't0ken', fetch: fakeServer(mock(), { token: 't0ken' }).fetch }),
  ],
]

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('Expected the call to fail')
}

describe.each(clients)('%s', (_, make) => {
  it('captures and reads back the same item', async () => {
    const client = make()
    const item = await client.capture('text me tomorrow at 9am to call the bank')

    expect(item).toMatchObject({ text: 'Call the bank', due_at: '2026-11-06T09:00:00+05:30' })
    expect(await client.getItem(item.id)).toEqual(item)
  })

  it('lists with the same filters and order', async () => {
    const client = make()

    const open = await client.listItems()
    expect(open[0]?.text).toBe('Call the bank about the card')
    expect((await client.listItems({ include_done: true })).length).toBe(open.length + 1)
    expect((await client.listItems({ unacknowledged: true })).map((i) => i.id)).toEqual([1])
    expect((await client.listItems({ q: 'car service' })).map((i) => i.id)).toEqual([9, 12])
  })

  it('keeps a recurring series going after done (ADR 0009)', async () => {
    const client = make()

    expect(await client.done(3)).toMatchObject({
      status: 'pending',
      due_at: '2026-11-12T17:00:00+05:30',
    })
  })

  it('snoozes, stops and deletes', async () => {
    const client = make()

    expect((await client.snooze(2, '10m')).due_at).toBe('2026-11-05T14:10:00+05:30')
    expect((await client.stop(3)).status).toBe('done')
    await client.deleteItem(4)
    expect((await rejection(client.getItem(4))).kind).toBe('not_found')
  })

  it('fails the same way', async () => {
    const client = make()

    expect(await rejection(client.getItem(999))).toMatchObject({
      kind: 'not_found',
      message: 'No item #999',
    })
    expect(await rejection(client.snooze(2, 'xx'))).toMatchObject({
      kind: 'invalid',
      message: "Can't read duration 'xx'; try 10m, 2h or 1d",
    })
    expect((await rejection(client.capture(''))).kind).toBe('invalid')
  })

  it('searches notes, newest first', async () => {
    const client = make()

    expect((await client.searchNotes('')).map((i) => i.id)).toEqual([10, 11, 12])
    expect((await client.searchNotes('plumber')).map((i) => i.id)).toEqual([11])
  })

  it('reports the server timezone', async () => {
    expect((await make().health()).timezone).toBe(TZ)
  })

  it('serves the same feed (#22)', async () => {
    const client = make()
    const feed = await client.feed()

    expect(feed.timezone).toBe(TZ)
    expect(feed.needs_look.map((i) => i.id)).toEqual([13])
    expect(feed.upcoming.slice(0, 3).map((o) => [o.item.id, o.at, o.state, o.acts])).toEqual([
      [1, '2026-11-05T13:20:00+05:30', 'awaiting', true],
      [5, '2026-11-05T14:20:00+05:30', 'snoozed', true],
      [2, '2026-11-05T15:30:00+05:30', 'scheduled', true],
    ])
    expect(feed.anytime).toEqual([])
    expect(feed.later).toBe(0)
    expect((await client.feed(1)).upcoming.every((o) => o.at < '2026-11-06')).toBe(true)
  })

  it('undoes a capture the same way (#104)', async () => {
    const client = make()
    const result = await client.captureSplit('renew the passport')
    const saved = result.added[0]?.id ?? -1

    await client.undoCapture(result.undo?.id ?? -1)
    expect((await rejection(client.getItem(saved))).kind).toBe('not_found')
    await client.undoCapture(result.undo?.id ?? -1) // undone is undone: yes again, not an error
    expect((await rejection(client.undoCapture(999))).kind).toBe('not_found')
  })

  it('fills and dismisses what needs a look the same way (#126)', async () => {
    const client = make()
    expect(await rejection(client.fillGap(13, 'no idea'))).toMatchObject({
      kind: 'invalid',
      message: "Couldn't read a time in that; #13 still needs a time",
    })
    const filled = await client.fillGap(13, 'tomorrow at 9am')
    expect([filled.kind, filled.due_at, filled.gap]).toEqual([
      'reminder',
      '2026-11-06T09:00:00+05:30',
      null,
    ])
    expect((await rejection(client.fillGap(13, '5pm'))).kind).toBe('not_found') // no gap any more
    expect((await rejection(client.dismissGap(1))).kind).toBe('not_found')

    const other = make()
    await other.dismissGap(13)
    expect((await other.feed()).needs_look).toEqual([])
  })

  it('rejects what the backend does not have yet', async () => {
    const client = make()
    for (const [method, support] of Object.entries(SUPPORT)) {
      if (support.level !== 'none') continue
      // Called on the client, without arguments: they reject before reading any.
      const calls = client as unknown as Record<string, () => Promise<unknown>>
      expect((await rejection(calls[method]?.() ?? Promise.resolve())).kind, method).toBe(
        'unsupported',
      )
    }
  })

  it('starts connected', () => {
    expect(make().connection()).toEqual({ status: 'connected' })
  })
})

// The 401 and 429 lock (#112): the same on sample data as against a server. `attempts` counts what
// reached the server (or the mock's failure rule); a locked client adds nothing to it.
interface Locking {
  client: MaestroClient
  attempts: () => number
  advance: (ms: number) => void
}

function lockingClients(kind: 'unauthorized' | 'rate_limited'): [string, () => Locking][] {
  const injected = () => failWith(kind, { times: 1, retryAfterSeconds: 30 })
  return [
    [
      'MockClient',
      () => {
        let now = new Date(START).getTime()
        let attempts = 0
        const rule = injected()
        const client = new MockClient({
          now: () => new Date(now),
          timeZone: TZ,
          storage: null,
          masterToken: 't0ken',
          failure: (method) => {
            attempts += 1
            return rule(method)
          },
        })
        return { client, attempts: () => attempts, advance: (ms) => (now += ms) }
      },
    ],
    [
      'HttpClient',
      () => {
        let now = new Date(START).getTime()
        // A 401 comes from the fake server's own token check; a 429 is injected in the backend,
        // which shares the clock because it locks on it too and must lift it in step.
        const backend = new MockClient({
          now: () => new Date(now),
          timeZone: TZ,
          storage: null,
          failure: kind === 'rate_limited' ? injected() : null,
        })
        const server = fakeServer(backend, { token: 't0ken' })
        const client = new HttpClient({
          token: kind === 'unauthorized' ? 'wrong' : 't0ken',
          fetch: server.fetch,
          now: () => now,
        })
        return { client, attempts: () => server.requests.length, advance: (ms) => (now += ms) }
      },
    ],
  ]
}

describe.each(lockingClients('unauthorized'))('%s signing in (ADR 0018)', (_, make) => {
  it('takes the master token once, keeps a device token, and lists itself', async () => {
    const { client, attempts } = make()
    await rejection(client.listItems()) // signed out
    const before = attempts()

    expect((await rejection(client.registerDevice('wrong', 'Phone'))).kind).toBe('unauthorized')
    expect(attempts()).toBe(before + 1) // sign-in gets past the lock, right or wrong

    const signedIn = await client.registerDevice('t0ken', "Sachin's iPhone")
    expect(signedIn.token).toMatch(/^mdt_/)
    expect(signedIn.device).toMatchObject({ name: "Sachin's iPhone", current: false })
    expect((await client.listItems()).length).toBeGreaterThan(0)
    expect(client.connection()).toEqual({ status: 'connected' })
    expect((await client.listDevices()).map((d) => d.current)).toEqual([true])
  })

  it('captures the same way through /capture', async () => {
    const { client } = make()
    await rejection(client.listItems()) // signed out first, as the app would be
    await client.registerDevice('t0ken', 'Phone')
    const result = await client.captureSplit('text me tomorrow at 9am to call the bank')

    expect(result.added.map((i) => i.text)).toEqual(['Call the bank'])
    expect(result.needs_look).toEqual([])
    expect(result.undo?.id).toBe(1)
    expect(result.proposal).toBeNull()
    expect((await rejection(client.captureSplit(''))).kind).toBe('invalid')
  })
})

describe.each(lockingClients('unauthorized'))('%s after a 401', (_, make) => {
  it('stops trying until a token is set', async () => {
    const { client, attempts } = make()

    expect((await rejection(client.listItems())).kind).toBe('unauthorized')
    expect(client.connection()).toEqual({ status: 'signed_out' })
    expect((await rejection(client.health())).kind).toBe('unauthorized')
    expect(attempts()).toBe(1)

    client.setToken('t0ken')
    expect((await client.health()).status).toBe('ok')
    expect(client.connection()).toEqual({ status: 'connected' })
  })
})

describe.each(lockingClients('rate_limited'))('%s after a 429', (_, make) => {
  it('stops trying until Retry-After has passed, then carries on', async () => {
    const { client, attempts, advance } = make()

    expect((await rejection(client.listItems())).kind).toBe('rate_limited')
    expect(client.connection()).toEqual({ status: 'rate_limited', retry_after_seconds: 30 })
    advance(29_999)
    expect((await rejection(client.listItems())).kind).toBe('rate_limited')
    expect(attempts()).toBe(1)

    advance(1)
    expect((await client.listItems()).length).toBeGreaterThan(0)
    expect(client.connection()).toEqual({ status: 'connected' })
  })
})
