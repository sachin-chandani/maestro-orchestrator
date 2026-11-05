import { createContext, useContext, useSyncExternalStore } from 'react'
import type { MaestroClient } from '../api/client.ts'
import type { ConnectionState } from '../api/types.ts'
import type { ClientKind } from './createClient.ts'

export interface ClientValue {
  client: MaestroClient
  kind: ClientKind
}

export const ClientContext = createContext<ClientValue | null>(null)

/** The app's MaestroClient. Screens use this and nothing else to reach Maestro (ADR 0014). */
export function useClient(): ClientValue {
  const value = useContext(ClientContext)
  if (!value) throw new Error('useClient() needs a <ClientProvider> above it')
  return value
}

/** The client's connection state, re-rendering when it changes. */
export function useConnection(): ConnectionState {
  const { client } = useClient()
  return useSyncExternalStore(
    (onChange) => client.onConnectionChange(onChange),
    () => client.connection(),
  )
}
