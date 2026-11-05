import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App from './App.tsx'
import { axeViolations } from './test/axe.ts'

describe('placeholder page', () => {
  it('renders the sample reminders with no backend', () => {
    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: 'Maestro' })).toBeInTheDocument()
    expect(screen.getByText('3 left today')).toBeInTheDocument()
    expect(screen.getByText('Mock data · no server')).toBeInTheDocument()
  })

  it('groups reminders under Today, Tomorrow and Later', () => {
    render(<App />)

    const groups = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(groups).toEqual(['Today', 'Tomorrow', 'Later'])

    const today = screen.getByRole('region', { name: 'Today' })
    expect(within(today).getAllByRole('listitem')).toHaveLength(4)
    expect(
      within(screen.getByRole('region', { name: 'Tomorrow' })).getByText('Dentist'),
    ).toBeVisible()
  })

  it('tells screen readers which reminders are done, not only by strike-through', () => {
    render(<App />)

    const bins = screen.getByText('Put the bins out').closest('li')
    expect(bins).toHaveTextContent('Put the bins out (done)')
    const bank = screen.getByText('Call the bank about the card').closest('li')
    expect(bank).not.toHaveTextContent('(done)')
  })

  it('has no axe violations', async () => {
    const { container } = render(<App />)

    expect(await axeViolations(container)).toEqual([])
  })
})
