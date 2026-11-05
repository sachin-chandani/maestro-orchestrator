import type { Item } from '../api/types.ts'

export const TZ = 'Asia/Kolkata'

/** A pending one-off reminder at 17:45 in TZ, with `overrides` on top. */
export function makeItem(overrides: Partial<Item> = {}): Item {
  return {
    id: 1,
    text: 'Pick up the kids from practice',
    kind: 'reminder',
    status: 'pending',
    due_at: '2026-11-05T17:45:00+05:30',
    snoozed_until: null,
    created_at: '2026-11-02T09:00:00+05:30',
    recurrence: null,
    recurrence_label: null,
    channels: null,
    urgent: false,
    when: 'Thu 05 Nov 17:45',
    awaiting_acknowledgement: false,
    source_text: 'pick up the kids from practice at 5:45pm',
    ...overrides,
  }
}
