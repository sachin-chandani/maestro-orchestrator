// The backend's product rules that the screens show, ported for MockClient:
// - done and snooze on a recurring reminder act on one occurrence (ADR 0009, maestro/storage/logic.py)
// - quiet hours hold non-urgent reminders until the window ends; urgent ones get through
//   (maestro/quiet.py, ADR 0011)

import { fromWallTime, nextOccurrence, wallTime } from './time.ts'

/** A done or snooze acknowledges the latest delivery only if it went out within this window. */
export const ACK_WINDOW_MS = 12 * 60 * 60_000

/** A recurring item that is still running. */
export interface Series {
  rule: string
  /** The series' own next occurrence. */
  nextDue: Date
  /** One occurrence pushed back by the person, or null. Always before nextDue. */
  snoozedUntil: Date | null
}

/** Where an action leaves a recurring item. */
export interface Move {
  dueAt: Date
  snoozedUntil: Date | null
}

/** The series' first occurrence strictly after `after`, keeping its wall-clock time and rhythm. */
function advance(series: Series, after: Date, tz: string): Date {
  const { hour, minute, second } = wallTime(series.nextDue, tz)
  return nextOccurrence(series.rule, after, { hour, minute, second }, tz, series.nextDue)
}

/**
 * Done on a recurring item: one occurrence is finished, the series carries on. `target` is the
 * delivered occurrence being acknowledged, or null for the upcoming one, which is then skipped.
 */
export function complete(series: Series, target: Date | null, tz: string): Move {
  let snoozed = series.snoozedUntil
  let nextDue = series.nextDue
  if (target) {
    if (snoozed?.getTime() === target.getTime()) snoozed = null // done with the snoozed re-send
    if (nextDue <= target) nextDue = advance(series, target, tz)
  } else if (snoozed) {
    snoozed = null // the upcoming one was the snoozed re-send
  } else {
    nextDue = advance(series, nextDue, tz)
  }
  return { dueAt: nextDue, snoozedUntil: snoozed }
}

/**
 * Snooze on a recurring item: the occurrence goes out again at `until` while the series keeps its
 * own time, and any occurrence on or before `until` is skipped.
 */
export function snooze(series: Series, until: Date, target: Date | null, tz: string): Move {
  let passed: Date
  if (!target && !series.snoozedUntil) {
    passed = until > series.nextDue ? until : series.nextDue // snoozing the upcoming one: skip it
  } else {
    passed = target && target > until ? target : until
  }
  const dueAt = series.nextDue <= passed ? advance(series, passed, tz) : series.nextDue
  return { dueAt, snoozedUntil: until }
}

// ---------------------------------------------------------------------------------------------------
// Quiet hours

export interface QuietHours {
  /** Local wall-clock times, "22:00" and "07:00"; the end is exclusive. */
  start: string
  end: string
}

function minutesOf(hhmm: string): number {
  const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(hhmm)
  if (!m) throw new Error(`Quiet hours ${hhmm} should look like 22:00`)
  return Number(m[1]) * 60 + Number(m[2])
}

/** Whether `moment` falls inside the window, in `tz`. */
export function inQuietHours(quiet: QuietHours, moment: Date, tz: string): boolean {
  const start = minutesOf(quiet.start)
  const end = minutesOf(quiet.end)
  const w = wallTime(moment, tz)
  const now = w.hour * 60 + w.minute
  return start < end ? start <= now && now < end : now >= start || now < end
}

/** The next end of the window at or after `moment`: when held reminders go out. */
export function quietEndsAfter(quiet: QuietHours, moment: Date, tz: string): Date {
  const end = minutesOf(quiet.end)
  const w = wallTime(moment, tz)
  const at = { hour: Math.floor(end / 60), minute: end % 60, second: 0 }
  const today = fromWallTime({ year: w.year, month: w.month, day: w.day, ...at }, tz)
  return today > moment
    ? today
    : fromWallTime({ year: w.year, month: w.month, day: w.day + 1, ...at }, tz)
}

const NOT_URGENT = /\b(?:not|non|no|isn'?t|nothing)[\s-]+urgent\b/i

/** Text rule for items stored before the urgent flag existed (parser.text_says_urgent). */
export function textSaysUrgent(text: string): boolean {
  if (text.trimStart().startsWith('!')) return true
  return /\burgent\b/i.test(text) && !NOT_URGENT.test(text)
}

export function isUrgent(item: { urgent: boolean | null; text: string }): boolean {
  return item.urgent ?? textSaysUrgent(item.text)
}

/**
 * When a reminder due at `due` actually goes out: then, or at the end of quiet hours if it falls in
 * them and isn't urgent.
 */
export function sendTime(
  item: { urgent: boolean | null; text: string },
  due: Date,
  quiet: QuietHours | null,
  tz: string,
): Date {
  if (quiet && !isUrgent(item) && inQuietHours(quiet, due, tz))
    return quietEndsAfter(quiet, due, tz)
  return due
}
