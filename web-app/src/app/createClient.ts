// Which MaestroClient the app runs on (#101). Screens never know: they get it from ClientProvider.

import type { MaestroClient } from '../api/client.ts'
import { HttpClient } from '../api/http/HttpClient.ts'
import { MockClient } from '../api/mock/MockClient.ts'
import { loadToken } from './session.ts'

export type ClientKind = 'mock' | 'http'

/** Where the dev panel keeps its client choice (development builds only). */
export const DEV_CLIENT_KEY = 'maestro.dev.client'

export interface ClientEnv {
  DEV: boolean
  MODE: string
  VITE_MAESTRO_MOCK?: string
}

/**
 * In order: the dev panel's choice (development only); VITE_MAESTRO_MOCK=1 or 0; `npm run dev:api`
 * (mode "api"), which proxies to a real server; otherwise the server in a production build (#108)
 * and the mock in `npm run dev`, which has no server behind it.
 */
export function chooseClientKind(env: ClientEnv, devChoice: string | null): ClientKind {
  if (env.DEV && (devChoice === 'mock' || devChoice === 'http')) return devChoice
  if (env.VITE_MAESTRO_MOCK === '1') return 'mock'
  if (env.VITE_MAESTRO_MOCK === '0' || env.MODE === 'api') return 'http'
  return env.DEV ? 'mock' : 'http'
}

export function createClient(kind: ClientKind): MaestroClient {
  // Same origin: `maestro serve` serves the app at /app and the API at /, and dev:api proxies.
  // The device's own token, if it has signed in before (ADR 0018); none means a 401 and sign-in.
  return kind === 'mock' ? new MockClient() : new HttpClient({ token: loadToken() })
}

/** The dev panel's saved choice, if storage can be read. */
export function readDevChoice(): string | null {
  try {
    return localStorage.getItem(DEV_CLIENT_KEY)
  } catch {
    return null
  }
}
