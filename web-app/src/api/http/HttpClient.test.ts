import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../errors.ts'
import type { ConnectionState } from '../types.ts'
import { HttpClient } from './HttpClient.ts'

// A fetch that answers from a script and records what it was asked. Nothing reaches the network.
function scripted(...answers: (Response | Error)[]) {
  const calls: { url: string; init: RequestInit }[] = []
  const fetchImpl = vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: urlOf(input), init })
    const next = answers.shift() ?? new Response('{}', { status: 200 })
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next)
  })
  return { fetch: fetchImpl as unknown as typeof fetch, calls, mock: fetchImpl }
}

/** The URL a fetch was called with, whichever form it came in. */
const urlOf = (input: RequestInfo | URL): string =>
  typeof input === 'string' ? input : input instanceof URL ? input.href : input.url

const ok = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const fail = (status: number, detail: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify({ detail }), { status, headers })

const headersOf = (init: RequestInit) => new Headers(init.headers)

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
  vi.unstubAllGlobals()
})

describe('requests', () => {
  it('sends the token, says it is the web UI, and posts JSON', async () => {
    const server = scripted(ok({ id: 1 }, 201))
    const client = new HttpClient({
      baseUrl: 'https://maestro.example/',
      token: 't0ken',
      ...server,
    })
    await client.capture('call mom at 5pm')

    const [call] = server.calls
    expect(call?.url).toBe('https://maestro.example/items')
    expect(call?.init.method).toBe('POST')
    expect(call?.init.body).toBe('{"text":"call mom at 5pm"}')
    const headers = headersOf(call?.init ?? {})
    expect(headers.get('Authorization')).toBe('Bearer t0ken')
    expect(headers.get('X-Maestro-Client')).toBe('web')
    expect(headers.get('Content-Type')).toBe('application/json')
  })

  it('sends no Authorization header without a token, and no Content-Type without a body', async () => {
    const server = scripted(ok([]))
    await new HttpClient(server).listItems()

    const headers = headersOf(server.calls[0]?.init ?? {})
    expect(headers.has('Authorization')).toBe(false)
    expect(headers.has('Content-Type')).toBe(false)
    expect(server.calls[0]?.url).toBe('/items')
  })

  it('maps each method to its route', async () => {
    const server = scripted()
    const client = new HttpClient(server)
    await client.listItems({ include_done: true, q: 'bins & bags', unacknowledged: true })
    await client.getItem(7)
    await client.done(7)
    await client.snooze(7, '2h')
    await client.stop(7)
    await client.health()

    expect(server.calls.map((c) => `${c.init.method ?? ''} ${c.url}`)).toEqual([
      'GET /items?include_done=true&q=bins+%26+bags&unacknowledged=true',
      'GET /items/7',
      'POST /items/7/done',
      'POST /items/7/snooze',
      'POST /items/7/stop',
      'GET /health',
    ])
    expect(server.calls[3]?.init.body).toBe('{"duration":"2h"}')
  })

  it('reads a 204 as no content', async () => {
    const server = scripted(new Response(null, { status: 204 }))
    await expect(new HttpClient(server).deleteItem(7)).resolves.toBeUndefined()
    expect(server.calls[0]?.init.method).toBe('DELETE')
  })

  it("uses the browser's fetch by default", async () => {
    const browserFetch = vi.fn(() => Promise.resolve(ok({ status: 'ok' })))
    vi.stubGlobal('fetch', browserFetch)

    await new HttpClient().health()
    expect(browserFetch).toHaveBeenCalledOnce()
  })
})

describe('errors', () => {
  it('uses the server message for a missing item', async () => {
    const client = new HttpClient(scripted(fail(404, 'No item #7')))
    expect(await rejection(client.getItem(7))).toMatchObject({
      kind: 'not_found',
      status: 404,
      message: 'No item #7',
    })
  })

  it('joins request-validation messages', async () => {
    const detail = [{ loc: ['body', 'text'], msg: 'String should have at least 1 character' }]
    const client = new HttpClient(scripted(fail(422, detail)))
    expect(await rejection(client.capture(''))).toMatchObject({
      kind: 'invalid',
      message: 'String should have at least 1 character',
    })
  })

  it('reports a server error that is not JSON by its status', async () => {
    const client = new HttpClient(scripted(new Response('<h1>Bad Gateway</h1>', { status: 502 })))
    expect(await rejection(client.listItems())).toMatchObject({
      kind: 'server',
      status: 502,
      message: 'The server answered 502',
    })
  })

  it('reports a success it cannot read', async () => {
    const client = new HttpClient(scripted(new Response('not json', { status: 200 })))
    expect((await rejection(client.listItems())).kind).toBe('server')
  })
})

describe('the 401 and 429 lock', () => {
  it('stops sending requests after a 401 until a token is set', async () => {
    const server = scripted(fail(401, 'Invalid API token'), ok([]))
    const client = new HttpClient({ token: 'wrong', ...server })

    expect((await rejection(client.listItems())).kind).toBe('unauthorized')
    expect(client.connection()).toEqual({ status: 'signed_out' })
    expect((await rejection(client.listItems())).message).toBe('Invalid API token')
    expect(server.mock).toHaveBeenCalledTimes(1) // the second call never left the client

    client.setToken('t0ken')
    expect(await client.listItems()).toEqual([])
    expect(client.connection()).toEqual({ status: 'connected' })
  })

  it('stops after a 429 and says when to try again', async () => {
    const server = scripted(fail(429, 'Too many requests', { 'Retry-After': '30' }))
    const client = new HttpClient({ token: 'wrong', ...server })

    expect(await rejection(client.listItems())).toMatchObject({
      kind: 'rate_limited',
      retryAfterSeconds: 30,
    })
    expect(client.connection()).toEqual({ status: 'rate_limited', retry_after_seconds: 30 })
    await rejection(client.health())
    expect(server.mock).toHaveBeenCalledTimes(1)
  })

  it('lifts the 429 lock once Retry-After has passed, without a reload (#112)', async () => {
    let now = 1_000_000
    const server = scripted(fail(429, 'Too many requests', { 'Retry-After': '30' }), ok([]))
    const client = new HttpClient({ token: 'wrong', now: () => now, ...server })

    await rejection(client.listItems())
    now += 29_999
    expect((await rejection(client.listItems())).kind).toBe('rate_limited')
    expect(server.mock).toHaveBeenCalledTimes(1)

    now += 1
    expect(await client.listItems()).toEqual([])
    expect(client.connection()).toEqual({ status: 'connected' })
  })

  it('assumes 30 seconds when a 429 names no Retry-After', async () => {
    let now = 0
    const server = scripted(fail(429, 'Too many requests'), ok([]))
    const client = new HttpClient({ now: () => now, ...server })

    await rejection(client.listItems())
    now = 29_999
    await rejection(client.listItems())
    now = 30_000
    expect(await client.listItems()).toEqual([])
    expect(server.mock).toHaveBeenCalledTimes(2)
  })

  it('does not lock on other errors', async () => {
    const server = scripted(fail(404, 'No item #7'), ok({ id: 7 }))
    const client = new HttpClient(server)

    await rejection(client.getItem(7))
    await expect(client.getItem(7)).resolves.toEqual({ id: 7 })
  })
})

describe('connection state', () => {
  it('goes offline when the server cannot be reached, and back when it answers', async () => {
    const server = scripted(new TypeError('Failed to fetch'), ok([]))
    const client = new HttpClient(server)
    const seen: ConnectionState[] = []
    client.onConnectionChange((s) => seen.push(s))

    const error = await rejection(client.listItems())
    expect(error.kind).toBe('offline')
    expect(error.cause).toBeInstanceOf(TypeError)
    await client.listItems()

    expect(seen).toEqual([{ status: 'offline', queued: 0 }, { status: 'connected' }])
  })
})

describe('endpoints the backend does not have yet', () => {
  it('reject without sending a request', async () => {
    const server = scripted()
    const client = new HttpClient(server)

    expect(await rejection(client.parse())).toMatchObject({
      kind: 'unsupported',
      message: "parse needs backend support that isn't built yet (#94)",
    })
    expect((await rejection(client.listLists())).message).toContain('#88')
    expect(server.mock).not.toHaveBeenCalled()
  })
})

describe('searchNotes', () => {
  it('picks the notes out of a search, newest first', async () => {
    const server = scripted(
      ok([
        { id: 1, kind: 'reminder', created_at: '2026-11-05T09:00:00+05:30' },
        { id: 2, kind: 'note', created_at: '2026-11-01T09:00:00+05:30' },
        { id: 3, kind: 'note', created_at: '2026-11-04T09:00:00+05:30' },
      ]),
    )
    const notes = await new HttpClient(server).searchNotes(' gift ')

    expect(notes.map((n) => n.id)).toEqual([3, 2])
    expect(server.calls[0]?.url).toBe('/items?q=gift')
  })
})
