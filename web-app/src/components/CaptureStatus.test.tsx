import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { axeViolations } from '../test/axe.ts'
import { CaptureStatus } from './CaptureStatus.tsx'

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

function setup(undoId: number | null = 7) {
  const onUndo = vi.fn()
  const onDismiss = vi.fn()
  render(<CaptureStatus message="Added 2." undoId={undoId} onUndo={onUndo} onDismiss={onDismiss} />)
  return { onUndo, onDismiss }
}

describe('CaptureStatus', () => {
  it('announces the message politely and counts the undo window down', () => {
    const { onDismiss } = setup()
    expect(screen.getByRole('status')).toHaveTextContent('Added 2.')
    expect(screen.getByRole('button', { name: 'Undo · 10s' })).toBeEnabled()

    act(() => void vi.advanceTimersByTime(4000))
    expect(screen.getByRole('button', { name: 'Undo · 6s' })).toBeVisible()
    expect(onDismiss).not.toHaveBeenCalled()

    act(() => void vi.advanceTimersByTime(6000))
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('calls back with the undo token', () => {
    const { onUndo } = setup(42)
    fireEvent.click(screen.getByRole('button', { name: /^Undo/ }))
    expect(onUndo).toHaveBeenCalledWith(42)
  })

  it('offers no undo when the capture saved nothing to take back', () => {
    setup(null)
    expect(screen.queryByRole('button')).toBeNull()
    act(() => void vi.advanceTimersByTime(10_000)) // the line still goes on its own
  })

  it('disables the button while the undo is in flight', () => {
    render(
      <CaptureStatus message="Added 1." undoId={1} busy onUndo={vi.fn()} onDismiss={vi.fn()} />,
    )
    expect(screen.getByRole('button', { name: /^Undo/ })).toBeDisabled()
  })

  it('does not restart the countdown when the parent re-renders', () => {
    const { rerender } = render(
      <CaptureStatus message="Added 1." undoId={1} onUndo={vi.fn()} onDismiss={vi.fn()} />,
    )
    act(() => void vi.advanceTimersByTime(5000))
    rerender(
      <CaptureStatus message="Added 1." undoId={1} busy onUndo={vi.fn()} onDismiss={vi.fn()} />,
    )
    expect(screen.getByRole('button', { name: 'Undo · 5s' })).toBeVisible()
  })

  it('has no axe violations', async () => {
    vi.useRealTimers()
    const { container } = render(
      <CaptureStatus message="Added 2." undoId={1} onUndo={vi.fn()} onDismiss={vi.fn()} />,
    )
    expect(await axeViolations(container)).toEqual([])
  })
})
