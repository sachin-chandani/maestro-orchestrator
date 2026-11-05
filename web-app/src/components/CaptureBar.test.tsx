import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { PreviewChip } from '../api/types.ts'
import { axeViolations } from '../test/axe.ts'
import { CaptureBar } from './CaptureBar.tsx'

function Harness({
  onSubmit,
  preview,
}: {
  onSubmit: (t: string) => void
  preview?: PreviewChip[]
}) {
  const [value, setValue] = useState('')
  return <CaptureBar value={value} onChange={setValue} onSubmit={onSubmit} preview={preview} />
}

const PREVIEW: PreviewChip[] = [
  { kind: 'time', label: 'Tomorrow' },
  { kind: 'time', label: '9:00 am' },
  { kind: 'channel', label: 'Text me' },
]

describe('CaptureBar', () => {
  it('has a labelled field and an Add button', () => {
    render(<Harness onSubmit={vi.fn()} />)

    expect(screen.getByRole('textbox', { name: 'Capture a reminder or note' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add' })).toHaveAttribute('type', 'submit')
    expect(screen.getByRole('form', { name: 'Capture' })).toBeInTheDocument()
  })

  it('submits the trimmed text on Enter', () => {
    const onSubmit = vi.fn()
    render(<Harness onSubmit={onSubmit} />)
    const field = screen.getByRole('textbox')

    fireEvent.change(field, { target: { value: '  call mom at 5pm  ' } })
    fireEvent.submit(field)
    expect(onSubmit).toHaveBeenCalledWith('call mom at 5pm')
  })

  it('submits nothing when the field is blank', () => {
    const onSubmit = vi.fn()
    render(<Harness onSubmit={onSubmit} />)

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('shows what Maestro hears in a polite live region', () => {
    render(<Harness onSubmit={vi.fn()} preview={PREVIEW} />)

    const hears = screen.getByRole('list', { name: 'Maestro hears' })
    expect(hears.parentElement).toHaveAttribute('aria-live', 'polite')
    expect(
      within(hears)
        .getAllByText(/./)
        .map((el) => el.textContent),
    ).toEqual(['Maestro hears:', 'Tomorrow', '9:00 am', 'Text me'])
  })

  it('keeps the live region in the page with no preview, so later chips are announced', () => {
    const { container } = render(<Harness onSubmit={vi.fn()} />)

    expect(screen.queryByRole('list')).toBeNull()
    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument()
  })

  it.each([
    ['empty', undefined],
    ['with a preview', PREVIEW],
  ])('has no axe violations %s', async (_, preview) => {
    const { container } = render(<Harness onSubmit={vi.fn()} preview={preview} />)
    expect(await axeViolations(container)).toEqual([])
  })
})
