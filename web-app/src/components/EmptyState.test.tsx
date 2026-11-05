import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { axeViolations } from '../test/axe.ts'
import { EmptyState } from './EmptyState.tsx'

describe('EmptyState', () => {
  it('shows the title and the hint that teaches the capture phrase', () => {
    render(
      <EmptyState title="No lists yet" hint="Try “add oat milk to the shopping list” below." />,
    )

    expect(screen.getByText('No lists yet')).toBeInTheDocument()
    expect(screen.getByText('Try “add oat milk to the shopping list” below.')).toBeInTheDocument()
  })

  it('works without a hint', () => {
    const { container } = render(<EmptyState title="Nothing left today" />)
    expect(container.querySelectorAll('p')).toHaveLength(1)
  })

  it('has no axe violations', async () => {
    const { container } = render(<EmptyState title="No notes" hint="Say “note: …” to add one." />)
    expect(await axeViolations(container)).toEqual([])
  })
})
