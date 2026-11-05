// The one error type every MaestroClient throws, so screens handle failures the same way whether the
// data came from the mock or the server.

export type ApiErrorKind =
  /** No token, or the server rejected it (401). */
  | 'unauthorized'
  /** Too many failed attempts (429); `retryAfterSeconds` says when to try again, if known. */
  | 'rate_limited'
  /** The item, list or device doesn't exist (404). */
  | 'not_found'
  /** The server refused the input, e.g. a snooze of "xx" (422); `message` says why. */
  | 'invalid'
  /** A pairing code that has run out (#95). */
  | 'expired'
  /** The server couldn't be reached at all. */
  | 'offline'
  /** The server answered with an error of its own (5xx) or something unreadable. */
  | 'server'
  /** The backend has no endpoint for this yet; see SUPPORT in client.ts. */
  | 'unsupported'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status: number | null
  readonly retryAfterSeconds: number | null

  constructor(
    kind: ApiErrorKind,
    message: string,
    options: { status?: number | null; retryAfterSeconds?: number | null; cause?: unknown } = {},
  ) {
    super(message, { cause: options.cause })
    this.name = 'ApiError'
    this.kind = kind
    this.status = options.status ?? null
    this.retryAfterSeconds = options.retryAfterSeconds ?? null
  }
}

export function isApiError(error: unknown, kind?: ApiErrorKind): error is ApiError {
  return error instanceof ApiError && (kind === undefined || error.kind === kind)
}
