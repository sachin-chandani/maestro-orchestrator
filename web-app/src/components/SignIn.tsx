import { useId, useState, type SyntheticEvent } from 'react'
import { isApiError } from '../api/errors.ts'
import styles from './SignIn.module.css'

export interface SignInProps {
  /**
   * Sign this device in with the server's master token. Rejects with an ApiError; the form
   * explains it and keeps what was typed.
   */
  onSignIn: (masterToken: string, deviceName: string) => Promise<void>
  /** Why sign-in is being asked for, when it isn't the first time: "This device was signed out." */
  reason?: string
}

/** The device name when none is given: good enough to tell phones apart in `maestro devices list`. */
const DEFAULT_NAME = 'This device'

function explain(error: unknown): string {
  if (!isApiError(error)) return "Couldn't sign in."
  switch (error.kind) {
    case 'unauthorized':
      return "That isn't the server's token. It's MAESTRO_API_TOKEN on the server."
    case 'rate_limited':
      return error.retryAfterSeconds
        ? `Too many attempts. Try again in ${String(error.retryAfterSeconds)} seconds.`
        : 'Too many attempts. Try again in a moment.'
    case 'offline':
      return "Can't reach the server. Check the connection and try again."
    default:
      return `Couldn't sign in: ${error.message}`
  }
}

/**
 * Sign-in (ADR 0018): the server's token is sent once, this device gets a token of its own, and
 * the server token is never stored here. Shown in place of the list while the app is signed out.
 */
export function SignIn({ onSignIn, reason }: SignInProps) {
  const tokenId = useId()
  const nameId = useId()
  const [token, setToken] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: SyntheticEvent) => {
    event.preventDefault()
    const master = token.trim()
    if (!master || busy) return
    setBusy(true)
    setError(null)
    try {
      await onSignIn(master, name.trim() || DEFAULT_NAME)
      setToken('') // the master token is never kept, not even in the form
    } catch (cause) {
      setError(explain(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className={styles.panel}
      onSubmit={(e) => void submit(e)}
      aria-labelledby={`${tokenId}-title`}
    >
      <h2 className={styles.title} id={`${tokenId}-title`}>
        Sign in to your server
      </h2>
      {reason && <p className={styles.reason}>{reason}</p>}
      <p className={styles.hint}>
        Paste the server's token once. This device gets a token of its own, which you can revoke any
        time with <code>maestro devices revoke</code>. The server's token is not stored here.
      </p>

      <label className={styles.label} htmlFor={tokenId}>
        Server token
      </label>
      <input
        id={tokenId}
        className={styles.input}
        type="password"
        value={token}
        onChange={(e) => {
          setToken(e.target.value)
        }}
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        required
      />

      <label className={styles.label} htmlFor={nameId}>
        Name this device
      </label>
      <input
        id={nameId}
        className={styles.input}
        type="text"
        value={name}
        onChange={(e) => {
          setName(e.target.value)
        }}
        placeholder={DEFAULT_NAME}
        maxLength={80}
        autoComplete="off"
      />

      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      <button type="submit" className={styles.submit} disabled={busy || !token.trim()}>
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  )
}
