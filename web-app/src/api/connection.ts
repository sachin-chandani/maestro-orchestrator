// The connection state and the 401/429 lock, shared by every MaestroClient (#112). One place, so a
// screen built on MockClient sees exactly what HttpClient does in production.

import type { ApiError } from './errors.ts'
import type { ConnectionState } from './types.ts'

/** What to assume when a 429 carries no Retry-After (maestro/api.py always sends one). */
export const DEFAULT_RETRY_AFTER_SECONDS = 30

const same = (a: ConnectionState, b: ConnectionState) => JSON.stringify(a) === JSON.stringify(b)

/**
 * Tracks what a client's requests say about the connection, and locks the client after a 401 or
 * 429 so polling can't keep sending a missing or wrong token and extend a lockout (as the current
 * web page does). A 401 lock lasts until a token is set; a 429 lock lifts by itself once its
 * Retry-After has passed, so a rate limit never means reloading the page.
 */
export class Connection {
  private state: ConnectionState = { status: 'connected' }
  private readonly listeners = new Set<(state: ConnectionState) => void>()
  private lock: ApiError | null = null
  private lockUntil: number | null = null
  private readonly now: () => number

  constructor(now: () => number = Date.now) {
    this.now = now
  }

  current(): ConnectionState {
    return this.state
  }

  subscribe(listener: (state: ConnectionState) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** The error still locking the client, if any. Checked before each request. */
  locked(): ApiError | null {
    if (this.lock && this.lockUntil !== null && this.now() >= this.lockUntil) {
      this.lock = null
      this.lockUntil = null
    }
    return this.lock
  }

  /** A request failed: lock after 401 and 429, and say what it means for the connection. */
  failed(error: ApiError): void {
    if (error.kind === 'unauthorized') {
      this.lock = error
      this.lockUntil = null
    } else if (error.kind === 'rate_limited') {
      this.lock = error
      const seconds = error.retryAfterSeconds ?? DEFAULT_RETRY_AFTER_SECONDS
      this.lockUntil = this.now() + seconds * 1000
    }
    this.update(stateOf(error))
  }

  /** A request succeeded, so the server is reachable and the token is good. */
  succeeded(): void {
    this.update({ status: 'connected' })
  }

  /** A new token was set: whatever was locking the client no longer applies. */
  unlock(): void {
    this.lock = null
    this.lockUntil = null
  }

  private update(next: ConnectionState | null): void {
    if (!next || same(next, this.state)) return
    this.state = next
    for (const listener of this.listeners) listener(next)
  }
}

/** What an error says about the connection; null when it says nothing (the server answered). */
function stateOf(error: ApiError): ConnectionState | null {
  switch (error.kind) {
    case 'unauthorized':
      return { status: 'signed_out' }
    case 'rate_limited':
      return { status: 'rate_limited', retry_after_seconds: error.retryAfterSeconds }
    case 'offline':
      return { status: 'offline', queued: 0 }
    default:
      return null
  }
}
