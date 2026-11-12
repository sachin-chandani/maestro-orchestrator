// Where this device keeps its own token (ADR 0018). Not the server's master token: that is sent
// once to sign in and never stored. localStorage for now; IndexedDB when the service worker (#24)
// needs to read it too.

export const TOKEN_KEY = 'maestro.device_token'

export function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null // storage blocked: every visit signs in again
  }
}

export function saveToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    // storage blocked or full: the token lives for this page load only
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    // nothing to clear if storage is blocked
  }
}
