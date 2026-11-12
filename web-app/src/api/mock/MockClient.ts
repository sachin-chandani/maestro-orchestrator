import { unsupportedError, type MaestroClient } from '../client.ts'
import { Connection } from '../connection.ts'
import { ApiError, type ApiErrorKind } from '../errors.ts'
import type {
  CaptureResult,
  ConnectionState,
  Device,
  Feed,
  GapSuggestion,
  GapSuggestions,
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
import {
  addDays,
  describeRule,
  fromWallTime,
  nextOccurrence,
  shortLabel,
  toIso,
  wallTime,
} from './time.ts'

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
  /**
   * The master token the pretend server wants for registerDevice (default: any is accepted), so
   * the sign-in screen's wrong-token path can be tried on sample data.
   */
  masterToken?: string | null
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

// maestro.confidence: words that name a time the rules can't read, so a task saying them is
// asked for one rather than filed untimed (ADR 0017).
const VAGUE_TIME_RE =
  /\b(?:some\s?time|soon|later|eventually|asap|at some point|(?:next|this) (?:week|month|year)|after (?:work|lunch|dinner))\b/i
// maestro.gaps.WANTED
const GAP_WANTED: Record<string, string> = {
  when: 'a time',
  what: 'a description',
  channel: 'a channel',
}

// maestro.suggest.said: the vague words the question is about.
const SAID_RE =
  /\b(?:some\s?time\s+(?:next|this)\s+(?:week|month)|(?:next|this)\s+(?:week|month)|at some point|some\s?time|soon|later|eventually|asap|tonight|morning|afternoon|evening|weekend|before|until|from)\b/i

function saidOf(text: string): string {
  return SAID_RE.exec(text)?.[0] ?? text
}

/** An ISO 8601 answer with an offset, or null: "2026-11-09T09:00:00+05:30". */
function isoWithOffset(phrase: string): Date | null {
  const trimmed = phrase.trim()
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})$/.test(trimmed)) return null
  const parsed = new Date(trimmed)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/**
 * MaestroClient on in-memory sample data (#101), so screens can be built and demoed with no
 * backend. It follows the backend's rules where the screens show them: done and snooze on a
 * recurring reminder act on one occurrence (ADR 0009), and quiet hours hold reminders that aren't
 * urgent. Reminders "go out" when their time passes, as the scheduler would send them, so they
 * become awaiting an answer. Endpoints the backend doesn't have yet reject with "unsupported".
 * After an injected 401 or 429 it locks exactly as HttpClient does (Connection), so a screen can't
 * behave differently on sample data than against a server (#112).
 */
export class MockClient implements MaestroClient {
  private readonly now: () => Date
  private readonly tz: string
  private readonly quiet: QuietHours | null
  private readonly storage: KeyValueStorage | null
  private latencyMs: number
  private failure: FailureRule | null
  private state: MockState
  private readonly conn: Connection
  /** The token the app has set; the mock checks nothing against it, but keeps it as a client would. */
  token: string | null = null
  private readonly masterToken: string | null
  /** Devices signed in during this session (not persisted: the sample data is about reminders). */
  private readonly signedIn: (Device & { token: string })[] = []
  private undoIds = 0
  /** What each capture added and until when it can be taken back (not persisted, like a session). */
  private readonly undoable = new Map<number, { itemIds: number[]; until: Date }>()
  /** Captures already taken back: a second undo answers yes again, as the server does (#117). */
  private readonly undone = new Set<number>()

  constructor(options: MockClientOptions = {}) {
    this.now = options.now ?? (() => new Date())
    this.conn = new Connection(() => this.now().getTime())
    this.tz = options.timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone
    this.quiet = options.quietHours === undefined ? DEFAULT_QUIET : options.quietHours
    this.storage = options.storage === undefined ? defaultStorage() : options.storage
    this.latencyMs = options.latencyMs ?? 0
    this.failure = options.failure ?? null
    this.masterToken = options.masterToken ?? null
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

  /** Change what fails. Switching failures off (null) also lifts a 401 or 429 lock they caused. */
  setFailure(rule: FailureRule | null): void {
    this.failure = rule
    if (rule === null) this.conn.unlock()
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

  async captureSplit(text: string): Promise<CaptureResult> {
    // A stand-in for maestro.gaps: something to do with no time, in words that name one the sample
    // parser can't read ("sometime next week", "soon"), is saved as needing a time (ADR 0017).
    // The sample parser doesn't split sentences; that is the server's job (#130).
    const item = await this.capture(text)
    const vague = item.kind === 'task' && VAGUE_TIME_RE.test(item.text)
    if (vague) {
      const stored = this.state.items.find((i) => i.id === item.id)
      if (stored) stored.gap = 'when'
      this.persist()
      item.gap = 'when'
    }
    this.undoIds += 1
    const expires = new Date(this.now().getTime() + 10 * 60_000)
    this.undoable.set(this.undoIds, { itemIds: [item.id], until: expires })
    return {
      added: vague ? [] : [item],
      needs_look: vague ? [item] : [],
      undo: { id: this.undoIds, expires_at: this.iso(expires) },
      proposal: null,
    }
  }

  undoCapture(id: number): Promise<void> {
    return this.call('undoCapture', () => {
      if (this.undone.has(id)) return // undone is undone: answered yes again, like the server
      const undo = this.undoable.get(id)
      if (!undo) throw new ApiError('not_found', `No capture #${String(id)}`, { status: 404 })
      if (this.now() > undo.until) {
        throw new ApiError('conflict', `The undo window for capture #${String(id)} has passed`, {
          status: 409,
        })
      }
      this.undoable.delete(id)
      this.undone.add(id)
      this.state.items = this.state.items.filter((i) => !undo.itemIds.includes(i.id))
    })
  }

  // -------------------------------------------------------------------------------------------------
  // The list (#22): the same rows maestro.feed builds, from the sample data.

  feed(days = 7): Promise<Feed> {
    return this.call('feed', () => {
      const now = this.now()
      const today = wallTime(now, this.tz)
      const until = fromWallTime(
        { ...addDays(today, days), hour: 0, minute: 0, second: 0 },
        this.tz,
      )
      const live = this.state.items.filter((i) => i.status !== 'done')
      const upcoming: Feed['upcoming'] = []
      let later = 0
      for (const item of live) {
        if (item.gap || item.kind !== 'reminder' || !item.due_at) continue
        const rows = this.occurrences(item, now, until)
        if (rows.length) upcoming.push(...rows)
        else later += 1
      }
      upcoming.sort((a, b) => a.at.localeCompare(b.at) || a.item.id - b.item.id)
      return {
        now: this.iso(now),
        until: this.iso(until),
        timezone: this.tz,
        needs_look: live
          .filter((i) => i.gap)
          .sort(byNewest)
          .map((i) => this.view(i)),
        upcoming,
        anytime: live
          .filter((i) => !i.gap && i.kind === 'task')
          .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id)
          .map((i) => this.view(i)),
        later,
      }
    })
  }

  fillGap(id: number, phrase: string): Promise<Item> {
    return this.call('fillGap', () => {
      const item = this.find(id)
      if (!item.gap)
        throw new ApiError('not_found', `No item #${String(id)} needing a look`, { status: 404 })
      const wanted = GAP_WANTED[item.gap] ?? 'an answer'
      // A suggested time comes back as the ISO stamp with its offset (#128), read exactly,
      // as maestro.gaps does; anything else is a phrase for the sample parser.
      const exact = item.gap === 'when' ? isoWithOffset(phrase) : null
      const read = exact ? null : parseCapture(phrase, this.now(), this.tz)
      if (item.gap === 'when') {
        const dueAt = exact ?? read?.dueAt
        if (!dueAt) throw this.cannotRead(wanted, id)
        item.due_at = this.iso(dueAt)
        item.recurrence = exact ? null : (read?.recurrence ?? null)
        item.kind = 'reminder'
      } else if (item.gap === 'channel') {
        if (!read?.channels) throw this.cannotRead(wanted, id)
        item.channels = read.channels
      } else {
        if (!read?.text.trim()) throw this.cannotRead(wanted, id)
        item.text = read.text
        if (read.dueAt) {
          item.due_at = this.iso(read.dueAt)
          item.kind = 'reminder'
        } else item.kind = read.kind
      }
      item.gap = null
      return this.view(item)
    })
  }

  gapSuggestions(id: number): Promise<GapSuggestions> {
    return this.call('gapSuggestions', () => {
      const item = this.find(id)
      if (!item.gap) {
        throw new ApiError('not_found', `No item #${String(id)} needing a look`, { status: 404 })
      }
      return {
        said: saidOf(item.text),
        suggestions: item.gap === 'when' ? this.suggestTimes(item.text) : [],
      }
    })
  }

  keepGap(id: number): Promise<Item> {
    return this.call('keepGap', () => {
      const item = this.find(id)
      if (!item.gap) {
        throw new ApiError('not_found', `No item #${String(id)} needing a look`, { status: 404 })
      }
      item.gap = null
      return this.view(item)
    })
  }

  /** The server's suggestion shapes (maestro/suggest.py), for the phrases the mock understands. */
  private suggestTimes(text: string): GapSuggestion[] {
    const now = this.now()
    const today = wallTime(now, this.tz)
    const at = (days: number, hour: number, reason: string): GapSuggestion => ({
      at: this.iso(fromWallTime({ ...addDays(today, days), hour, minute: 0, second: 0 }, this.tz)),
      reason,
    })
    // Days until the next Monday (1-7) and Saturday (1-7), strictly ahead, as the server counts.
    const nextMonday = ((0 - today.weekday + 6) % 7) + 1
    const nextSaturday = ((5 - today.weekday + 6) % 7) + 1
    let picks: GapSuggestion[]
    if (/\btonight\b/i.test(text)) {
      picks = [at(0, 19, 'tonight'), at(0, 20, 'tonight'), at(0, 21, 'tonight')]
    } else if (/\b(?:next|this) week\b/i.test(text)) {
      picks = [
        at(nextMonday, 9, 'start of the week'),
        at(nextMonday, 18, 'start of the week, evening'),
        at(nextMonday + 5, 10, 'the weekend'),
      ]
    } else {
      picks = [
        at(1, 9, 'tomorrow morning'),
        at(1, 18, 'tomorrow evening'),
        at(nextSaturday, 10, 'the weekend'),
      ]
    }
    return picks.filter((p) => new Date(p.at) > now).slice(0, 3)
  }

  dismissGap(id: number): Promise<void> {
    return this.call('dismissGap', () => {
      const item = this.find(id)
      if (!item.gap)
        throw new ApiError('not_found', `No item #${String(id)} needing a look`, { status: 404 })
      this.state.items = this.state.items.filter((i) => i !== item)
    })
  }

  private cannotRead(wanted: string, id: number): ApiError {
    // The server's own wording (maestro/api.py fill_needs_look).
    return new ApiError(
      'invalid',
      `Couldn't read ${wanted} in that; #${String(id)} still needs ${wanted}`,
      {
        status: 422,
      },
    )
  }

  /** The rows one reminder puts in the window (maestro.feed._occurrences). */
  private occurrences(item: StoredItem, now: Date, until: Date): Feed['upcoming'] {
    if (!item.due_at) return []
    const rows: Feed['upcoming'] = []
    const view = this.view(item)
    const delivered =
      this.awaiting(item) && item.delivered ? new Date(item.delivered.occurrence) : null
    let acts = true
    if (delivered) {
      rows.push({
        item: view,
        at: this.iso(delivered),
        state: 'awaiting',
        held_until: null,
        escalation_label: item.escalation_label ?? null,
        acts: true,
      })
      acts = false
    }
    const first = new Date(item.snoozed_until ?? item.due_at)
    const seenDays = new Set<string>()
    const series = this.series(item)
    let at = first
    let next: Date | null =
      series && first.getTime() !== new Date(item.due_at).getTime() ? new Date(item.due_at) : null
    let count = 0
    while (at < until && count < 31) {
      const day = this.iso(at).slice(0, 10)
      const skip = seenDays.has(day) || (delivered !== null && at <= delivered)
      if (!skip) {
        seenDays.add(day)
        const sentAt = sendTime(item, at, this.quiet, this.tz)
        const state =
          sentAt > at
            ? 'held'
            : at <= now
              ? 'overdue'
              : at.getTime() === first.getTime() && item.snoozed_until
                ? 'snoozed'
                : 'scheduled'
        rows.push({
          item: view,
          at: this.iso(at),
          state,
          held_until: sentAt > at ? this.iso(sentAt) : null,
          escalation_label: null,
          acts,
        })
        acts = false
        count += 1
      }
      if (!series) break
      if (next) {
        at = next
        next = null
      } else {
        const { hour, minute, second } = wallTime(new Date(item.due_at), this.tz)
        at = nextOccurrence(series.rule, at, { hour, minute, second }, this.tz, at)
      }
    }
    return rows
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
  async registerDevice(masterToken: string, deviceName: string): Promise<SignIn> {
    // Past the lock, like HttpClient: this is how a signed-out client signs in.
    const result = await this.call(
      'registerDevice',
      () => {
        if (this.masterToken !== null && masterToken !== this.masterToken) {
          throw new ApiError('unauthorized', 'Invalid API token', { status: 401 })
        }
        const id = this.signedIn.length + 1
        const device: Device & { token: string } = {
          id,
          name: deviceName.trim().slice(0, 80) || 'Device',
          created_at: this.iso(this.now()),
          last_seen_at: null,
          current: false,
          token: `mdt_mock-${String(id)}-${String(this.now().getTime())}`,
        }
        this.signedIn.push(device)
        return device
      },
      { pastLock: true },
    )
    const { token, ...device } = result
    this.setToken(token)
    return { token, device }
  }

  pair(): Promise<PairResult> {
    return this.unsupported('pair')
  }

  listDevices(): Promise<Device[]> {
    return this.call('listDevices', () =>
      this.signedIn.map(({ token, ...device }) => ({ ...device, current: token === this.token })),
    )
  }

  revokeDevice(id: number): Promise<void> {
    return this.call('revokeDevice', () => {
      const at = this.signedIn.findIndex((d) => d.id === id)
      if (at < 0) throw new ApiError('not_found', `No device #${String(id)}`, { status: 404 })
      this.signedIn.splice(at, 1)
    })
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

  setToken(token: string | null): void {
    // Nothing to check it against; setting one unlocks the client, as it does the real one.
    this.token = token
    this.conn.unlock()
  }

  connection(): ConnectionState {
    return this.conn.current()
  }

  onConnectionChange(listener: (state: ConnectionState) => void): () => void {
    return this.conn.subscribe(listener)
  }

  // -------------------------------------------------------------------------------------------------
  // Internals

  /**
   * Runs one call the way a request would: fail at once while locked, wait out the latency, fail
   * if a failure is injected (changing nothing), otherwise bring deliveries up to date, run it and
   * save.
   */
  private async call<T>(
    method: Method,
    run: () => T,
    options: { pastLock?: boolean } = {},
  ): Promise<T> {
    if (!options.pastLock) {
      const lock = this.conn.locked()
      if (lock) throw lock
    }
    if (this.latencyMs > 0) await new Promise((resolve) => setTimeout(resolve, this.latencyMs))
    const injected = this.failure?.(method) ?? null
    if (injected) {
      this.conn.failed(injected)
      throw injected
    }
    this.deliverDue()
    const before = JSON.stringify(this.state)
    try {
      // Copy out, so a caller changing what it got can't change the mock's state.
      const result = structuredClone(run())
      this.conn.succeeded()
      return result
    } catch (error) {
      this.state = JSON.parse(before) as MockState // a call that fails changes nothing
      // The "server" answered: that says nothing new about the connection, unless it refused
      // the token, which locks the client as it would HttpClient.
      if (error instanceof ApiError) this.conn.failed(error)
      throw error
    } finally {
      this.persist()
    }
  }

  private unsupported<T>(method: Method): Promise<T> {
    return this.call(method, () => {
      throw unsupportedError(method)
    })
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
    if (!item.due_at) return item.kind === 'task' ? 'task' : 'note' // matches commands.when (#114)
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
