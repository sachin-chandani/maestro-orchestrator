import { describe, expect, it } from 'vitest'
import { SUPPORT } from './client.ts'
import { ApiError, isApiError } from './errors.ts'
import type { Health, Item } from './types.ts'

// Captured from POST /items and GET /health on maestro 0.3.0 (in-memory store). `satisfies` makes a
// field the backend sends but Item lacks, or one Item requires but the backend doesn't send, a type
// error, so a renamed field shows up here first.
const itemFromBackend = {
  id: 2,
  text: 'Water the plants',
  kind: 'reminder',
  status: 'pending',
  due_at: '2026-11-05T21:00:00+05:30',
  snoozed_until: null,
  created_at: '2026-11-05T13:57:05+05:30',
  recurrence: 'weekly:3',
  recurrence_label: 'every Thursday',
  channels: null,
  urgent: false,
  when: 'Thu 05 Nov 21:00, every Thursday',
  awaiting_acknowledgement: false,
  source_text: 'water the plants every week at 9pm',
} satisfies Item

const healthFromBackend = {
  status: 'ok',
  version: '0.3.0',
  schema: 9,
  timezone: 'Asia/Kolkata',
  backend: 'sqlite',
} satisfies Health

describe('domain types', () => {
  it('match what the backend sends today', () => {
    expect(itemFromBackend.kind).toBe('reminder')
    expect(healthFromBackend.timezone).toBe('Asia/Kolkata')
  })
})

describe('SUPPORT', () => {
  const entries = Object.entries(SUPPORT)

  it('names an issue for everything the backend does not fully serve', () => {
    for (const [method, support] of entries) {
      if (support.level === 'none' || support.level === 'partial') {
        expect(support.issue, method).toBeGreaterThan(0)
      }
    }
  })

  it('lists only routes that exist in maestro/api.py today', () => {
    const routes = new Set([
      'GET /health',
      'GET /items',
      'GET /items?q=',
      'POST /items',
      'GET /items/{id}',
      'POST /items/{id}/done',
      'POST /items/{id}/stop',
      'POST /items/{id}/snooze',
      'DELETE /items/{id}',
      'POST /capture',
      'POST /captures/{id}/undo',
      'GET /feed',
      'POST /needs-look/{id}',
      'DELETE /needs-look/{id}',
      'POST /devices',
      'GET /devices',
      'DELETE /devices/{id}',
    ])
    for (const [method, support] of entries) {
      if (support.level === 'backend' || support.level === 'partial') {
        expect(routes, method).toContain(support.endpoint)
      }
    }
  })

  it('marks the endpoints that are still to be built', () => {
    const unsupported = entries.filter(([, s]) => s.level === 'none').map(([m]) => m)
    expect(unsupported).toEqual([
      'editItem',
      'parse',
      'listLists',
      'createList',
      'addToList',
      'setChecked',
      'clearChecked',
      'pair',
      'getSettings',
      'updateSettings',
      'sendTest',
    ])
  })
})

describe('ApiError', () => {
  it('carries a kind, a readable message and the HTTP details', () => {
    const error = new ApiError('rate_limited', 'Too many requests', {
      status: 429,
      retryAfterSeconds: 30,
    })

    expect(error).toBeInstanceOf(Error)
    expect(error.name).toBe('ApiError')
    expect(error.message).toBe('Too many requests')
    expect(error.status).toBe(429)
    expect(error.retryAfterSeconds).toBe(30)
  })

  it('defaults the HTTP details to null and keeps the cause', () => {
    const cause = new TypeError('Failed to fetch')
    const error = new ApiError('offline', 'Server unreachable', { cause })

    expect(error.status).toBeNull()
    expect(error.retryAfterSeconds).toBeNull()
    expect(error.cause).toBe(cause)
  })

  it('is recognised by isApiError, optionally by kind', () => {
    const error = new ApiError('unsupported', 'Lists need #88')

    expect(isApiError(error)).toBe(true)
    expect(isApiError(error, 'unsupported')).toBe(true)
    expect(isApiError(error, 'offline')).toBe(false)
    expect(isApiError(new Error('plain'))).toBe(false)
    expect(isApiError('unsupported')).toBe(false)
  })
})
