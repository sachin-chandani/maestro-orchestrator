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

  const route = async (method: string, url: URL, body: unknown): Promise<Response> => {
    const path = url.pathname
    const id = Number(/^\/items\/(\d+)/.exec(path)?.[1])
    const send = async (status: number, call: () => Promise<unknown>) => json(status, await call())

    if (method === 'GET' && path === '/health') return send(200, () => backend.health())
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

    if (options.token && url.pathname !== '/health') {
      const auth = headers.get('Authorization')
      if (auth === null) return json(401, { detail: 'Missing API token' })
      if (auth !== `Bearer ${options.token}`) return json(401, { detail: 'Invalid API token' })
    }
    try {
      return await route(method, url, body)
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
