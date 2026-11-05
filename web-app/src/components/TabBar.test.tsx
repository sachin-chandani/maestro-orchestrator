import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { axeViolations } from '../test/axe.ts'
import { TabBar } from './TabBar.tsx'

const TABS = [
  { id: 'today', label: 'Today' },
  { id: 'lists', label: 'Lists' },
  { id: 'notes', label: 'Notes' },
] as const

describe('TabBar', () => {
  it('is the Sections navigation, marking only the current tab', () => {
    render(<TabBar tabs={TABS} current="lists" onSelect={vi.fn()} />)

    expect(screen.getByRole('navigation', { name: 'Sections' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Lists' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Today' })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('button', { name: 'Notes' })).not.toHaveAttribute('aria-current')
  })

  it('reports the tab chosen', () => {
    const onSelect = vi.fn()
    render(<TabBar tabs={TABS} current="today" onSelect={onSelect} />)

    fireEvent.click(screen.getByRole('button', { name: 'Notes' }))
    expect(onSelect).toHaveBeenCalledWith('notes')
  })

  it('works with any number of tabs (Lists is hidden until #88)', () => {
    render(<TabBar tabs={[TABS[0], TABS[2]]} current="today" onSelect={vi.fn()} />)
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['Today', 'Notes'])
  })

  it('has no axe violations', async () => {
    const { container } = render(<TabBar tabs={TABS} current="today" onSelect={vi.fn()} />)
    expect(await axeViolations(container)).toEqual([])
  })
})
