// Wall-clock time in a named timezone, and the recurrence rules, for MockClient. Ported from
// maestro/tz.py and maestro/recurrence.py so the mock moves reminders the way the server does,
// using only Intl (no date library).

import { dateTimeFormat } from '../../lib/intl.ts'
import type { IsoDateTime } from '../types.ts'

const MINUTE = 60_000
const DAY = 24 * 60 * MINUTE

export interface Ymd {
  year: number
  /** 1–12 */
  month: number
  day: number
}

export interface Hms {
  hour: number
  minute: number
  second: number
}

export type WallTime = Ymd &
  Hms & {
    /** 0 = Monday … 6 = Sunday, as in Python's date.weekday(). */
    weekday: number
  }

function formatter(tz: string): Intl.DateTimeFormat {
  return dateTimeFormat('en-US', {
    timeZone: tz,
    hourCycle: 'h23',
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
  })
}

/** True if `tz` is an IANA timezone the runtime knows. */
export function isTimeZone(tz: string): boolean {
  try {
    formatter(tz)
    return true
  } catch {
    return false
  }
}

export function weekdayOf(d: Ymd): number {
  return (new Date(Date.UTC(d.year, d.month - 1, d.day)).getUTCDay() + 6) % 7
}

/** `d` plus `n` days (negative goes back); month and year ends roll over. */
export function addDays(d: Ymd, n: number): Ymd {
  const t = new Date(Date.UTC(d.year, d.month - 1, d.day + n))
  return { year: t.getUTCFullYear(), month: t.getUTCMonth() + 1, day: t.getUTCDate() }
}

function dayNumber(d: Ymd): number {
  return Math.round(Date.UTC(d.year, d.month - 1, d.day) / DAY)
}

/** The wall-clock reading of `instant` in `tz`. */
export function wallTime(instant: Date, tz: string): WallTime {
  const parts: Record<string, number> = {}
  for (const p of formatter(tz).formatToParts(instant)) {
    if (p.type !== 'literal') parts[p.type] = Number(p.value)
  }
  const ymd = { year: parts.year ?? 0, month: parts.month ?? 0, day: parts.day ?? 0 }
  return {
    ...ymd,
    hour: parts.hour ?? 0,
    minute: parts.minute ?? 0,
    second: parts.second ?? 0,
    weekday: weekdayOf(ymd),
  }
}

/** Minutes east of UTC that `tz` is at `instant`: 330 for Asia/Kolkata. */
export function offsetMinutes(instant: Date, tz: string): number {
  const w = wallTime(instant, tz)
  const asUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second)
  return Math.round((asUtc - Math.floor(instant.getTime() / 1000) * 1000) / MINUTE)
}

/**
 * The instant a wall-clock time in `tz` names, like Python's datetime(..., tzinfo=ZoneInfo(tz)):
 * a time that happens twice (clocks go back) is the first one; a time that never happens (clocks go
 * forward) is read with the offset from before the change, so 02:30 on a spring-forward night in
 * New York is 03:30. Fields past their range roll over (day 32 is the 1st of the next month).
 */
export function fromWallTime(w: Ymd & Partial<Hms>, tz: string): Date {
  const asUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour ?? 0, w.minute ?? 0, w.second ?? 0)
  const wanted = wallTime(new Date(asUtc), 'UTC')
  const before = offsetMinutes(new Date(asUtc - DAY), tz)
  const after = offsetMinutes(new Date(asUtc + DAY), tz)
  const candidates = [asUtc - before * MINUTE, asUtc - after * MINUTE]
  const exact = candidates.find((c) => {
    const got = wallTime(new Date(c), tz)
    return (
      got.year === wanted.year &&
      got.month === wanted.month &&
      got.day === wanted.day &&
      got.hour === wanted.hour &&
      got.minute === wanted.minute
    )
  })
  return new Date(exact ?? asUtc - before * MINUTE)
}

const pad = (n: number, width = 2) => String(Math.abs(n)).padStart(width, '0')

/** ISO 8601 with the offset `tz` has at that moment, as the backend writes it. */
export function toIso(instant: Date, tz: string): IsoDateTime {
  const w = wallTime(instant, tz)
  const off = offsetMinutes(instant, tz)
  const sign = off < 0 ? '-' : '+'
  return (
    `${pad(w.year, 4)}-${pad(w.month)}-${pad(w.day)}T${pad(w.hour)}:${pad(w.minute)}:${pad(w.second)}` +
    `${sign}${pad(Math.trunc(off / 60))}:${pad(off % 60)}`
  )
}

const SHORT_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const SHORT_MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
]

/** "Thu 05 Nov 17:45", like the backend's strftime("%a %d %b %H:%M"). */
export function shortLabel(instant: Date, tz: string): string {
  const w = wallTime(instant, tz)
  return `${SHORT_DAYS[w.weekday] ?? ''} ${pad(w.day)} ${SHORT_MONTHS[w.month - 1] ?? ''} ${pad(w.hour)}:${pad(w.minute)}`
}

// ---------------------------------------------------------------------------------------------------
// Recurrence rules (maestro/recurrence.py): "daily", "weekdays", "weekly:<0-6>", "monthly:<day>",
// "interval:<minutes>", "days:<n>", "weeks:<n>:<weekday>".

export const WEEKDAYS = [
  'monday',
  'tuesday',
  'wednesday',
  'thursday',
  'friday',
  'saturday',
  'sunday',
] as const

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/** "every Thursday", "every 2h": recurrence.describe(). */
export function describeRule(rule: string): string {
  const [kind = '', ...rest] = rule.split(':')
  const arg = rest.join(':')
  if (kind === 'interval') {
    const minutes = Number(arg)
    return minutes % 60 === 0 ? `every ${String(minutes / 60)}h` : `every ${arg}m`
  }
  if (kind === 'weekly') return `every ${capitalize(WEEKDAYS[Number(arg)] ?? '')}`
  if (kind === 'days') return arg === '2' ? 'every other day' : `every ${arg} days`
  if (kind === 'weeks') {
    const [n = '', weekday = ''] = arg.split(':')
    const day = capitalize(WEEKDAYS[Number(weekday)] ?? '')
    return n === '2' ? `every other ${day}` : `every ${n} weeks on ${day}`
  }
  if (kind === 'monthly') return `monthly on day ${arg}`
  return kind
}

function monthDay(year: number, month: number, day: number): Ymd {
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate()
  return { year, month, day: Math.min(day, last) }
}

/**
 * The first occurrence of `rule` at wall-clock time `at` strictly after `after`
 * (recurrence.next_occurrence). Calendar rules keep their wall-clock time across DST; interval rules
 * add elapsed time. `anchor` is the previous occurrence, which every-N-days/weeks rules step from.
 */
export function nextOccurrence(
  rule: string,
  after: Date,
  at: Hms,
  tz: string,
  anchor?: Date,
): Date {
  const [kind = '', ...rest] = rule.split(':')
  const arg = rest.join(':')
  const combine = (d: Ymd) => fromWallTime({ ...d, ...at }, tz)

  if (kind === 'interval') {
    const t = after.getTime() + Number(arg) * MINUTE
    return new Date(t - (t % MINUTE))
  }

  const afterDate = wallTime(after, tz)

  if (kind === 'days' || kind === 'weeks') {
    const [n = '1', weekday = '0'] = arg.split(':')
    const step = kind === 'days' ? Number(n) : Number(n) * 7
    if (!anchor)
      return nextOccurrence(kind === 'days' ? 'daily' : `weekly:${weekday}`, after, at, tz)
    const start = wallTime(anchor, tz)
    let k = Math.max(1, Math.floor((dayNumber(afterDate) - dayNumber(start)) / step))
    let candidate = combine(addDays(start, k * step))
    while (candidate <= after) {
      k += 1
      candidate = combine(addDays(start, k * step))
    }
    return candidate
  }

  if (kind === 'monthly') {
    const day = Number(arg)
    let candidate = combine(monthDay(afterDate.year, afterDate.month, day))
    if (candidate <= after) {
      const [year, month] =
        afterDate.month === 12 ? [afterDate.year + 1, 1] : [afterDate.year, afterDate.month + 1]
      candidate = combine(monthDay(year, month, day))
    }
    return candidate
  }

  const matches = (d: Ymd): boolean => {
    if (kind === 'daily') return true
    if (kind === 'weekdays') return weekdayOf(d) < 5
    if (kind === 'weekly') return weekdayOf(d) === Number(arg)
    throw new Error(`Unknown recurrence rule: ${rule}`)
  }
  for (let offset = 0; offset < 8; offset++) {
    const d = addDays(afterDate, offset)
    const candidate = combine(d)
    if (candidate > after && matches(d)) return candidate
  }
  throw new Error(`No occurrence found for ${rule}`)
}
