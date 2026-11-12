import { describe, expect, it } from 'vitest'
import type { Feed, Occurrence } from '../api/types.ts'
import { makeItem } from '../test/items.ts'
import { groupFeed, isEmpty } from './feedGroups.ts'

const TZ = 'Asia/Kolkata'
const NOW = new Date('2026-11-05T14:00:00+05:30') // Thursday

function row(id: number, at: string, extra: Partial<Occurrence> = {}): Occurrence {
  return {
    item: makeItem({ id, due_at: at }),
    at,
    state: 'scheduled',
    held_until: null,
    escalation_label: null,
    acts: true,
    ...extra,
  }
}

function feed(upcoming: Occurrence[], extra: Partial<Feed> = {}): Feed {
  return {
    now: NOW.toISOString(),
    until: '2026-11-12T00:00:00+05:30',
    timezone: TZ,
    needs_look: [],
    upcoming,
    anytime: [],
    later: 0,
    ...extra,
  }
}

const ids = (rows: Occurrence[]) => rows.map((o) => o.item.id)

describe('groupFeed', () => {
  it('takes the very next row as Next up and groups the rest by day in the server timezone', () => {
    const groups = groupFeed(
      feed([
        row(1, '2026-11-05T09:00:00+05:30'), // earlier today, overdue
        row(2, '2026-11-05T23:59:00+05:30'),
        row(3, '2026-11-06T00:00:00+05:30'),
        row(4, '2026-11-08T09:00:00+05:30'),
        row(5, '2026-11-11T09:00:00+05:30'),
      ]),
      NOW,
      TZ,
    )
    expect(groups.nextUp?.item.id).toBe(1)
    expect(ids(groups.today)).toEqual([2])
    expect(ids(groups.tomorrow)).toEqual([3])
    expect(ids(groups.thisWeek)).toEqual([4, 5])
    expect(groups.leftToday).toBe(2) // Next up (today) and row 2
  })

  it('reads the day in the server timezone, not UTC, and around midnight', () => {
    // 20:00 UTC on the 5th is 01:30 on the 6th in Kolkata: tomorrow
    const groups = groupFeed(
      feed([row(1, '2026-11-05T10:00:00Z'), row(2, '2026-11-05T20:00:00Z')]),
      NOW,
      TZ,
    )
    expect(ids(groups.tomorrow)).toEqual([2])
    // at 23:59 the same rows regroup: the first is still today, the second still tomorrow
    const late = groupFeed(
      feed([row(1, '2026-11-05T10:00:00Z'), row(2, '2026-11-05T20:00:00Z')]),
      new Date('2026-11-05T23:59:00+05:30'),
      TZ,
    )
    expect(ids(late.tomorrow)).toEqual([2])
    // and one minute later the day has moved on without any new data
    const midnight = groupFeed(
      feed([row(1, '2026-11-05T10:00:00Z'), row(2, '2026-11-05T20:00:00Z')]),
      new Date('2026-11-06T00:00:00+05:30'),
      TZ,
    )
    expect(midnight.nextUp?.item.id).toBe(1) // yesterday's, still first: overdue rows stay on top
    expect(ids(midnight.today)).toEqual([2])
    expect(midnight.tomorrow).toEqual([])
  })

  it('counts only rows that still take Done as left today', () => {
    const groups = groupFeed(
      feed([
        row(1, '2026-11-05T15:00:00+05:30'),
        row(1, '2026-11-05T17:00:00+05:30', { acts: false }), // a later occurrence of the series
        row(2, '2026-11-05T18:00:00+05:30'),
      ]),
      NOW,
      TZ,
    )
    expect(groups.leftToday).toBe(2)
  })

  it('puts Next up on tomorrow when nothing is left today', () => {
    const groups = groupFeed(feed([row(3, '2026-11-06T09:00:00+05:30')]), NOW, TZ)
    expect(groups.nextUp?.item.id).toBe(3)
    expect(groups.today).toEqual([])
    expect(groups.tomorrow).toEqual([])
    expect(groups.leftToday).toBe(0)
  })

  it('passes needs a look, anytime and later through', () => {
    const plumber = makeItem({ id: 9, kind: 'task', due_at: null, gap: 'when' })
    const task = makeItem({ id: 8, kind: 'task', due_at: null })
    const groups = groupFeed(
      feed([], { needs_look: [plumber], anytime: [task], later: 3 }),
      NOW,
      TZ,
    )
    expect(groups.needsLook).toEqual([plumber])
    expect(groups.anytime).toEqual([task])
    expect(groups.later).toBe(3)
    expect(groups.nextUp).toBeNull()
    expect(isEmpty(groups)).toBe(false)
  })

  it('is empty only when there is nothing at all', () => {
    expect(isEmpty(groupFeed(feed([]), NOW, TZ))).toBe(true)
    expect(isEmpty(groupFeed(feed([], { later: 1 }), NOW, TZ))).toBe(false)
    expect(isEmpty(groupFeed(feed([row(1, '2026-11-05T15:00:00+05:30')]), NOW, TZ))).toBe(false)
  })
})
