import { describe, expect, it } from 'vitest'
import { API_PATHS, apiProxy } from './dev-proxy.ts'

describe('apiProxy', () => {
  it('forwards each API path to the chosen server', () => {
    const proxy = apiProxy('http://127.0.0.1:8002')

    expect(Object.keys(proxy)).toEqual([...API_PATHS])
    for (const path of API_PATHS) {
      expect(proxy[path]).toEqual({ target: 'http://127.0.0.1:8002', changeOrigin: true })
    }
  })

  it('never proxies / , which belongs to the app', () => {
    expect(apiProxy('http://127.0.0.1:8002')).not.toHaveProperty('/')
  })

  it('uses only the origin of the URL', () => {
    expect(apiProxy('https://maestro.example:9000/some/path')['/items']?.target).toBe(
      'https://maestro.example:9000',
    )
  })

  it('has no default target', () => {
    expect(() => apiProxy(undefined)).toThrow(/needs MAESTRO_API_URL/)
    expect(() => apiProxy('')).toThrow(/needs MAESTRO_API_URL/)
  })

  it('rejects values that are not http(s) URLs', () => {
    expect(() => apiProxy('8002')).toThrow(/not a URL/)
    expect(() => apiProxy('ftp://127.0.0.1:8002')).toThrow(/http/)
  })
})
