import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors.ts'
import { createOutbox } from './outbox.ts'

const offline = () => new ApiError('offline', "Can't reach the Maestro server")

describe('outbox', () => {
  it('sends what is pending, oldest first, and empties', async () => {
    const outbox = createOutbox()
    outbox.push('one')
    outbox.push('two')
    const sent: string[] = []
    expect(await outbox.flush((t) => Promise.resolve(void sent.push(t)))).toBe(2)
    expect(sent).toEqual(['one', 'two'])
    expect(outbox.pending()).toEqual([])
  })

  it('stops at the first capture that is still offline and keeps the rest in order', async () => {
    const outbox = createOutbox()
    outbox.push('one')
    outbox.push('two')
    let calls = 0
    const sent = await outbox.flush((t) => {
      calls += 1
      return t === 'two' ? Promise.reject(offline()) : Promise.resolve()
    })
    expect(sent).toBe(1)
    expect(calls).toBe(2)
    expect(outbox.pending()).toEqual(['two'])
  })

  it('drops a refused capture to the reporter instead of retrying it forever', async () => {
    const outbox = createOutbox()
    outbox.push('bad')
    outbox.push('good')
    const refused = vi.fn()
    const sent = await outbox.flush(
      (t) =>
        t === 'bad'
          ? Promise.reject(new ApiError('invalid', 'no', { status: 422 }))
          : Promise.resolve(),
      refused,
    )
    expect(sent).toBe(1)
    expect(refused).toHaveBeenCalledWith('bad', expect.any(ApiError))
    expect(outbox.pending()).toEqual([])
  })

  it('one flush at a time: a second call while flushing does nothing', async () => {
    const outbox = createOutbox()
    outbox.push('one')
    let release: () => void = () => undefined
    const gate = new Promise<void>((r) => (release = r))
    const first = outbox.flush(() => gate)
    const second = await outbox.flush(() => Promise.resolve())
    expect(second).toBe(0)
    release()
    expect(await first).toBe(1)
  })
})
