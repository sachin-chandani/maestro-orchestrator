import { fireEvent, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { isTyping, SHORTCUT_LIST, useShortcuts, type Shortcuts } from './useShortcuts.ts'

function handlers(): Shortcuts {
  return {
    capture: vi.fn(),
    down: vi.fn(),
    up: vi.fn(),
    done: vi.fn(),
    snooze: vi.fn(),
    answer: vi.fn(),
    help: vi.fn(),
    escape: vi.fn(),
  }
}

function Harness({ on, enabled = true }: { on: Shortcuts; enabled?: boolean }) {
  useShortcuts(on, enabled)
  return <input aria-label="field" />
}

describe('useShortcuts', () => {
  it('maps each listed key to its handler', () => {
    const on = handlers()
    render(<Harness on={on} />)
    for (const [key] of SHORTCUT_LIST) fireEvent.keyDown(document.body, { key })
    fireEvent.keyDown(document.body, { key: 'Escape' })
    for (const fn of Object.values(on)) expect(fn).toHaveBeenCalledTimes(1)
  })

  it('is off while typing, except Escape, and with a modifier held', () => {
    const on = handlers()
    const { getByLabelText } = render(<Harness on={on} />)
    fireEvent.keyDown(getByLabelText('field'), { key: 'd' })
    fireEvent.keyDown(getByLabelText('field'), { key: 'Escape' })
    fireEvent.keyDown(document.body, { key: 'd', ctrlKey: true })
    expect(on.done).not.toHaveBeenCalled()
    expect(on.escape).toHaveBeenCalledTimes(1)
  })

  it('can be switched off, and unhooks on unmount', () => {
    const on = handlers()
    const { unmount, rerender } = render(<Harness on={on} enabled={false} />)
    fireEvent.keyDown(document.body, { key: 'd' })
    expect(on.done).not.toHaveBeenCalled()
    rerender(<Harness on={on} />)
    fireEvent.keyDown(document.body, { key: 'd' })
    expect(on.done).toHaveBeenCalledTimes(1)
    unmount()
    fireEvent.keyDown(document.body, { key: 'd' })
    expect(on.done).toHaveBeenCalledTimes(1)
  })

  it('knows what counts as typing', () => {
    expect(isTyping(document.createElement('input'))).toBe(true)
    expect(isTyping(document.createElement('textarea'))).toBe(true)
    expect(isTyping(document.createElement('select'))).toBe(true)
    expect(isTyping(document.createElement('button'))).toBe(false)
    expect(isTyping(null)).toBe(false)
  })
})
