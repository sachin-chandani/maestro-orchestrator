import { describe, expect, it } from 'vitest'
import { makeItem } from '../test/items.ts'
import { dayKey, groupReminders, headerDate, laterLabel, nextDayKey } from './groups.ts'

const TZ = 'Asia/Kolkata'
const NOW = new Date('2026-11-05T14:00:00+05:30')

const at = (id: number, due: string, extra = {}) => makeItem({ id, due_at: due, ...extra })
const names = (items: ReturnType<typeof groupReminders>) =>
  items.map((g) => [g.name, g.items.map((i) => i.id)])

describe('groupReminders', () => {
  it('groups by the calendar day in the server timezone', () => {
    const items = [
      at(1, '2026-11-05T09:00:00+05:30'), // earlier today, overdue
      at(2, '2026-11-05T23:59:00+05:30'),
      at(3, '2026-11-06T00:00:00+05:30'),
      at(4, '2026-11-08T09:00:00+05:30'),
    ]
    expect(names(groupReminders(items, NOW, TZ))).toEqual([
      ['Today', [1, 2]],
      ['Tomorrow', [3]],
      ['Later', [4]],
    ])
  })

  it('reads the day in the server timezone, not UTC', () => {
    // 20:00 UTC on the 5th is 01:30 on the 6th in Kolkata.
    expect(names(groupReminders([at(1, '2026-11-05T20:00:00Z')], NOW, TZ))).toEqual([
      ['Tomorrow', [1]],
    ])
  })

  it('files a snoozed reminder by its snooze, not its series time', () => {
    const item = at(1, '2026-11-12T07:30:00+05:30', { snoozed_until: '2026-11-05T15:00:00+05:30' })
    expect(names(groupReminders([item], NOW, TZ))).toEqual([['Today', [1]]])
  })

  it('leaves out notes and empty groups', () => {
    const note = makeItem({ id: 9, kind: 'note', due_at: null })
    expect(groupReminders([note], NOW, TZ)).toEqual([])
  })
})

describe('day helpers', () => {
  it('step to the next calendar day across month, year and a DST change', () => {
    expect(nextDayKey('2026-11-30')).toBe('2026-12-01')
    expect(nextDayKey('2026-12-31')).toBe('2027-01-01')
    expect(nextDayKey('2026-03-08')).toBe('2026-03-09') // New York's 23-hour day
    expect(dayKey(new Date('2026-11-05T20:00:00Z'), TZ)).toBe('2026-11-06')
  })

  it('label Later times and the header', () => {
    expect(laterLabel('2026-11-08T09:00:00+05:30', TZ)).toBe('Sun 8')
    expect(headerDate(NOW, TZ)).toBe('Thu 5 Nov')
  })
})
