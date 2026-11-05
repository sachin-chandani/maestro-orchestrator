// Sample data for MockClient, from docs/design/mobile/Today.dc.html. Times are placed relative to
// "now", so whatever time the app is opened every state the screens draw is there: overdue and
// awaiting an answer, urgent with escalation, held by quiet hours, snoozed, recurring with a streak,
// tomorrow, later, done, and notes from different sources.

import { addDays, fromWallTime, toIso, wallTime } from './time.ts'
import type { StoredItem } from './store.ts'

const MINUTE = 60_000

/** `now` plus `minutes`, on a whole five minutes so times look like ones a person would choose. */
function soon(now: Date, minutes: number): Date {
  const t = now.getTime() + minutes * MINUTE
  return new Date(t - (t % (5 * MINUTE)))
}

export function seedItems(now: Date, tz: string): StoredItem[] {
  const today = wallTime(now, tz)
  const on = (days: number, hour: number, minute = 0) =>
    fromWallTime({ ...addDays(today, days), hour, minute, second: 0 }, tz)
  const iso = (d: Date) => toIso(d, tz)
  const ago = (minutes: number) => iso(new Date(now.getTime() - minutes * MINUTE))

  const waterAt = soon(now, 180)
  const binsAt = on(6, 8, 30)
  // Tonight at 23:15 falls in the default quiet hours (22:00–07:00); if that has passed, tomorrow's.
  const tonight = on(0, 23, 15) > now ? on(0, 23, 15) : on(1, 23, 15)

  const base = {
    snoozed_until: null,
    recurrence: null,
    channels: null,
    urgent: false,
    delivered: null,
  } satisfies Partial<StoredItem>

  return [
    {
      ...base,
      id: 1,
      text: 'Call the bank about the card',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(soon(now, -40)),
      created_at: ago(26 * 60),
      channels: ['twilio_sms'],
      source_text: 'text me today at noon to call the bank about the card',
      source: 'telegram',
    },
    {
      ...base,
      id: 2,
      text: 'Pick up the kids from practice',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(soon(now, 90)),
      created_at: ago(3 * 24 * 60),
      urgent: true,
      source_text: '!pick up the kids from practice',
      source: 'web',
      escalation_label: 'Call if not done in 15m',
    },
    {
      ...base,
      id: 3,
      text: 'Water the plants',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(waterAt),
      created_at: ago(6 * 7 * 24 * 60),
      recurrence: `weekly:${String(wallTime(waterAt, tz).weekday)}`,
      source_text: 'water the plants every week',
      source: 'web',
      streak: 6,
    },
    {
      ...base,
      id: 4,
      text: 'Renew the car insurance',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(tonight),
      created_at: ago(5 * 60),
      source_text: 'renew the car insurance tonight at 11:15pm',
      source: 'twilio_call',
    },
    {
      ...base,
      id: 5,
      text: 'Stretch for ten minutes',
      kind: 'reminder',
      status: 'pending',
      // A daily series whose next occurrence is tomorrow, with one occurrence snoozed to soon.
      due_at: iso(on(1, 7, 30)),
      snoozed_until: iso(soon(now, 20)),
      created_at: ago(14 * 24 * 60),
      recurrence: 'daily',
      source_text: 'stretch for ten minutes every day at 7:30am',
      source: 'web',
    },
    {
      ...base,
      id: 6,
      text: 'Dentist',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(on(1, 15)),
      created_at: ago(9 * 24 * 60),
      source_text: 'dentist tomorrow at 3pm, remind me 1 hour before',
      source: 'email',
      lead_time_label: '1 hour before',
    },
    {
      ...base,
      id: 7,
      text: 'Send the school trip form',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(on(3, 9)),
      created_at: ago(2 * 24 * 60),
      channels: ['email'],
      source_text: 'email me in 3 days at 9am to send the school trip form',
      source: 'web',
    },
    {
      ...base,
      id: 8,
      text: 'Put the bins out',
      kind: 'reminder',
      status: 'pending',
      due_at: iso(binsAt),
      created_at: ago(30 * 24 * 60),
      recurrence: `weekly:${String(wallTime(binsAt, tz).weekday)}`,
      channels: ['telegram'],
      source_text: 'put the bins out every week at 8:30am on telegram',
      source: 'telegram',
    },
    {
      ...base,
      id: 9,
      text: 'Book the car service',
      kind: 'reminder',
      status: 'done',
      due_at: iso(on(-1, 10)),
      created_at: ago(4 * 24 * 60),
      source_text: 'book the car service yesterday at 10am',
      source: 'web',
    },
    {
      ...base,
      id: 10,
      text: 'Gift idea for Sam: the blue scarf from the Saturday market',
      kind: 'note',
      status: 'pending',
      due_at: null,
      created_at: ago(2 * 24 * 60),
      source_text: 'gift idea for Sam: the blue scarf from the Saturday market',
      source: 'telegram',
    },
    {
      ...base,
      id: 11,
      text: 'The plumber the neighbours liked is called Alex; number is on the fridge',
      kind: 'note',
      status: 'pending',
      due_at: null,
      created_at: ago(7 * 24 * 60),
      source_text: 'the plumber the neighbours liked is called Alex; number is on the fridge',
      source: 'twilio_call',
    },
    {
      ...base,
      id: 12,
      text: 'Car service is due at 60,000 km',
      kind: 'note',
      status: 'pending',
      due_at: null,
      created_at: ago(24 * 24 * 60),
      source_text: 'car service is due at 60,000 km',
      source: 'twilio_sms',
    },
  ]
}
