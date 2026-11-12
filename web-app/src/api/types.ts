// Domain types shared by every MaestroClient implementation and the screens (#101).
//
// Item and Health mirror the backend's JSON exactly (maestro/api.py: ItemOut, /health), field names
// included, so the HTTP client passes them through without a mapping layer. Everything else
// describes endpoints that don't exist yet; see SUPPORT in client.ts for what the backend serves today.

/** An ISO 8601 date-time with offset, as the backend sends it: "2026-11-05T17:45:00+05:30". */
export type IsoDateTime = string

// ---------------------------------------------------------------------------------------------------
// Items: served by the backend today (/items)

/** 'task' is something to do with no time: never delivered, done works on it (#114). */
export type ItemKind = 'reminder' | 'task' | 'note'
export type ItemStatus = 'pending' | 'fired' | 'done'
/** What a saved reading is still missing, when it is waiting for an answer (#126, ADR 0017). */
export type ItemGap = 'when' | 'what' | 'channel'

/** A delivery channel name, as in MAESTRO_CHANNELS: "twilio_sms", "telegram", "email", "ntfy"… */
export type ChannelName = string

/** One stored item, exactly as GET /items returns it (ItemOut). */
export interface Item {
  id: number
  text: string
  kind: ItemKind
  status: ItemStatus
  due_at: IsoDateTime | null
  /** A recurring reminder's snoozed occurrence, sent before due_at (ADR 0009). */
  snoozed_until: IsoDateTime | null
  created_at: IsoDateTime
  /** The rule, e.g. "weekly:3"; recurrence_label is the readable form, "every Thursday". */
  recurrence: string | null
  recurrence_label: string | null
  /** This item's own channels; null means the server's default channels. */
  channels: ChannelName[] | null
  /** Urgent reminders ignore quiet hours. null on items stored before the flag existed. */
  urgent: boolean | null
  /** Human-readable, in the server's timezone: "Wed 28 Oct 17:00, every Monday", "task" or "note". */
  when: string
  /** Delivered, and nobody has said done or snooze yet. */
  awaiting_acknowledgement: boolean
  source_text: string
  /**
   * What this item still needs before anything can happen to it (#126, ADR 0017). null — the
   * normal case — means nothing does; an item with a gap is saved and visible but is never
   * scheduled and never delivered until it is answered. The screen for these is #22 / #104.
   */
  gap?: ItemGap | null

  // Shown by the design (docs/design/mobile/Today.dc.html) but NOT sent by the backend yet.
  // MockClient fills them; HttpClient leaves them undefined until the backend adds them.
  /** "1 hour before" (lead-time reminders). */
  lead_time_label?: string
  /** "Call if not done in 15m" (escalation stage). */
  escalation_label?: string
  /** Consecutive occurrences done on time, for recurring reminders. */
  streak?: number
  /** Held by quiet hours until this time. */
  held_until?: IsoDateTime
  /** Where the capture came from: "telegram", "twilio_sms", "twilio_call", "email", "web"… */
  source?: string
}

export interface ListItemsOptions {
  /** Include done items (default false). */
  include_done?: boolean
  /** Text search; combines with `unacknowledged`. */
  q?: string
  /** Only reminders delivered and not yet acknowledged. */
  unacknowledged?: boolean
}

/**
 * How long to snooze: a number and a unit, "10m", "2h", "1d" (maestro.parser.parse_duration).
 * The backend rejects anything else with a readable message.
 */
export type SnoozeDuration = string

/** Changes to an item. No backend endpoint exists for editing yet. */
export interface ItemEdit {
  text?: string
  due_at?: IsoDateTime
}

/** GET /health. */
export interface Health {
  status: string
  version: string
  schema: number
  /** IANA name of the server's timezone (MAESTRO_TIMEZONE); groups and times use it. */
  timezone: string
  backend: string
}

// ---------------------------------------------------------------------------------------------------
// Connection: kept by the client itself from the outcome of its requests; no endpoint involved.

export type ConnectionState =
  | { status: 'connected' }
  /** The server can't be reached; captures made meanwhile are queued (#104). */
  | { status: 'offline'; queued: number }
  /** No token, or the server rejected it (401). Requests stop until a token is set. */
  | { status: 'signed_out' }
  /** Too many failed attempts (429). Requests stop, as the current page does. */
  | { status: 'rate_limited'; retry_after_seconds: number | null }

// ---------------------------------------------------------------------------------------------------
// Parse preview: POST /parse does not exist yet (#94).

/** The chip kinds the design shows (docs/design/mobile/README.md, #102). */
export type ChipKind =
  'time' | 'recurrence' | 'channel' | 'urgent' | 'escalation' | 'lead' | 'streak' | 'list' | 'note'

export interface PreviewChip {
  kind: ChipKind
  /** "Tomorrow", "9:00 am", "Text me", "List: shopping", "Note, no time"… */
  label: string
  /** Shown when the chip is tapped (#104): "Read as a time. Say 'not a time' to keep it as text." */
  explanation?: string
}

/** What a capture would create, without saving it. */
export interface ParsePreview {
  text: string
  kind: ItemKind
  due_at: IsoDateTime | null
  recurrence: string | null
  recurrence_label: string | null
  channels: ChannelName[] | null
  urgent: boolean
  /** Set when the capture adds to a list ("add milk to the shopping list"), #88. */
  list: { name: string; items: string[] } | null
  chips: PreviewChip[]
}

// ---------------------------------------------------------------------------------------------------
// Lists: no backend support yet (#88).

export interface ListEntry {
  id: number
  text: string
  checked: boolean
}

export interface TodoList {
  id: number
  name: string
  entries: ListEntry[]
}

// ---------------------------------------------------------------------------------------------------
// The upcoming feed (GET /feed, #22): everything the list shows, in one call.

/** What an occurrence is doing, in the scheduler's terms (maestro.feed.STATES). */
export type OccurrenceState = 'scheduled' | 'overdue' | 'awaiting' | 'held' | 'snoozed'

/** One time a reminder goes (or went) out: a row in the list. */
export interface Occurrence {
  item: Item
  /** When it goes out: the snoozed time, the due time, or the delivered occurrence. */
  at: IsoDateTime
  state: OccurrenceState
  /** While held by quiet hours: when they let it out. */
  held_until: IsoDateTime | null
  /** While awaiting an answer: the next escalation stage, "text at 17:12". */
  escalation_label: string | null
  /** Done and +1 hour apply to this row (the first occurrence of its series, ADR 0009). */
  acts: boolean
}

export interface Feed {
  now: IsoDateTime
  /** The end of the window, exclusive. */
  until: IsoDateTime
  /** The server's timezone; the client groups rows into days in it. */
  timezone: string
  /** Saved with a gap, newest first. */
  needs_look: Item[]
  /** Every row in the window, by `at`. */
  upcoming: Occurrence[]
  /** Tasks: reminders with no time, oldest first. */
  anytime: Item[]
  /** Reminders whose next time is past the window; none of their rows are listed. */
  later: number
}

// ---------------------------------------------------------------------------------------------------
// Capture that saves what it can and names what still needs a look (POST /capture, ADR 0017).

export interface UndoToken {
  id: number
  expires_at: IsoDateTime
}

/** What one capture did (CaptureOut): what was saved, what still needs a look, and the undo for all of it. */
export interface CaptureResult {
  /** Saved and, if they have a time, scheduled. */
  added: Item[]
  /** Saved with a `gap`: visible, never scheduled until it is answered. */
  needs_look: Item[]
  /** null when the capture saved nothing to take back. */
  undo: UndoToken | null
  /**
   * Under MAESTRO_CONFIRM=unsure the server holds the whole message as a proposal instead (ADR
   * 0016) and answers 202; then nothing was saved and this names what would be.
   */
  proposal: { id: number; lines: string[] } | null
}

// ---------------------------------------------------------------------------------------------------
// Devices (POST /devices, ADR 0018) and pairing by code (#95, not built yet).

/** A phone or browser signed in with a token of its own (DeviceOut). */
export interface Device {
  id: number
  name: string
  created_at: IsoDateTime
  /** To the hour. */
  last_seen_at: IsoDateTime | null
  /** The device making this request. */
  current: boolean
}

/** The answer to signing a device in: its one and only token, which the client keeps from then on. */
export interface SignIn {
  token: string
  device: Device
}

export interface PairRequest {
  /** The server address from the QR code or typed in: "https://maestro.example". */
  server_url: string
  /** The one-time code `maestro pair` shows. */
  code: string
  device_name: string
}

export interface PairResult {
  /** This device's own token; the client stores it and uses it from then on. */
  token: string
  device: Device
  server_name: string
}

// ---------------------------------------------------------------------------------------------------
// Settings: no backend support yet (#98).

/** A setting's value, and the environment variable that fixes it, if any (env vars win, #98). */
export interface Setting<T> {
  value: T
  /** e.g. "MAESTRO_QUIET_HOURS" when set in the environment; the UI shows it read-only. */
  fixed_by: string | null
}

export interface QuietHours {
  /** Local wall-clock times, "22:00" and "07:00". */
  start: string
  end: string
}

export interface EscalationStage {
  /** How long after the reminder: "10m", "30m". */
  after: string
  channel: ChannelName
}

/** A secret is write-only: reading it only says whether it is set (#98, #107). */
export interface SecretState {
  is_set: boolean
  fixed_by: string | null
}

export interface ChannelStatus {
  name: ChannelName
  /** From the same checks as `maestro doctor`. */
  status: 'ok' | 'not_configured' | 'error'
  detail: string | null
  /** Keyed by setting name, e.g. { TWILIO_AUTH_TOKEN: { is_set: true, fixed_by: null } }. */
  secrets: Record<string, SecretState>
}

export interface Settings {
  channels: ChannelStatus[]
  default_channels: Setting<ChannelName[]>
  quiet_hours: Setting<QuietHours | null>
  escalation: Setting<EscalationStage[]>
}

export interface SettingsUpdate {
  default_channels?: ChannelName[]
  quiet_hours?: QuietHours | null
  escalation?: EscalationStage[]
  /** New secret values by channel and setting name. Never returned once saved. */
  secrets?: Record<ChannelName, Record<string, string>>
}
