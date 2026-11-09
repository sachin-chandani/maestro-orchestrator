import { SUPPORT, type MaestroClient } from '../client.ts'
import { ApiError, type ApiErrorKind } from '../errors.ts'
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
import { parseCapture } from './capture.ts'
import {
  ACK_WINDOW_MS,
  complete,
  sendTime,
  snooze as snoozeSeries,
  type QuietHours,
  type Series,
} from './rules.ts'
import { seedItems } from './seed.ts'
import {
  clear,
  defaultStorage,
  load,
  save,
  type KeyValueStorage,
  type MockState,
  type StoredItem,
} from './store.ts'
import { describeRule, shortLabel, toIso } from './time.ts'

type Method = keyof MaestroClient

/** Decides whether a call fails, and how: return an ApiError to fail it, null to let it through. */
export type FailureRule = (method: Method) => ApiError | null

export interface MockClientOptions {
  /** The clock (default: the real one). Tests pass a fixed or stepping one. */
  now?: () => Date
  /** The server timezone the mock pretends to have (default: the browser's). */
  timeZone?: string
  /** Quiet hours, as MAESTRO_QUIET_HOURS (default 22:00–07:00, as in the design); null for none. */
  quietHours?: QuietHours | null
  /** Delay before each call settles, in ms (default 0). */
  latencyMs?: number
  /** Failures to inject (default none). */
  failure?: FailureRule | null
  /** Where state survives a reload (default localStorage); null keeps it in memory only. */
  storage?: KeyValueStorage | null
}

/**
 * A rule failing calls with `kind`. `methods` limits it to some methods; `times` to the next N calls
 * it matches (default: every one).
 */
export function failWith(
  kind: ApiErrorKind,
  options: {
    methods?: Method[]
    times?: number
    message?: string
    retryAfterSeconds?: number
  } = {},
): FailureRule {
  let left = options.times ?? Infinity
  return (method) => {
    if (left <= 0 || (options.methods && !options.methods.includes(method))) return null
    left -= 1
    return new ApiError(kind, options.message ?? `Injected ${kind} failure in ${method}`, {
      status: ERROR_STATUS[kind] ?? null,
      retryAfterSeconds: options.retryAfterSeconds ?? null,
    })
  }
}

/** The HTTP status the backend answers with for each kind, where there is one. */
const ERROR_STATUS: Partial<Record<ApiErrorKind, number>> = {
  unauthorized: 401,
  rate_limited: 429,
  not_found: 404,
  invalid: 422,
  expired: 410,
  server: 500,
}

// maestro.parser.DURATION_RE
const DURATION = /^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$/i
const UNIT_MS = { m: 60_000, h: 3_600_000, d: 86_400_000 } as const

const DEFAULT_QUIET: QuietHours = { start: '22:00', end: '07:00' }

/**
 * MaestroClient on in-memory sample data (#101), so screens can be built and demoed with no
 * backend. It follows the backend's rules where the screens show them: done and snooze on a
 * recurring reminder act on one occurrence (ADR 0009), and quiet hours hold reminders that aren't
 * urgent. Reminders "go out" when their time passes, as the scheduler would send them, so they
 * become awaiting an answer. Endpoints the backend doesn't have yet reject with "unsupported".
 */
export class MockClient implements MaestroClient {
  private readonly now: () => Date
  private readonly tz: string
  private readonly quiet: QuietHours | null
  private readonly storage: KeyValueStorage | null
  private latencyMs: number
  private failure: FailureRule | null
  private state: MockState
  private connectionState: ConnectionState = { status: 'connected' }
  private readonly listeners = new Set<(state: ConnectionState) => void>()

  constructor(options: MockClientOptions = {}) {
    this.now = options.now ?? (() => new Date())
    this.tz = options.timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone
    this.quiet = options.quietHours === undefined ? DEFAULT_QUIET : options.quietHours
    this.storage = options.storage === undefined ? defaultStorage() : options.storage
    this.latencyMs = options.latencyMs ?? 0
    this.failure = options.failure ?? null
    this.state = load(this.storage) ?? this.seed()
    this.persist()
  }

  // -------------------------------------------------------------------------------------------------
  // Controls for tests and the dev panel; not part of MaestroClient.

  /** Throw away every change and start again from the sample data, placed around the clock's now. */
  reset(): void {
    clear(this.storage)
    this.state = this.seed()
    this.persist()
  }

  setLatency(ms: number): void {
    this.latencyMs = ms
  }

  setFailure(rule: FailureRule | null): void {
    this.failure = rule
  }

  // -------------------------------------------------------------------------------------------------
  // Items

  listItems(options: ListItemsOptions = {}): Promise<Item[]> {
    return this.call('listItems', () => {
      let items = this.state.items
      const q = options.q?.toLowerCase()
      if (options.unacknowledged) {
        items = items.filter((i) => this.awaiting(i))
        if (q) items = items.filter((i) => i.text.toLowerCase().includes(q))
      } else if (q) {
        // GET /items?q= searches everything, done items too, newest first.
        items = items.filter((i) => i.text.toLowerCase().includes(q)).sort(byNewest)
      } else {
        if (!options.include_done) items = items.filter((i) => i.status !== 'done')
        items = [...items].sort(byNextOut)
      }
      return items.map((i) => this.view(i))
    })
  }

  getItem(id: number): Promise<Item> {
    return this.call('getItem', () => this.view(this.find(id)))
  }

  capture(text: string): Promise<Item> {
    return this.call('capture', () => {
      if (!text.trim() || text.length > 2000) {
        throw new ApiError('invalid', 'A capture needs between 1 and 2000 characters', {
          status: 422,
        })
      }
      const now = this.now()
      const c = parseCapture(text, now, this.tz)
      const item: StoredItem = {
        id: this.state.nextId,
        text: c.text,
        kind: c.kind,
        status: 'pending',
        due_at: c.dueAt ? this.iso(c.dueAt) : null,
        snoozed_until: null,
        created_at: this.iso(now),
        recurrence: c.recurrence,
        channels: c.channels,
        urgent: c.urgent,
        source_text: c.sourceText,
        source: 'web',
        delivered: null,
      }
      this.state.items.push(item)
      this.state.nextId += 1
      return this.view(item)
    })
  }

  done(id: number): Promise<Item> {
    return this.call('done', () => {
      const item = this.find(id)
      const series = this.series(item)
      if (series) {
        this.move(item, complete(series, this.target(item), this.tz))
      } else {
        item.status = 'done'
      }
      this.acknowledge(item)
      return this.view(item)
    })
  }

  snooze(id: number, duration: SnoozeDuration): Promise<Item> {
    return this.call('snooze', () => {
      const item = this.find(id)
      const m = DURATION.exec(duration)
      if (!m) {
        throw new ApiError('invalid', `Can't read duration '${duration}'; try 10m, 2h or 1d`, {
          status: 422,
        })
      }
      const unit = (m[2] ?? 'm').charAt(0).toLowerCase() as keyof typeof UNIT_MS
      const now = this.now()
      const until = new Date(now.getTime() - (now.getTime() % 1000) + Number(m[1]) * UNIT_MS[unit])
      const series = this.series(item)
      if (series) {
        this.move(item, snoozeSeries(series, until, this.target(item), this.tz))
      } else {
        // A one-off (or a note) moves to the snooze time and is a pending reminder again.
        item.due_at = this.iso(until)
        item.snoozed_until = null
        item.status = 'pending'
        item.kind = 'reminder'
      }
      this.acknowledge(item)
      return this.view(item)
    })
  }

  stop(id: number): Promise<Item> {
    return this.call('stop', () => {
      const item = this.find(id)
      item.status = 'done'
      item.snoozed_until = null
      this.acknowledge(item)
      return this.view(item)
    })
  }

  deleteItem(id: number): Promise<void> {
    return this.call('deleteItem', () => {
      const item = this.find(id)
      this.state.items = this.state.items.filter((i) => i !== item)
    })
  }

  editItem(): Promise<Item> {
    return this.unsupported('editItem')
  }

  // -------------------------------------------------------------------------------------------------
  // Notes

  searchNotes(query: string): Promise<Item[]> {
    return this.call('searchNotes', () => {
      const q = query.trim().toLowerCase()
      return this.state.items
        .filter((i) => i.kind === 'note' && (!q || i.text.toLowerCase().includes(q)))
        .sort(byNewest)
        .map((i) => this.view(i))
    })
  }

  // -------------------------------------------------------------------------------------------------
  // Endpoints the backend doesn't have yet (SUPPORT in client.ts): unsupported here too.

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
    return this.call('health', () => ({
      status: 'ok',
      version: 'mock',
      schema: 0,
      timezone: this.tz,
      backend: 'mock',
    }))
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

  /**
   * Runs one call the way a request would: wait out the latency, fail if a failure is injected
   * (changing nothing), otherwise bring deliveries up to date, run it and save.
   */
  private async call<T>(method: Method, run: () => T): Promise<T> {
    if (this.latencyMs > 0) await new Promise((resolve) => setTimeout(resolve, this.latencyMs))
    const injected = this.failure?.(method) ?? null
    if (injected) {
      this.setConnection(injected)
      throw injected
    }
    this.deliverDue()
    const before = JSON.stringify(this.state)
    try {
      // Copy out, so a caller changing what it got can't change the mock's state.
      return structuredClone(run())
    } catch (error) {
      this.state = JSON.parse(before) as MockState // a call that fails changes nothing
      throw error
    } finally {
      this.setConnection(null)
      this.persist()
    }
  }

  private unsupported<T>(method: Method): Promise<T> {
    const support = SUPPORT[method]
    const issue = 'issue' in support ? ` (#${String(support.issue)})` : ''
    return this.call(method, () => {
      throw new ApiError(
        'unsupported',
        `${method} needs backend support that isn't built yet${issue}`,
      )
    })
  }

  private setConnection(error: ApiError | null): void {
    let next: ConnectionState = { status: 'connected' }
    if (error?.kind === 'unauthorized') next = { status: 'signed_out' }
    else if (error?.kind === 'rate_limited') {
      next = { status: 'rate_limited', retry_after_seconds: error.retryAfterSeconds }
    } else if (error?.kind === 'offline') next = { status: 'offline', queued: 0 }
    else if (error) return // other failures say nothing about the connection
    if (JSON.stringify(next) === JSON.stringify(this.connectionState)) return
    this.connectionState = next
    for (const listener of this.listeners) listener(next)
  }

  /**
   * What the scheduler would have sent by now: each pending reminder whose time (or snooze) has
   * passed goes out once, at the end of quiet hours if it fell in them and isn't urgent. A recurring
   * one moves to its next occurrence; several missed occurrences make one catch-up notice.
   */
  private deliverDue(): void {
    const now = this.now()
    for (const item of this.state.items) {
      for (let guard = 0; guard < 10_000; guard++) {
        if (item.status !== 'pending' || item.kind !== 'reminder' || !item.due_at) break
        const occurrence = new Date(item.snoozed_until ?? item.due_at)
        const sentAt = sendTime(item, occurrence, this.quiet, this.tz)
        if (sentAt > now) break
        item.delivered = {
          occurrence: this.iso(occurrence),
          sent_at: this.iso(sentAt),
          acknowledged: false,
        }
        const series = this.series(item)
        if (!series) {
          item.status = 'fired'
        } else if (item.snoozed_until) {
          item.snoozed_until = null // the snoozed re-send is done; the series keeps its time
        } else {
          item.due_at = this.iso(complete(series, occurrence, this.tz).dueAt)
        }
      }
    }
  }

  private find(id: number): StoredItem {
    const item = this.state.items.find((i) => i.id === id)
    if (!item) throw new ApiError('not_found', `No item #${String(id)}`, { status: 404 })
    return item
  }

  private series(item: StoredItem): Series | null {
    if (!item.recurrence || !item.due_at || item.status === 'done') return null
    return {
      rule: item.recurrence,
      nextDue: new Date(item.due_at),
      snoozedUntil: item.snoozed_until ? new Date(item.snoozed_until) : null,
    }
  }

  /** The delivered occurrence a done or snooze is about, or null for the upcoming one (ADR 0009). */
  private target(item: StoredItem): Date | null {
    const d = item.delivered
    if (!d || d.acknowledged) return null
    if (this.now().getTime() - new Date(d.sent_at).getTime() > ACK_WINDOW_MS) return null
    return new Date(d.occurrence)
  }

  private acknowledge(item: StoredItem): void {
    if (item.delivered) item.delivered.acknowledged = true
  }

  private move(item: StoredItem, move: { dueAt: Date; snoozedUntil: Date | null }): void {
    item.due_at = this.iso(move.dueAt)
    item.snoozed_until = move.snoozedUntil ? this.iso(move.snoozedUntil) : null
    item.status = 'pending'
    item.kind = 'reminder'
  }

  private awaiting(item: StoredItem): boolean {
    return item.status !== 'done' && !!item.delivered && !item.delivered.acknowledged
  }

  /** The item as the API returns it, with the fields the backend derives filled in. */
  private view(item: StoredItem): Item {
    const view: Item & { delivered?: unknown } = {
      ...structuredClone(item),
      recurrence_label: item.recurrence ? describeRule(item.recurrence) : null,
      when: this.when(item),
      awaiting_acknowledgement: this.awaiting(item),
    }
    delete view.delivered
    const next = item.snoozed_until ?? item.due_at
    if (item.status === 'pending' && item.kind === 'reminder' && next) {
      const due = new Date(next)
      const sentAt = sendTime(item, due, this.quiet, this.tz)
      if (sentAt > due) view.held_until = this.iso(sentAt)
    }
    return view
  }

  /** commands.when(): "Thu 05 Nov 17:45", "… (snoozed), then …", ", every Thursday", or "note". */
  private when(item: StoredItem): string {
    if (!item.due_at) return item.kind === 'task' ? 'task' : 'note'  // matches commands.when (#114)
    let label = shortLabel(new Date(item.due_at), this.tz)
    if (item.snoozed_until) {
      label = `${shortLabel(new Date(item.snoozed_until), this.tz)} (snoozed), then ${label}`
    }
    return item.recurrence ? `${label}, ${describeRule(item.recurrence)}` : label
  }

  private iso(d: Date): string {
    return toIso(d, this.tz)
  }

  private seed(): MockState {
    const items = seedItems(this.now(), this.tz)
    return { items, nextId: Math.max(0, ...items.map((i) => i.id)) + 1 }
  }

  private persist(): void {
    save(this.storage, this.state)
  }
}

const time = (i: StoredItem) => new Date(i.snoozed_until ?? i.due_at ?? 0).getTime()

/** Store.list(): reminders by when they next go out, then notes by creation; id breaks ties. */
function byNextOut(a: StoredItem, b: StoredItem): number {
  const aNote = a.due_at === null ? 1 : 0
  const bNote = b.due_at === null ? 1 : 0
  if (aNote !== bNote) return aNote - bNote
  return (
    time(a) - time(b) ||
    new Date(a.created_at).getTime() - new Date(b.created_at).getTime() ||
    a.id - b.id
  )
}

/** Store.search(): newest first. */
function byNewest(a: StoredItem, b: StoredItem): number {
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime() || b.id - a.id
}
