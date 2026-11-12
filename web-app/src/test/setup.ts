import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Testing Library only cleans up on its own when test globals are enabled; they aren't here.
afterEach(cleanup)

// Node 22.4+ ships an experimental `localStorage` global that, without --localstorage-file, has no
// working methods and shadows jsdom's. Tests need a real one (the device token lives there), so
// put a plain in-memory Storage in its place when what's there is unusable.
function memoryStorage(): Storage {
  const data = new Map<string, string>()
  return {
    get length() {
      return data.size
    },
    clear: () => {
      data.clear()
    },
    getItem: (key) => data.get(key) ?? null,
    key: (index) => [...data.keys()][index] ?? null,
    removeItem: (key) => {
      data.delete(key)
    },
    setItem: (key, value) => {
      data.set(key, value)
    },
  }
}

const storage = (globalThis as { localStorage?: Partial<Storage> }).localStorage
if (typeof storage?.removeItem !== 'function' || typeof storage.getItem !== 'function') {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
}
