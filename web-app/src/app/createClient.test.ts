import { describe, expect, it } from 'vitest'
import { HttpClient } from '../api/http/HttpClient.ts'
import { MockClient } from '../api/mock/MockClient.ts'
import { chooseClientKind, createClient } from './createClient.ts'

const dev = { DEV: true, MODE: 'development' }
const prod = { DEV: false, MODE: 'production' }

describe('chooseClientKind', () => {
  it('uses the mock for npm run dev and, until #108, for production builds', () => {
    expect(chooseClientKind(dev, null)).toBe('mock')
    expect(chooseClientKind(prod, null)).toBe('mock')
  })

  it('uses the server for npm run dev:api', () => {
    expect(chooseClientKind({ DEV: true, MODE: 'api' }, null)).toBe('http')
  })

  it('follows VITE_MAESTRO_MOCK', () => {
    expect(chooseClientKind({ ...prod, VITE_MAESTRO_MOCK: '0' }, null)).toBe('http')
    expect(chooseClientKind({ DEV: true, MODE: 'api', VITE_MAESTRO_MOCK: '1' }, null)).toBe('mock')
  })

  it('lets the dev panel choose, in development only', () => {
    expect(chooseClientKind(dev, 'http')).toBe('http')
    expect(chooseClientKind({ DEV: true, MODE: 'api' }, 'mock')).toBe('mock')
    expect(chooseClientKind(prod, 'http')).toBe('mock') // a stale choice never reaches production
    expect(chooseClientKind(dev, 'nonsense')).toBe('mock')
  })
})

describe('createClient', () => {
  it('makes the chosen client', () => {
    expect(createClient('mock')).toBeInstanceOf(MockClient)
    expect(createClient('http')).toBeInstanceOf(HttpClient)
  })
})
