import { describe, expect, it } from 'vitest'
import { dateTimeFormat } from './intl.ts'

describe('dateTimeFormat', () => {
  it('hands back the same formatter for the same locale and options', () => {
    const a = dateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit' })
    const b = dateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit' })
    expect(a).toBe(b)
  })

  it('keeps formatters for different options apart', () => {
    const kolkata = dateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit' })
    const london = dateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit' })
    expect(kolkata).not.toBe(london)
    expect(kolkata.resolvedOptions().timeZone).not.toBe(london.resolvedOptions().timeZone)
  })
})
