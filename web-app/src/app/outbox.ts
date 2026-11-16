// Captures made while the server can't be reached (#104): kept on this phone and sent when it can
// be. In memory only for now — a reload loses them, and the line's wording owns that — until the
// service worker (#24) gives them somewhere durable to live.

import { type ApiErrorKind, isApiError } from '../api/errors.ts'

export interface Outbox {
  /** Keep a capture to send later. */
  push(text: string): void
  /** The captures waiting, oldest first, for the connection pill's count. */
  pending(): readonly string[]
  /**
   * Send everything waiting, oldest first, with `send`. A capture the server refused (it would
   * refuse the same words forever) is dropped to the reporter; anything else — offline, the
   * client's own 401 or 429 lock, a 5xx, an error we don't recognise — keeps the capture at the
   * front and stops the pass, because whatever stopped this one stops the rest of the queue too
   * (#138). Returns how many were sent.
   */
  flush(
    send: (text: string) => Promise<void>,
    refused?: (text: string, error: unknown) => void,
  ): Promise<number>
}

export function createOutbox(): Outbox {
  let queue: string[] = []
  let flushing = false
  return {
    push(text) {
      queue.push(text)
    },
    pending() {
      return queue
    },
    async flush(send, refused) {
      if (flushing) return 0 // a reconnect and a manual capture can both ask; one pass is enough
      flushing = true
      let sent = 0
      try {
        while (queue.length > 0) {
          const text = queue[0]
          if (text === undefined) break
          try {
            await send(text)
            queue = queue.slice(1)
            sent += 1
          } catch (error) {
            if (!isRefusal(error)) return sent // unsent: keep this one and the rest for next time
            queue = queue.slice(1) // refused by the server: holding it would refuse it forever
            refused?.(text, error)
          }
        }
        return sent
      } finally {
        flushing = false
      }
    },
  }
}

/**
 * The failures that mean these words will never be accepted: the server (or the client's own
 * routing table) read the request and said no. Everything else — `offline`, `unauthorized` and
 * `rate_limited` (the lock in api/connection.ts fails these without even sending), `server`, and
 * anything that isn't an ApiError at all — may yet succeed unchanged, so the capture is kept.
 * A drop list rather than a keep list: a kind added later is kept, not silently lost.
 */
const REFUSALS: ReadonlySet<ApiErrorKind> = new Set<ApiErrorKind>([
  'invalid',
  'conflict',
  'not_found',
  'expired',
  'unsupported',
])

function isRefusal(error: unknown): boolean {
  return isApiError(error) && REFUSALS.has(error.kind)
}
