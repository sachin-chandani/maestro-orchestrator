import { describe, expect, it } from 'vitest'
import { makeItem, TZ } from '../test/items.ts'
import { channelLabel, connectionLabel, formatTime, itemChips } from './format.ts'

describe('formatTime', () => {
  it('shows the time in the server timezone, 24-hour', () => {
    expect(formatTime('2026-11-05T12:15:00Z', TZ)).toBe('17:45')
    expect(formatTime('2026-11-05T12:15:00Z', 'America/New_York')).toBe('07:15')
    expect(formatTime('2026-11-05T18:30:00Z', TZ)).toBe('00:00')
  })
})

describe('itemChips', () => {
  it('has none for a plain reminder', () => {
    expect(itemChips(makeItem())).toEqual([])
  })

  it('lists them in the design order', () => {
    const item = makeItem({
      recurrence: 'weekly:3',
      recurrence_label: 'every Thursday',
      channels: ['twilio_sms', 'telegram'],
      urgent: true,
      lead_time_label: '1 hour before',
      escalation_label: 'Call if not done in 15m',
      streak: 6,
    })
    expect(itemChips(item)).toEqual([
      { kind: 'recurrence', label: 'Every Thursday' },
      { kind: 'channel', label: 'Text me' },
      { kind: 'channel', label: 'Telegram' },
      { kind: 'urgent', label: 'Urgent' },
      { kind: 'lead', label: '1 hour before' },
      { kind: 'escalation', label: 'Call if not done in 15m' },
      { kind: 'streak', label: '6-week streak' },
    ])
  })

  it.each([
    ['daily', '3-day streak'],
    ['monthly:15', '3-month streak'],
    ['interval:60', '3 in a row'],
  ])('names a %s streak', (recurrence, label) => {
    expect(itemChips(makeItem({ recurrence, streak: 3 }))).toEqual([{ kind: 'streak', label }])
  })

  it('treats a missing urgent flag (older items) as not urgent', () => {
    expect(itemChips(makeItem({ urgent: null }))).toEqual([])
  })
})

describe('channelLabel', () => {
  it('uses the design words, and the name for channels it does not know', () => {
    expect(channelLabel('twilio_call')).toBe('Call me')
    expect(channelLabel('email')).toBe('Email me')
    expect(channelLabel('carrier_pigeon')).toBe('carrier_pigeon')
  })
})

describe('connectionLabel', () => {
  it.each([
    [{ status: 'connected' } as const, 'Home server'],
    [{ status: 'offline', queued: 0 } as const, 'Offline'],
    [{ status: 'offline', queued: 2 } as const, 'Offline · 2 waiting to send'],
    [{ status: 'signed_out' } as const, 'Signed out'],
    [
      { status: 'rate_limited', retry_after_seconds: 30 } as const,
      'Too many attempts · try in 30s',
    ],
    [{ status: 'rate_limited', retry_after_seconds: null } as const, 'Too many attempts'],
  ])('says %o in words', (state, label) => {
    expect(connectionLabel(state, 'Home server')).toBe(label)
  })
})
