// Today / Tomorrow / Later, by calendar day in the server's timezone. The design's three groups;
// #103 adds "This week" and the tests around midnight and DST.

import type { Item } from '../api/types.ts'

export type GroupName = 'Today' | 'Tomorrow' | 'Later'

export interface Group {
  name: GroupName
  items: Item[]
}

const dayFormats = new Map<string, Intl.DateTimeFormat>()

/** "2026-11-05": the calendar day of `instant` in `timeZone`. */
export function dayKey(instant: Date, timeZone: string): string {
  let f = dayFormats.get(timeZone)
  if (!f) {
    f = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    })
    dayFormats.set(timeZone, f)
  }
  return f.format(instant)
}

/** The day after `key`, by the calendar (so a 23- or 25-hour day can't skip or repeat one). */
export function nextDayKey(key: string): string {
  const [y = 0, m = 1, d = 1] = key.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10)
}

/** When a reminder next goes out: its snooze if it has one, else its due time. */
export function nextTime(item: Item): string | null {
  return item.snoozed_until ?? item.due_at
}

/**
 * Reminders (not notes) grouped by the day they next go out; anything earlier than today, such as
 * an overdue reminder, is in Today. Order within a group is the client's, which is by send time.
 */
export function groupReminders(items: Item[], now: Date, timeZone: string): Group[] {
  const today = dayKey(now, timeZone)
  const tomorrow = nextDayKey(today)
  const groups: Record<GroupName, Item[]> = { Today: [], Tomorrow: [], Later: [] }
  for (const item of items) {
    const next = nextTime(item)
    if (item.kind !== 'reminder' || !next) continue
    const day = dayKey(new Date(next), timeZone)
    groups[day <= today ? 'Today' : day === tomorrow ? 'Tomorrow' : 'Later'].push(item)
  }
  return (['Today', 'Tomorrow', 'Later'] as const)
    .map((name) => ({ name, items: groups[name] }))
    .filter((g) => g.items.length > 0)
}

const laterFormats = new Map<string, Intl.DateTimeFormat>()

/** The time column for a Later reminder, where a time alone would be ambiguous: "Fri 8". */
export function laterLabel(iso: string, timeZone: string): string {
  let f = laterFormats.get(timeZone)
  if (!f) {
    f = new Intl.DateTimeFormat('en-GB', { timeZone, weekday: 'short', day: 'numeric' })
    laterFormats.set(timeZone, f)
  }
  const parts = f.formatToParts(new Date(iso))
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  return `${part('weekday')} ${part('day')}`
}

const headerFormats = new Map<string, Intl.DateTimeFormat>()

/** "Thu 5 Nov", for the header. */
export function headerDate(now: Date, timeZone: string): string {
  let f = headerFormats.get(timeZone)
  if (!f) {
    f = new Intl.DateTimeFormat('en-GB', {
      timeZone,
      weekday: 'short',
      day: 'numeric',
      month: 'short',
    })
    headerFormats.set(timeZone, f)
  }
  const parts = f.formatToParts(now)
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  return `${part('weekday')} ${part('day')} ${part('month')}`
}
