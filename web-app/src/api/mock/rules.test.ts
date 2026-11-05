import { describe, expect, it } from 'vitest'
import {
  complete,
  inQuietHours,
  isUrgent,
  quietEndsAfter,
  sendTime,
  snooze,
  textSaysUrgent,
  type Series,
} from './rules.ts'
import { toIso } from './time.ts'

// Expected moves come from maestro/storage/logic.py run on the same inputs (ADR 0009).

const TZ = 'Asia/Kolkata'
const d = (s: string) => new Date(s)
const iso = (x: Date | null) => (x ? toIso(x, TZ) : null)
const daily = (nextDue: string, snoozedUntil: string | null = null): Series => ({
  rule: 'daily',
  nextDue: d(nextDue),
  snoozedUntil: snoozedUntil ? d(snoozedUntil) : null,
})
const show = (m: { dueAt: Date; snoozedUntil: Date | null }) => ({
  dueAt: iso(m.dueAt),
  snoozedUntil: iso(m.snoozedUntil),
})

describe('done on a recurring reminder (ADR 0009)', () => {
  it('skips the upcoming occurrence; the series carries on', () => {
    expect(show(complete(daily('2026-11-06T07:30:00+05:30'), null, TZ))).toEqual({
      dueAt: '2026-11-07T07:30:00+05:30',
      snoozedUntil: null,
    })
  })

  it('finishes a delivered occurrence and moves to the next one', () => {
    const series = daily('2026-11-05T07:30:00+05:30')
    expect(show(complete(series, d('2026-11-05T07:30:00+05:30'), TZ))).toEqual({
      dueAt: '2026-11-06T07:30:00+05:30',
      snoozedUntil: null,
    })
  })

  it('when a re-send is snoozed, drops only that and keeps the series time', () => {
    const series = daily('2026-11-06T07:30:00+05:30', '2026-11-05T15:00:00+05:30')
    expect(show(complete(series, null, TZ))).toEqual({
      dueAt: '2026-11-06T07:30:00+05:30',
      snoozedUntil: null,
    })
  })
})

describe('snooze on a recurring reminder (ADR 0009)', () => {
  it('re-sends the upcoming occurrence at the snooze time and skips it in the series', () => {
    const series = daily('2026-11-06T07:30:00+05:30')
    expect(show(snooze(series, d('2026-11-05T15:00:00+05:30'), null, TZ))).toEqual({
      dueAt: '2026-11-07T07:30:00+05:30',
      snoozedUntil: '2026-11-05T15:00:00+05:30',
    })
  })

  it('skips every occurrence up to a snooze that reaches past the next one', () => {
    const series = daily('2026-11-06T07:30:00+05:30')
    const move = snooze(series, d('2026-11-06T09:00:00+05:30'), d('2026-11-05T07:30:00+05:30'), TZ)
    expect(show(move)).toEqual({
      dueAt: '2026-11-07T07:30:00+05:30',
      snoozedUntil: '2026-11-06T09:00:00+05:30',
    })
  })

  it('always leaves the snooze before the series time', () => {
    const move = snooze(
      daily('2026-11-06T07:30:00+05:30'),
      d('2026-11-05T15:00:00+05:30'),
      null,
      TZ,
    )
    expect(move.snoozedUntil?.getTime()).toBeLessThan(move.dueAt.getTime())
  })
})

describe('quiet hours', () => {
  const quiet = { start: '22:00', end: '07:00' }
  const plain = { urgent: false, text: 'Renew the car insurance' }
  const urgent = { urgent: true, text: 'Pick up the kids' }

  it('wrap past midnight with an exclusive end', () => {
    expect(inQuietHours(quiet, d('2026-11-05T23:15:00+05:30'), TZ)).toBe(true)
    expect(inQuietHours(quiet, d('2026-11-06T06:59:00+05:30'), TZ)).toBe(true)
    expect(inQuietHours(quiet, d('2026-11-06T07:00:00+05:30'), TZ)).toBe(false)
    expect(inQuietHours(quiet, d('2026-11-05T21:59:00+05:30'), TZ)).toBe(false)
  })

  it('hold a reminder that is not urgent until the window ends', () => {
    expect(iso(sendTime(plain, d('2026-11-05T23:15:00+05:30'), quiet, TZ))).toBe(
      '2026-11-06T07:00:00+05:30',
    )
    expect(iso(quietEndsAfter(quiet, d('2026-11-06T02:00:00+05:30'), TZ))).toBe(
      '2026-11-06T07:00:00+05:30',
    )
  })

  it('let urgent reminders through', () => {
    expect(iso(sendTime(urgent, d('2026-11-05T23:15:00+05:30'), quiet, TZ))).toBe(
      '2026-11-05T23:15:00+05:30',
    )
  })

  it('change nothing outside the window or when there are none', () => {
    expect(iso(sendTime(plain, d('2026-11-05T15:00:00+05:30'), quiet, TZ))).toBe(
      '2026-11-05T15:00:00+05:30',
    )
    expect(iso(sendTime(plain, d('2026-11-05T23:15:00+05:30'), null, TZ))).toBe(
      '2026-11-05T23:15:00+05:30',
    )
  })

  it('read urgency from the text for items stored before the flag', () => {
    expect(textSaysUrgent('!take insulin')).toBe(true)
    expect(textSaysUrgent('urgent: call the vet')).toBe(true)
    expect(textSaysUrgent('not urgent: call the vet')).toBe(false)
    expect(textSaysUrgent("isn't urgent, water plants")).toBe(false)
    expect(isUrgent({ urgent: null, text: '!take insulin' })).toBe(true)
    expect(isUrgent({ urgent: false, text: '!take insulin' })).toBe(false) // the flag wins
  })
})
