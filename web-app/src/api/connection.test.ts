import { describe, expect, it, vi } from 'vitest'
import { Connection, DEFAULT_RETRY_AFTER_SECONDS } from './connection.ts'
import { ApiError } from './errors.ts'

const unauthorized = () => new ApiError('unauthorized', 'Invalid API token', { status: 401 })
const rateLimited = (retryAfterSeconds: number | null) =>
  new ApiError('rate_limited', 'Too many requests', { status: 429, retryAfterSeconds })

describe('Connection', () => {
  it('starts connected and unlocked', () => {
    const conn = new Connection()
    expect(conn.current()).toEqual({ status: 'connected' })
    expect(conn.locked()).toBeNull()
  })

  it('locks on a 401 until unlocked', () => {
    let now = 0
    const conn = new Connection(() => now)
    const error = unauthorized()
    conn.failed(error)

    expect(conn.current()).toEqual({ status: 'signed_out' })
    now += 24 * 3_600_000
    expect(conn.locked()).toBe(error) // time alone never lifts it
    conn.unlock()
    expect(conn.locked()).toBeNull()
  })

  it('locks on a 429 for exactly Retry-After', () => {
    let now = 0
    const conn = new Connection(() => now)
    conn.failed(rateLimited(30))

    expect(conn.current()).toEqual({ status: 'rate_limited', retry_after_seconds: 30 })
    now = 29_999
    expect(conn.locked()?.kind).toBe('rate_limited')
    now = 30_000
    expect(conn.locked()).toBeNull()
  })

  it('assumes a default when a 429 names no Retry-After', () => {
    let now = 0
    const conn = new Connection(() => now)
    conn.failed(rateLimited(null))

    now = DEFAULT_RETRY_AFTER_SECONDS * 1000 - 1
    expect(conn.locked()).not.toBeNull()
    now += 1
    expect(conn.locked()).toBeNull()
  })

  it('does not lock on other failures, and only offline changes the state', () => {
    const conn = new Connection()
    const seen: unknown[] = []
    conn.subscribe((s) => seen.push(s))

    conn.failed(new ApiError('not_found', 'No item #7', { status: 404 }))
    conn.failed(new ApiError('server', 'Database is locked', { status: 500 }))
    expect(conn.locked()).toBeNull()
    expect(seen).toEqual([])

    conn.failed(new ApiError('offline', "Can't reach the Maestro server"))
    conn.succeeded()
    expect(seen).toEqual([{ status: 'offline', queued: 0 }, { status: 'connected' }])
  })

  it('tells listeners once per change and lets them unsubscribe', () => {
    const conn = new Connection()
    const listener = vi.fn()
    const unsubscribe = conn.subscribe(listener)

    conn.succeeded() // already connected: nothing to say
    conn.failed(new ApiError('offline', 'x'))
    conn.failed(new ApiError('offline', 'x')) // same state again: nothing to say
    expect(listener).toHaveBeenCalledTimes(1)

    unsubscribe()
    conn.succeeded()
    expect(listener).toHaveBeenCalledTimes(1)
  })
})
