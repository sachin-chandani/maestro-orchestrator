// An in-process stand-in for `maestro serve`, for tests: a fetch that answers the routes in
// maestro/api.py the way FastAPI does (status codes, {"detail": …} bodies, bearer token), backed by a
// MockClient. HttpClient can then be tested end to end with no network and no database.

import type { MaestroClient } from '../client.ts'
import { ApiError } from '../errors.ts'

/** The URL a fetch was called with, whichever form it came in. */
const urlOf = (input: RequestInfo | URL): string =>
  typeof input === 'string' ? input : input instanceof URL ? input.href : input.url

const STATUS: Partial<Record<ApiError['kind'], number>> = {
  unauthorized: 401,
  not_found: 404,
  invalid: 422,
  rate_limited: 429,
}

/** Device tokens the fake server has handed out (POST /devices), accepted like the master token. */

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })

export interface FakeServer {
  fetch: typeof fetch
  /** Every request received, for assertions. */
  requests: { method: string; url: string; headers: Headers; body: unknown }[]
}

export function fakeServer(backend: MaestroClient, options: { token?: string } = {}): FakeServer {
  const requests: FakeServer['requests'] = []
  const deviceTokens = new Set<string>()

  const route = async (
    method: string,
    url: URL,
    body: unknown,
    master: boolean,
  ): Promise<Response> => {
    const path = url.pathname
    const id = Number(/^\/(?:items|devices|needs-look)\/(\d+)/.exec(path)?.[1])
    const send = async (status: number, call: () => Promise<unknown>) => json(status, await call())

    if (method === 'GET' && path === '/health') return send(200, () => backend.health())
    if (method === 'GET' && path === '/feed') {
      const days = url.searchParams.get('days')
      return send(200, () => backend.feed(days === null ? undefined : Number(days)))
    }
    if (/^\/needs-look\/\d+$/.test(path)) {
      if (method === 'POST') {
        const phrase = (body as { phrase?: unknown } | null)?.phrase
        if (typeof phrase !== 'string' || phrase.length < 1) {
          return json(422, {
            detail: [{ loc: ['body', 'phrase'], msg: 'String should have at least 1 character' }],
          })
        }
        return send(200, () => backend.fillGap(id, phrase))
      }
      if (method === 'DELETE') {
        await backend.dismissGap(id)
        return json(204, null)
      }
    }
    if (path === '/devices' || /^\/devices\/\d+$/.test(path)) {
      // Only the master token manages devices (ADR 0018); a device may list them.
      if (options.token && !master && !(method === 'GET' && path === '/devices')) {
        return json(403, { detail: "Only the server's API token can manage devices" })
      }
      if (method === 'POST' && path === '/devices') {
        const name = (body as { name?: unknown } | null)?.name
        if (typeof name !== 'string' || name.length < 1 || name.length > 80) {
          return json(422, {
            detail: [{ loc: ['body', 'name'], msg: 'String should have at most 80 characters' }],
          })
        }
        const signedIn = await backend.registerDevice(options.token ?? '', name)
        deviceTokens.add(signedIn.token)
        return json(201, { ...signedIn.device, token: signedIn.token })
      }
      if (method === 'GET' && path === '/devices') return send(200, () => backend.listDevices())
      if (method === 'DELETE') {
        await backend.revokeDevice(id)
        return json(204, null)
      }
    }
    if (method === 'POST' && path === '/capture') {
      const text = (body as { text?: unknown } | null)?.text
      if (typeof text !== 'string' || text.length < 1) {
        return json(422, {
          detail: [{ loc: ['body', 'text'], msg: 'String should have at least 1 character' }],
        })
      }
      const result = await backend.captureSplit(text)
      if (result.proposal) {
        return json(202, {
          id: result.proposal.id,
          lines: result.proposal.lines,
          status: 'pending',
        })
      }
      return json(201, { added: result.added, needs_look: result.needs_look, undo: result.undo })
    }
    if (method === 'GET' && path === '/items') {
      const p = url.searchParams
      return send(200, () =>
        backend.listItems({
          include_done: p.get('include_done') === 'true',
          q: p.get('q') ?? undefined,
          unacknowledged: p.get('unacknowledged') === 'true',
        }),
      )
    }
    if (method === 'POST' && path === '/items') {
      const text = (body as { text?: unknown } | null)?.text
      if (typeof text !== 'string' || text.length < 1) {
        return json(422, {
          detail: [{ loc: ['body', 'text'], msg: 'String should have at least 1 character' }],
        })
      }
      return send(201, () => backend.capture(text))
    }
    if (method === 'GET' && /^\/items\/\d+$/.test(path)) return send(200, () => backend.getItem(id))
    if (method === 'POST' && path.endsWith('/done')) return send(200, () => backend.done(id))
    if (method === 'POST' && path.endsWith('/stop')) return send(200, () => backend.stop(id))
    if (method === 'POST' && path.endsWith('/snooze')) {
      const duration = (body as { duration?: unknown } | null)?.duration
      return send(200, () => backend.snooze(id, typeof duration === 'string' ? duration : ''))
    }
    if (method === 'DELETE' && /^\/items\/\d+$/.test(path)) {
      await backend.deleteItem(id)
      return json(204, null)
    }
    return json(404, { detail: 'Not Found' })
  }

  const fetchImpl = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(urlOf(input), 'http://maestro.test')
    const headers = new Headers(init.headers)
    const method = init.method ?? 'GET'
    const body: unknown = typeof init.body === 'string' ? JSON.parse(init.body) : null
    requests.push({ method, url: url.pathname + url.search, headers, body })

    let master = true
    if (options.token && url.pathname !== '/health') {
      const auth = headers.get('Authorization')
      if (auth === null) return json(401, { detail: 'Missing API token' })
      master = auth === `Bearer ${options.token}`
      const presented = auth.startsWith('Bearer ') ? auth.slice('Bearer '.length) : ''
      if (!master && !deviceTokens.has(presented)) return json(401, { detail: 'Invalid API token' })
    }
    try {
      return await route(method, url, body, master)
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      // A 429 says when to try again, as maestro/api.py does.
      const extra: Record<string, string> =
        error.retryAfterSeconds !== null ? { 'Retry-After': String(error.retryAfterSeconds) } : {}
      return json(STATUS[error.kind] ?? 500, { detail: error.message }, extra)
    }
  }

  return { fetch: fetchImpl, requests }
}
