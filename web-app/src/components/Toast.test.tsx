import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { axeViolations } from '../test/axe.ts'
import { Toast } from './Toast.tsx'

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('Toast', () => {
  it('shows the message in a status region', () => {
    render(<Toast message="Snoozed to 18:45." onDismiss={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent('Snoozed to 18:45.')
  })

  it('keeps an empty status region in the page with no message', () => {
    render(<Toast message={null} onDismiss={vi.fn()} />)
    expect(screen.getByRole('status')).toBeEmptyDOMElement()
  })

  it('dismisses itself after 2.6 seconds', () => {
    const onDismiss = vi.fn()
    render(<Toast message="Saved as a note." onDismiss={onDismiss} />)

    act(() => void vi.advanceTimersByTime(2599))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => void vi.advanceTimersByTime(1))
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('does not restart its timer when the parent re-renders with a new callback', () => {
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = render(<Toast message="Saved." onDismiss={first} />)

    act(() => void vi.advanceTimersByTime(2000))
    rerender(<Toast message="Saved." onDismiss={second} />)
    act(() => void vi.advanceTimersByTime(600))
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledOnce()
  })

  it('starts again for a new message', () => {
    const onDismiss = vi.fn()
    const { rerender } = render(<Toast message="One." onDismiss={onDismiss} durationMs={1000} />)

    act(() => void vi.advanceTimersByTime(900))
    rerender(<Toast message="Two." onDismiss={onDismiss} durationMs={1000} />)
    act(() => void vi.advanceTimersByTime(900))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => void vi.advanceTimersByTime(100))
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('starts again for the same words said again, told apart by messageKey (#112)', () => {
    const onDismiss = vi.fn()
    const { rerender } = render(
      <Toast message="Done." messageKey={1} onDismiss={onDismiss} durationMs={1000} />,
    )

    act(() => void vi.advanceTimersByTime(900))
    rerender(<Toast message="Done." messageKey={2} onDismiss={onDismiss} durationMs={1000} />)
    act(() => void vi.advanceTimersByTime(900))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => void vi.advanceTimersByTime(100))
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('has no axe violations', async () => {
    vi.useRealTimers()
    const { container } = render(<Toast message="Done." onDismiss={vi.fn()} />)
    expect(await axeViolations(container)).toEqual([])
  })
})
