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

  // #138: flush() used to drop a capture on any non-offline failure, including the client's own
  // 401/429 lock, which fails a request without ever sending it. Those words were simply lost.
  it.each([
    ['the 401 lock', new ApiError('unauthorized', 'signed out', { status: 401 })],
    ['the 429 lock', new ApiError('rate_limited', 'slow down', { status: 429 })],
    ['a server error', new ApiError('server', 'boom', { status: 500 })],
    ['an error that is not an ApiError', new TypeError('undefined is not a function')],
  ])('keeps everything and stops the pass on %s', async (_name, error) => {
    const outbox = createOutbox()
    outbox.push('one')
    outbox.push('two')
    outbox.push('three')
    const refused = vi.fn()
    let calls = 0
    const sent = await outbox.flush((t) => {
      calls += 1
      return t === 'two' ? Promise.reject(error) : Promise.resolve()
    }, refused)
    expect(sent).toBe(1)
    expect(calls).toBe(2) // stopped at 'two': whatever stopped it stops 'three' as well
    expect(outbox.pending()).toEqual(['two', 'three'])
    expect(refused).not.toHaveBeenCalled()
  })

  it('sends the kept captures on a later flush, still oldest first', async () => {
    const outbox = createOutbox()
    outbox.push('one')
    outbox.push('two')
    let locked = true
    const send = (t: string) =>
      locked && t === 'two'
        ? Promise.reject(new ApiError('unauthorized', 'signed out', { status: 401 }))
        : Promise.resolve()
    expect(await outbox.flush(send)).toBe(1)
    locked = false // the device signed in again
    const sent: string[] = []
    expect(await outbox.flush((t) => send(t).then(() => void sent.push(t)))).toBe(1)
    expect(sent).toEqual(['two'])
    expect(outbox.pending()).toEqual([])
  })

  it('drops only the refused capture and carries on with the rest', async () => {
    const outbox = createOutbox()
    outbox.push('one')
    outbox.push('bad')
    outbox.push('three')
    const refused = vi.fn()
    const sent: string[] = []
    const count = await outbox.flush(
      (t) =>
        t === 'bad'
          ? Promise.reject(new ApiError('invalid', 'no', { status: 422 }))
          : Promise.resolve(void sent.push(t)),
      refused,
    )
    expect(count).toBe(2)
    expect(sent).toEqual(['one', 'three'])
    expect(refused).toHaveBeenCalledTimes(1)
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
