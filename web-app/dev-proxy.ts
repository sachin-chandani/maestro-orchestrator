import type { ProxyOptions } from 'vite'

// The backend routes the app calls (maestro/api.py). `/` is left out: the dev server serves the app.
export const API_PATHS = [
  '/health',
  '/items',
  '/capture',
  '/devices',
  '/needs-look',
  '/captures',
] as const

// Proxy for `npm run dev:api`. There is deliberately no default target, so the app never talks to a
// server nobody chose; point it at a `maestro serve` running on a scratch MAESTRO_DB.
export function apiProxy(target: string | undefined): Record<string, ProxyOptions> {
  if (!target) {
    throw new Error(
      'npm run dev:api needs MAESTRO_API_URL, e.g. MAESTRO_API_URL=http://127.0.0.1:8002 npm run dev:api ' +
        '(a maestro serve started with a scratch MAESTRO_DB; see web-app/README.md)',
    )
  }
  let url: URL
  try {
    url = new URL(target)
  } catch {
    throw new Error(`MAESTRO_API_URL is not a URL: ${target}`)
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`MAESTRO_API_URL must be http:// or https://, got ${url.protocol}`)
  }
  const origin = url.origin
  return Object.fromEntries(API_PATHS.map((path) => [path, { target: origin, changeOrigin: true }]))
}
