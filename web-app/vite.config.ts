/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { apiProxy } from './dev-proxy.ts'

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  // `maestro serve` will serve the built app at /app (#100); dev uses the same path so links match.
  base: '/app/',
  plugins: [react()],
  build: {
    // Inside the Python package, where `maestro serve` looks for it (maestro/api.py, WEB_APP_DIR).
    outDir: '../maestro/web/app',
    emptyOutDir: true,
  },
  server: {
    // Vite moves to the next free port if 5173 is taken, so it never clashes with another dev server.
    port: 5173,
    // `npm run dev` is mock data only. `npm run dev:api` (mode "api") forwards API calls to MAESTRO_API_URL.
    proxy: mode === 'api' ? apiProxy(process.env.MAESTRO_API_URL) : undefined,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', '*.test.ts'],
  },
}))
