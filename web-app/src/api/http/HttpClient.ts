import { unsupportedError, type MaestroClient } from '../client.ts'
import { Connection } from '../connection.ts'
import { ApiError } from '../errors.ts'
import type {
  CaptureResult,
  ConnectionState,
  Device,
  Health,
  Item,
  ListItemsOptions,
  PairResult,
  ParsePreview,
  Settings,
  SignIn,
  SnoozeDuration,
  TodoList,
} from '../types.ts'

/** POST /capture answers CaptureOut, or ProposalOut (202) under MAESTRO_CONFIRM=unsure. */
type CaptureAnswer =
  | { added: Item[]; needs_look: Item[]; undo: CaptureResult['undo'] }
  | { id: number; lines: string[]; status: string }

/** POST /devices: DeviceOut plus the token. */
type DeviceCreated = Device & { token: string }

type Method = keyof MaestroClient

export interface HttpClientOptions {
  /** Where `maestro serve` is: "" (default) for the server this app was loaded from. */
  baseUrl?: string
  /** The API token (MAESTRO_API_TOKEN), or null when none is set. */
  token?: string | null
  /** The fetch to use (default the browser's); tests pass their own so nothing touches the network. */
  fetch?: typeof fetch
  /** The clock, in ms (default Date.now); decides when a 429 lock lifts. Tests pass their own. */
  now?: () => number
}

/**
 * MaestroClient against a real `maestro serve` (#101), for the endpoints in maestro/api.py.
 *
 * After a 401 or 429 the client locks (Connection): every call fails at once without a request, so
 * polling can't keep sending a missing or wrong token and extend a lockout. Setting a token unlocks
 * it; a 429 lock also lifts by itself after its Retry-After (#112). Methods the backend has no
 * endpoint for reject with "unsupported" (SUPPORT in client.ts) without a request.
 */
export class HttpClient implements MaestroClient {
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private token: string | null
  private readonly conn: Connection

  constructor(options: HttpClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '')
    this.token = options.token ?? null
    // Bound, because calling the browser's fetch as a method of another object throws.
    this.fetchImpl = options.fetch ?? fetch.bind(globalThis)
    this.conn = new Connection(options.now)
  }

  setToken(token: string | null): void {
    this.token = token
    this.conn.unlock()
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

  async captureSplit(text: string): Promise<CaptureResult> {
    const answer = await this.request<CaptureAnswer>('POST', '/capture', { text })
    if ('lines' in answer) {
      return {
        added: [],
        needs_look: [],
        undo: null,
        proposal: { id: answer.id, lines: answer.lines },
      }
    }
    return { ...answer, proposal: null }
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
  async registerDevice(masterToken: string, deviceName: string): Promise<SignIn> {
    // Past the lock and with the master token, once: a signed-out client is exactly the one that
    // needs this. On success the device's own token takes over and the lock is lifted.
    const { token, ...device } = await this.send<DeviceCreated>(
      'POST',
      '/devices',
      { name: deviceName },
      { token: masterToken, pastLock: true },
    )
    this.setToken(token)
    return { token, device }
  }

  pair(): Promise<PairResult> {
    return this.unsupported('pair')
  }

  listDevices(): Promise<Device[]> {
    return this.request('GET', '/devices')
  }

  revokeDevice(id: number): Promise<void> {
    return this.request('DELETE', `/devices/${itemId(id)}`)
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
    return this.conn.current()
  }

  onConnectionChange(listener: (state: ConnectionState) => void): () => void {
    return this.conn.subscribe(listener)
  }

  // -------------------------------------------------------------------------------------------------
  // Internals

  private request<T>(method: string, path: string, body?: unknown): Promise<T> {
    return this.send(method, path, body, {})
  }

  private async send<T>(
    method: string,
    path: string,
    body: unknown,
    options: { token?: string; pastLock?: boolean },
  ): Promise<T> {
    if (!options.pastLock) {
      const lock = this.conn.locked()
      if (lock) throw lock
    }

    const headers: Record<string, string> = {
      Accept: 'application/json',
      // Done, snooze and stop are then recorded as coming from the web UI (api.py client_name).
      'X-Maestro-Client': 'web',
    }
    const token = options.token ?? this.token
    if (token) headers.Authorization = `Bearer ${token}`
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
      this.conn.failed(error)
      throw error
    }

    if (!response.ok) {
      const error = await errorFrom(response)
      this.conn.failed(error)
      throw error
    }
    this.conn.succeeded()
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
    return Promise.reject(unsupportedError(method))
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
