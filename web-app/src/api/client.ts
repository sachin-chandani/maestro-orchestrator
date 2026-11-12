import { ApiError } from './errors.ts'
import type {
  CaptureResult,
  ConnectionState,
  Device,
  Health,
  Item,
  ItemEdit,
  ListItemsOptions,
  PairRequest,
  PairResult,
  ParsePreview,
  Settings,
  SettingsUpdate,
  SignIn,
  SnoozeDuration,
  TodoList,
} from './types.ts'

/**
 * Everything the screens need from Maestro (#101). Screens depend on this interface only, so moving a
 * screen from MockClient to HttpClient never changes the screen (ADR 0014).
 *
 * Every method rejects with an ApiError. Methods the backend doesn't serve yet reject with kind
 * "unsupported" in HttpClient; SUPPORT below lists which those are.
 */
export interface MaestroClient {
  // Items
  listItems(options?: ListItemsOptions): Promise<Item[]>
  getItem(id: number): Promise<Item>
  /** Capture plain text; the server parses it into a reminder or a note. (`POST /items`: one item.) */
  capture(text: string): Promise<Item>
  /**
   * Capture plain text the way the app does (ADR 0017): every sure reading is saved at once, an
   * unsure one is saved with a gap to answer later, and one undo covers the lot.
   */
  captureSplit(text: string): Promise<CaptureResult>
  /** Done. On a recurring reminder this is one occurrence; the series continues (ADR 0009). */
  done(id: number): Promise<Item>
  /** Snooze. On a recurring reminder this is one occurrence (ADR 0009). */
  snooze(id: number, duration: SnoozeDuration): Promise<Item>
  /** End an item for good; on a recurring reminder, the whole series. */
  stop(id: number): Promise<Item>
  deleteItem(id: number): Promise<void>
  editItem(id: number, changes: ItemEdit): Promise<Item>

  // Notes
  /** Notes matching `query` (all notes when empty), newest first. */
  searchNotes(query: string): Promise<Item[]>

  // Parse preview
  /** What `capture(text)` would create, without saving anything. */
  parse(text: string): Promise<ParsePreview>

  // Lists
  listLists(): Promise<TodoList[]>
  createList(name: string): Promise<TodoList>
  addToList(listId: number, texts: string[]): Promise<TodoList>
  setChecked(listId: number, entryId: number, checked: boolean): Promise<TodoList>
  clearChecked(listId: number): Promise<TodoList>

  // Devices and pairing
  /**
   * Sign this device in with the server's master token (ADR 0018). Works while the client is
   * locked by a 401: that is when it is needed. On success the client uses the device's own token
   * from then on; the screen stores it.
   */
  registerDevice(masterToken: string, deviceName: string): Promise<SignIn>
  pair(request: PairRequest): Promise<PairResult>
  listDevices(): Promise<Device[]>
  revokeDevice(id: number): Promise<void>

  // Settings
  getSettings(): Promise<Settings>
  updateSettings(changes: SettingsUpdate): Promise<Settings>
  /** Send a test message on one channel, like `maestro doctor --send-test`. */
  sendTest(channel: string): Promise<void>

  // Server and connection
  health(): Promise<Health>
  /** Use a new token (or none). Unlocks a client locked by a 401 or 429. */
  setToken(token: string | null): void
  /** The current connection state; kept by the client from the outcome of its own requests. */
  connection(): ConnectionState
  /** Called whenever the connection state changes. Returns a function that unsubscribes. */
  onConnectionChange(listener: (state: ConnectionState) => void): () => void
}

/** How far the real backend supports a client method today. */
export type Support =
  /** Served by an existing endpoint. */
  | { level: 'backend'; endpoint: string }
  /** Served, but some of what the screens show isn't sent yet. */
  | { level: 'partial'; endpoint: string; missing: string; issue: number }
  /** No endpoint yet: both clients reject with ApiError "unsupported" until its issue is built. */
  | { level: 'none'; issue: number }
  /** Kept by the client itself; no endpoint needed. */
  | { level: 'client' }

const ITEM_EXTRAS = 'lead_time_label, escalation_label, streak, held_until, source'

/**
 * Support per method, checked against the backend at maestro/api.py. Typed by the interface's keys,
 * so adding a method without saying how it is supported fails the type check.
 */
export const SUPPORT: Record<keyof MaestroClient, Support> = {
  listItems: {
    level: 'partial',
    endpoint: 'GET /items',
    missing: ITEM_EXTRAS,
    issue: 101,
  },
  getItem: { level: 'backend', endpoint: 'GET /items/{id}' },
  capture: { level: 'backend', endpoint: 'POST /items' },
  captureSplit: { level: 'backend', endpoint: 'POST /capture' },
  done: {
    level: 'partial',
    endpoint: 'POST /items/{id}/done',
    missing: 'which pending deliveries (SMS, call) it cancelled',
    issue: 103,
  },
  snooze: { level: 'backend', endpoint: 'POST /items/{id}/snooze' },
  stop: { level: 'backend', endpoint: 'POST /items/{id}/stop' },
  deleteItem: { level: 'backend', endpoint: 'DELETE /items/{id}' },
  editItem: { level: 'none', issue: 101 },
  searchNotes: {
    level: 'partial',
    endpoint: 'GET /items?q=',
    missing: 'where a note came from (source); notes are filtered from items client-side',
    issue: 105,
  },
  parse: { level: 'none', issue: 94 },
  listLists: { level: 'none', issue: 88 },
  createList: { level: 'none', issue: 88 },
  addToList: { level: 'none', issue: 88 },
  setChecked: { level: 'none', issue: 88 },
  clearChecked: { level: 'none', issue: 88 },
  registerDevice: { level: 'backend', endpoint: 'POST /devices' },
  pair: { level: 'none', issue: 95 },
  listDevices: { level: 'backend', endpoint: 'GET /devices' },
  revokeDevice: { level: 'backend', endpoint: 'DELETE /devices/{id}' },
  getSettings: { level: 'none', issue: 98 },
  updateSettings: { level: 'none', issue: 98 },
  sendTest: { level: 'none', issue: 98 },
  health: { level: 'backend', endpoint: 'GET /health' },
  setToken: { level: 'client' },
  connection: { level: 'client' },
  onConnectionChange: { level: 'client' },
}

/** The rejection for a method the backend has no endpoint for yet, naming the issue that adds it. */
export function unsupportedError(method: keyof MaestroClient): ApiError {
  const support = SUPPORT[method]
  const issue = 'issue' in support ? ` (#${String(support.issue)})` : ''
  return new ApiError('unsupported', `${method} needs backend support that isn't built yet${issue}`)
}
