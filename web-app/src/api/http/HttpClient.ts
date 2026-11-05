import { SUPPORT, type MaestroClient } from '../client.ts'
import { ApiError } from '../errors.ts'
import type {
  ConnectionState,
  Device,
  Health,
  Item,
  ListItemsOptions,
  PairResult,
  ParsePreview,
  Settings,
  SnoozeDuration,
  TodoList,
} from '../types.ts'

type Method = keyof MaestroClient

export interface HttpClientOptions {
  /** Where `maestro serve` is: "" (default) for the server this app was loaded from. */
  baseUrl?: string
  /** The API token (MAESTRO_API_TOKEN), or null when none is set. */
  token?: string | null
  /** The fetch to use (default the browser's); tests pass their own so nothing touches the network. */
  fetch?: typeof fetch
}

/**
 * MaestroClient against a real `maestro serve` (#101), for the endpoints in maestro/api.py.
 *
 * After a 401 or 429 the client locks: every call fails at once without a request, as the current
 * page does, so polling can't keep sending a missing or wrong token and extend a lockout. Setting a
 * token unlocks it. Methods the backend has no endpoint for reject with "unsupported" (SUPPORT in
 * client.ts) without a request.
 */
export class HttpClient implements MaestroClient {
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private token: string | null
  private lock: ApiError | null = null
  private connectionState: ConnectionState = { status: 'connected' }
  private readonly listeners = new Set<(state: ConnectionState) => void>()

  constructor(options: HttpClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '')
    this.token = options.token ?? null
    // Bound, because calling the browser's fetch as a method of another object throws.
    this.fetchImpl = options.fetch ?? fetch.bind(globalThis)
  }

  /** Use a new token (or none). Unlocks a client locked by a 401 or 429. */
  setToken(token: string | null): void {
    this.token = token
    this.lock = null
  }

  // -------------------------------------------------------------------------------------------------
  // Items

  listItems(options: ListItemsOptions = {}): Promise<Item[]> {
    const query = new URLSearchParams()
    if (options.include_done) query.set('include_done', 'true')
    if (options.q) query.set('q', options.q)
    if (options.unacknowledged) query.set('unacknowledged', 'true')
    const qs = query.toString()
    return this.request('GET', `/items${qs ? `?${qs}` : ''}`)
  }

  getItem(id: number): Promise<Item> {
    return this.request('GET', `/items/${itemId(id)}`)
  }

  capture(text: string): Promise<Item> {
    return this.request('POST', '/items', { text })
  }

  done(id: number): Promise<Item> {
    return this.request('POST', `/items/${itemId(id)}/done`)
  }

  snooze(id: number, duration: SnoozeDuration): Promise<Item> {
    return this.request('POST', `/items/${itemId(id)}/snooze`, { duration })
  }

  stop(id: number): Promise<Item> {
    return this.request('POST', `/items/${itemId(id)}/stop`)
  }

  deleteItem(id: number): Promise<void> {
    return this.request('DELETE', `/items/${itemId(id)}`)
  }

  editItem(): Promise<Item> {
    return this.unsupported('editItem')
  }

  // -------------------------------------------------------------------------------------------------
  // Notes: GET /items?q= has no notes-only filter yet, so notes are picked out here (SUPPORT).

  async searchNotes(query: string): Promise<Item[]> {
    const items = await this.listItems(query.trim() ? { q: query.trim() } : {})
    return items
      .filter((i) => i.kind === 'note')
      .sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime() || b.id - a.id,
      )
  }

  // -------------------------------------------------------------------------------------------------
  // Endpoints the backend doesn't have yet

  parse(): Promise<ParsePreview> {
    return this.unsupported('parse')
  }
  listLists(): Promise<TodoList[]> {
    return this.unsupported('listLists')
  }
  createList(): Promise<TodoList> {
    return this.unsupported('createList')
  }
  addToList(): Promise<TodoList> {
    return this.unsupported('addToList')
  }
  setChecked(): Promise<TodoList> {
    return this.unsupported('setChecked')
  }
  clearChecked(): Promise<TodoList> {
    return this.unsupported('clearChecked')
  }
  pair(): Promise<PairResult> {
    return this.unsupported('pair')
  }
  listDevices(): Promise<Device[]> {
    return this.unsupported('listDevices')
  }
  revokeDevice(): Promise<void> {
    return this.unsupported('revokeDevice')
  }
  getSettings(): Promise<Settings> {
    return this.unsupported('getSettings')
  }
  updateSettings(): Promise<Settings> {
    return this.unsupported('updateSettings')
  }
  sendTest(): Promise<void> {
    return this.unsupported('sendTest')
  }

  // -------------------------------------------------------------------------------------------------
  // Server and connection

  health(): Promise<Health> {
    return this.request('GET', '/health')
  }

  connection(): ConnectionState {
    return this.connectionState
  }

  onConnectionChange(listener: (state: ConnectionState) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  // -------------------------------------------------------------------------------------------------
  // Internals

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    if (this.lock) throw this.lock

    const headers: Record<string, string> = {
      Accept: 'application/json',
      // Done, snooze and stop are then recorded as coming from the web UI (api.py client_name).
      'X-Maestro-Client': 'web',
    }
    if (this.token) headers.Authorization = `Bearer ${this.token}`
    if (body !== undefined) headers['Content-Type'] = 'application/json'

    let response: Response
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch (cause) {
      const error = new ApiError('offline', "Can't reach the Maestro server", { cause })
      this.setConnection(error)
      throw error
    }

    if (!response.ok) {
      const error = await errorFrom(response)
      if (error.kind === 'unauthorized' || error.kind === 'rate_limited') this.lock = error
      this.setConnection(error)
      throw error
    }
    this.setConnection(null)
    if (response.status === 204) return undefined as T
    try {
      return (await response.json()) as T
    } catch (cause) {
      throw new ApiError('server', 'The server sent a response that could not be read', {
        status: response.status,
        cause,
      })
    }
  }

  private unsupported<T>(method: Method): Promise<T> {
    const support = SUPPORT[method]
    const issue = 'issue' in support ? ` (#${String(support.issue)})` : ''
    return Promise.reject(
      new ApiError('unsupported', `${method} needs backend support that isn't built yet${issue}`),
    )
  }

  private setConnection(error: ApiError | null): void {
    let next: ConnectionState = { status: 'connected' }
    if (error?.kind === 'unauthorized') next = { status: 'signed_out' }
    else if (error?.kind === 'rate_limited') {
      next = { status: 'rate_limited', retry_after_seconds: error.retryAfterSeconds }
    } else if (error?.kind === 'offline') next = { status: 'offline', queued: 0 }
    else if (error) return // the server answered: that says nothing new about the connection
    if (JSON.stringify(next) === JSON.stringify(this.connectionState)) return
    this.connectionState = next
    for (const listener of this.listeners) listener(next)
  }
}

const itemId = (id: number) => encodeURIComponent(String(id))

const KIND_BY_STATUS: Record<number, ApiError['kind']> = {
  401: 'unauthorized',
  404: 'not_found',
  410: 'expired',
  422: 'invalid',
  429: 'rate_limited',
}

/** The ApiError for an error response, with FastAPI's `detail` as the message where it has one. */
async function errorFrom(response: Response): Promise<ApiError> {
  let detail: unknown = null
  try {
    detail = ((await response.json()) as { detail?: unknown }).detail ?? null
  } catch {
    // not JSON (a proxy's error page, say): fall back to the status
  }
  let message = `The server answered ${String(response.status)}`
  if (typeof detail === 'string') message = detail
  else if (Array.isArray(detail)) {
    // Request validation: [{ loc: ["body", "text"], msg: "String should have at least 1 character" }]
    const msgs = detail.map((d: { msg?: unknown }) => d.msg).filter((m) => typeof m === 'string')
    if (msgs.length) message = msgs.join('; ')
  }
  const retryAfter = Number(response.headers.get('Retry-After'))
  return new ApiError(KIND_BY_STATUS[response.status] ?? 'server', message, {
    status: response.status,
    retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
  })
}
