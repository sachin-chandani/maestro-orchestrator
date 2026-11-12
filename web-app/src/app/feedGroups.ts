// The one list (#22, ADR 0017): the feed's rows grouped into the design's sections, by calendar day
// in the server's timezone. Pure, and given `now`, so the screen can regroup at midnight without
// asking the server again, and tests can pin the midnight and DST cases.

import type { Feed, Item, Occurrence } from '../api/types.ts'
import { dayKey, nextDayKey } from './groups.ts'

export type FeedGroupName =
  'Needs a look' | 'Next up' | 'Today' | 'Tomorrow' | 'This week' | 'Anytime'

export interface FeedGroups {
  /** Saved with a gap; present only when there is at least one. */
  needsLook: Item[]
  /** The very next occurrence, whatever its day, shown big. Null when nothing is timed. */
  nextUp: Occurrence | null
  /** The rest of today, after Next up. Overdue and awaiting rows are here too. */
  today: Occurrence[]
  tomorrow: Occurrence[]
  /** Days +2 to the end of the window; the screen collapses these to a count by default. */
  thisWeek: Occurrence[]
  /** Tasks, oldest first. */
  anytime: Item[]
  /** How many left today: rows today (and Next up if it is today) that have not been answered. */
  leftToday: number
  /** Reminders past the window, counted on the server. */
  later: number
}

export function groupFeed(feed: Feed, now: Date, timeZone: string): FeedGroups {
  const today = dayKey(now, timeZone)
  const tomorrow = nextDayKey(today)
  const [first, ...rest] = feed.upcoming
  const groups: FeedGroups = {
    needsLook: feed.needs_look,
    nextUp: first ?? null,
    today: [],
    tomorrow: [],
    thisWeek: [],
    anytime: feed.anytime,
    leftToday: 0,
    later: feed.later,
  }
  for (const row of rest) {
    const day = dayKey(new Date(row.at), timeZone)
    if (day <= today) groups.today.push(row)
    else if (day === tomorrow) groups.tomorrow.push(row)
    else groups.thisWeek.push(row)
  }
  const todayRows = [
    ...(first && dayKey(new Date(first.at), timeZone) <= today ? [first] : []),
    ...groups.today,
  ]
  groups.leftToday = todayRows.filter((o) => o.acts).length
  return groups
}

/** Whether the whole list is empty: nothing to show but the capture bar. */
export function isEmpty(groups: FeedGroups): boolean {
  return (
    groups.needsLook.length === 0 &&
    groups.nextUp === null &&
    groups.anytime.length === 0 &&
    groups.later === 0
  )
}

/** "Fri 13", for a row in This week, where a time alone would be ambiguous. */
export { laterLabel as dayLabel } from './groups.ts'
