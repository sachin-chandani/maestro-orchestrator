// MockClient's saved state: what it keeps per item, and how that survives a reload.

import type { Item } from '../types.ts'

/** The fields of Item the mock derives on every read rather than storing. */
type Derived = 'when' | 'awaiting_acknowledgement' | 'recurrence_label' | 'held_until'

/** One item as MockClient keeps it. */
export type StoredItem = Omit<Item, Derived> & {
  /** The latest occurrence sent out, and whether someone has said done or snooze to it since. */
  delivered: { occurrence: string; sent_at: string; acknowledged: boolean } | null
}

export interface MockState {
  items: StoredItem[]
  nextId: number
}

/** Bump when MockState changes shape; saved state from another version is replaced by the seed. */
export const STORAGE_VERSION = 1
export const STORAGE_KEY = `maestro.mock.v${String(STORAGE_VERSION)}`

/** The part of the Web Storage API the mock uses, so tests can pass their own. */
export type KeyValueStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

interface Saved {
  version: number
  state: MockState
}

function isState(value: unknown): value is MockState {
  if (typeof value !== 'object' || value === null) return false
  const s = value as Partial<MockState>
  return Array.isArray(s.items) && typeof s.nextId === 'number'
}

/** The saved state, or null when there is none, it is from another version or it can't be read. */
export function load(storage: KeyValueStorage | null): MockState | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (raw === null) return null
    const saved = JSON.parse(raw) as Partial<Saved>
    return saved.version === STORAGE_VERSION && isState(saved.state) ? saved.state : null
  } catch {
    return null // unreadable (or storage blocked): start again from the seed
  }
}

/** Saves `state`. Returns false if storage refused (full, or blocked by the browser). */
export function save(storage: KeyValueStorage | null, state: MockState): boolean {
  if (!storage) return false
  try {
    storage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: STORAGE_VERSION, state } satisfies Saved),
    )
    return true
  } catch {
    return false
  }
}

export function clear(storage: KeyValueStorage | null): void {
  try {
    storage?.removeItem(STORAGE_KEY)
  } catch {
    // blocked storage: nothing was saved either
  }
}

/** The browser's localStorage if it can be used here, else null (private modes can block it). */
export function defaultStorage(): KeyValueStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}
