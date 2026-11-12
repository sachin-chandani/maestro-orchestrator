// What the components show for an item: its time, and the chips the design puts under its text.
// Pure functions, so screens and tests can use them without rendering.

import type {
  ChipKind,
  ConnectionState,
  IsoDateTime,
  Item,
  ItemGap,
  Occurrence,
} from '../api/types.ts'
import { dateTimeFormat } from '../lib/intl.ts'

export interface ChipData {
  kind: ChipKind
  label: string
}

/** "17:45" in the server's timezone (times are always shown there, ENGINEERING.md). */
export function formatTime(iso: IsoDateTime, timeZone: string): string {
  return dateTimeFormat('en-GB', {
    timeZone,
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(new Date(iso))
}

/** How the design names a channel on a chip ("text me" was the capture, "Text me" the chip). */
const CHANNEL_LABELS: Record<string, string> = {
  twilio_sms: 'Text me',
  twilio_call: 'Call me',
  email: 'Email me',
  telegram: 'Telegram',
  ntfy: 'Push',
  webpush: 'Push',
  desktop: 'Desktop',
  webhook: 'Webhook',
}

export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] ?? channel
}

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

function streakLabel(streak: number, recurrence: string | null): string {
  const unit = recurrence?.startsWith('weekly') ? 'week' : recurrence === 'daily' ? 'day' : null
  if (recurrence?.startsWith('monthly')) return `${String(streak)}-month streak`
  return unit ? `${String(streak)}-${unit} streak` : `${String(streak)} in a row`
}

/** The chips under a reminder, in the design's order: recurrence, channel, urgency, lead, escalation, streak. */
export function itemChips(item: Item): ChipData[] {
  const chips: ChipData[] = []
  if (item.recurrence_label)
    chips.push({ kind: 'recurrence', label: capitalize(item.recurrence_label) })
  for (const channel of item.channels ?? [])
    chips.push({ kind: 'channel', label: channelLabel(channel) })
  if (item.urgent) chips.push({ kind: 'urgent', label: 'Urgent' })
  if (item.lead_time_label) chips.push({ kind: 'lead', label: item.lead_time_label })
  if (item.escalation_label) chips.push({ kind: 'escalation', label: item.escalation_label })
  if (item.streak) chips.push({ kind: 'streak', label: streakLabel(item.streak, item.recurrence) })
  return chips
}

/** The connection pill's words: they, not the colour, say what's wrong. */
export function connectionLabel(state: ConnectionState, serverName: string): string {
  switch (state.status) {
    case 'connected':
      return serverName
    case 'offline':
      return state.queued > 0 ? `Offline · ${String(state.queued)} waiting to send` : 'Offline'
    case 'signed_out':
      return 'Signed out'
    case 'rate_limited':
      return state.retry_after_seconds
        ? `Too many attempts · try in ${String(state.retry_after_seconds)}s`
        : 'Too many attempts'
  }
}

/**
 * What a row in the list is doing, in words (#22): the occurrence's own state, not the item's,
 * so a later occurrence of a snoozed series doesn't claim to be snoozed. Null says nothing.
 */
export function occurrenceState(occurrence: Occurrence, timeZone: string): string | null {
  switch (occurrence.state) {
    case 'awaiting':
      return occurrence.escalation_label
        ? `Sent · waiting for Done or Snooze · ${occurrence.escalation_label}`
        : 'Sent · waiting for Done or Snooze'
    case 'overdue':
      return `Overdue · due ${formatTime(occurrence.at, timeZone)}`
    case 'held':
      return occurrence.held_until
        ? `Held for quiet hours · goes out at ${formatTime(occurrence.held_until, timeZone)}`
        : 'Held for quiet hours'
    case 'snoozed':
      return `Snoozed to ${formatTime(occurrence.at, timeZone)}`
    case 'scheduled':
      return null
  }
}

/** What a gap is called on the chip, and what the answer field asks for (maestro.gaps.WANTED). */
export const GAP_WORDS: Record<ItemGap, { chip: string; ask: string; example: string }> = {
  when: { chip: 'Needs a time', ask: 'When?', example: 'tomorrow at 9am' },
  what: { chip: 'Needs a description', ask: 'What did you mean?', example: 'call the dentist' },
  channel: { chip: 'Needs a channel', ask: 'Which channel?', example: 'text me' },
}
