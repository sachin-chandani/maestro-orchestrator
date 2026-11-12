// Captures made while the server can't be reached (#104): kept on this phone and sent when it can
// be. In memory only for now — a reload loses them, and the line's wording owns that — until the
// service worker (#24) gives them somewhere durable to live.

export interface Outbox {
  /** Keep a capture to send later. */
  push(text: string): void
  /** The captures waiting, oldest first, for the connection pill's count. */
  pending(): readonly string[]
  /**
   * Send everything waiting, oldest first, with `send`. A capture that fails as offline again goes
   * back to the front and flushing stops; any other failure drops the capture to the reporter (it
   * was refused, not unsent). Returns how many were sent.
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
            if (isOffline(error)) return sent // still unreachable: keep the rest for next time
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

function isOffline(error: unknown): boolean {
  return (
    typeof error === 'object' && error !== null && (error as { kind?: unknown }).kind === 'offline'
  )
}
