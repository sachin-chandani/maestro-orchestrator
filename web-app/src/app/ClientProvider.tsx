import { useMemo, type ReactNode } from 'react'
import type { MaestroClient } from '../api/client.ts'
import { ClientContext } from './clientContext.ts'
import type { ClientKind } from './createClient.ts'

export interface ClientProviderProps {
  client: MaestroClient
  kind: ClientKind
  children: ReactNode
}

/** Gives everything below it one MaestroClient; tests pass their own. */
export function ClientProvider({ client, kind, children }: ClientProviderProps) {
  const value = useMemo(() => ({ client, kind }), [client, kind])
  return <ClientContext value={value}>{children}</ClientContext>
}
