// The list's rows in reading order (#22): what the keyboard walks with j and k, and how each row
// is found in the page. Shared by the list and the screen; kept apart from the components so the
// dev server can hot-reload them.

import type { Item, Occurrence } from '../api/types.ts'
import type { FeedGroups } from './feedGroups.ts'

export type Row =
  | { kind: 'occurrence'; occurrence: Occurrence }
  | { kind: 'needs'; item: Item }
  | { kind: 'task'; item: Item }

/** A row's DOM id, so the keyboard can focus it (j/k). */
export function rowId(row: Row): string {
  if (row.kind === 'occurrence') return `row-${String(row.occurrence.item.id)}-${row.occurrence.at}`
  return `row-${row.kind}-${String(row.item.id)}`
}

/** Every row the list shows, top to bottom; the collapsed week is skipped. */
export function rowsOf(groups: FeedGroups, weekOpen: boolean): Row[] {
  const rows: Row[] = groups.needsLook.map((item) => ({ kind: 'needs', item }))
  for (const occurrence of [
    ...(groups.nextUp ? [groups.nextUp] : []),
    ...groups.today,
    ...groups.tomorrow,
    ...(weekOpen ? groups.thisWeek : []),
  ]) {
    rows.push({ kind: 'occurrence', occurrence })
  }
  rows.push(...groups.anytime.map((item): Row => ({ kind: 'task', item })))
  return rows
}
