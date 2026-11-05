import { describe, expect, it } from 'vitest'
import {
  describeRule,
  fromWallTime,
  isTimeZone,
  nextOccurrence,
  shortLabel,
  toIso,
  wallTime,
} from './time.ts'

// Expected values come from the backend (maestro/recurrence.py on Python's zoneinfo), so the mock
// moves reminders exactly as the server does.

const NY = 'America/New_York'
const KOLKATA = 'Asia/Kolkata'
const at = (hour: number, minute = 0) => ({ hour, minute, second: 0 })
const iso = (d: Date, tz: string) => toIso(d, tz)

describe('wall-clock time', () => {
  it('writes ISO 8601 with the zone offset, as the backend does', () => {
    const d = new Date('2026-11-05T12:15:00Z')
    expect(toIso(d, KOLKATA)).toBe('2026-11-05T17:45:00+05:30')
    expect(toIso(d, 'UTC')).toBe('2026-11-05T12:15:00+00:00')
    expect(toIso(d, NY)).toBe('2026-11-05T07:15:00-05:00')
  })

  it('round-trips a wall-clock time', () => {
    const d = fromWallTime({ year: 2026, month: 11, day: 5, hour: 17, minute: 45 }, KOLKATA)
    expect(d.toISOString()).toBe('2026-11-05T12:15:00.000Z')
    expect(wallTime(d, KOLKATA)).toMatchObject({ day: 5, hour: 17, minute: 45, weekday: 3 })
  })

  it('reads a time that happens twice (clocks go back) as the first one', () => {
    const d = fromWallTime({ year: 2026, month: 11, day: 1, hour: 1, minute: 30 }, NY)
    expect(iso(d, NY)).toBe('2026-11-01T01:30:00-04:00')
  })

  it('reads a time that never happens (clocks go forward) with the offset from before', () => {
    const d = fromWallTime({ year: 2026, month: 3, day: 8, hour: 2, minute: 30 }, NY)
    expect(iso(d, NY)).toBe('2026-03-08T03:30:00-04:00')
  })

  it('rolls over days past the end of the month', () => {
    const d = fromWallTime({ year: 2026, month: 12, day: 32, hour: 9 }, KOLKATA)
    expect(iso(d, KOLKATA)).toBe('2027-01-01T09:00:00+05:30')
  })

  it('labels times like the backend', () => {
    expect(shortLabel(new Date('2026-11-05T12:15:00Z'), KOLKATA)).toBe('Thu 05 Nov 17:45')
  })

  it('knows which timezones exist', () => {
    expect(isTimeZone(KOLKATA)).toBe(true)
    expect(isTimeZone('Mars/Olympus_Mons')).toBe(false)
  })
})

describe('nextOccurrence', () => {
  const next = (rule: string, after: string, tz: string, hm: [number, number], anchor?: string) =>
    iso(
      nextOccurrence(rule, new Date(after), at(...hm), tz, anchor ? new Date(anchor) : undefined),
      tz,
    )

  it('keeps a daily reminder at its wall-clock time across a DST change', () => {
    expect(next('daily', '2026-03-07T08:00:00-05:00', NY, [8, 0])).toBe('2026-03-08T08:00:00-04:00')
  })

  it('moves a monthly reminder on the 31st to the last day of a short month', () => {
    expect(next('monthly:31', '2027-01-31T09:00:00+05:30', KOLKATA, [9, 0])).toBe(
      '2027-02-28T09:00:00+05:30',
    )
  })

  it('skips the weekend for weekdays', () => {
    expect(next('weekdays', '2026-11-06T09:00:00+05:30', KOLKATA, [9, 0])).toBe(
      '2026-11-09T09:00:00+05:30',
    )
  })

  it('adds elapsed time for intervals, on the minute', () => {
    expect(next('interval:90', '2026-11-05T14:00:30+05:30', KOLKATA, [0, 0])).toBe(
      '2026-11-05T15:30:00+05:30',
    )
  })

  it('keeps the rhythm of every-N-weeks from the previous occurrence', () => {
    expect(
      next('weeks:2:0', '2026-11-20T09:00:00+05:30', KOLKATA, [9, 0], '2026-11-09T09:00:00+05:30'),
    ).toBe('2026-11-23T09:00:00+05:30')
  })

  it('rejects a rule it does not know', () => {
    expect(() => next('fortnightly', '2026-11-05T09:00:00+05:30', KOLKATA, [9, 0])).toThrow(
      /Unknown recurrence rule/,
    )
  })
})

describe('describeRule', () => {
  it.each([
    ['weekly:3', 'every Thursday'],
    ['interval:120', 'every 2h'],
    ['interval:90', 'every 90m'],
    ['days:2', 'every other day'],
    ['days:3', 'every 3 days'],
    ['weeks:2:0', 'every other Monday'],
    ['weeks:3:4', 'every 3 weeks on Friday'],
    ['monthly:15', 'monthly on day 15'],
    ['daily', 'daily'],
    ['weekdays', 'weekdays'],
  ])('describes %s as the backend does', (rule, label) => {
    expect(describeRule(rule)).toBe(label)
  })
})
