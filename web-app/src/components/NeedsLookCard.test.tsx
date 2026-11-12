import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors.ts'
import type { GapSuggestions } from '../api/types.ts'
import { axeViolations } from '../test/axe.ts'
import { makeItem, TZ } from '../test/items.ts'
import { NeedsLookCard } from './NeedsLookCard.tsx'

const plumber = makeItem({
  id: 13,
  kind: 'task',
  due_at: null,
  text: 'Call the plumber sometime next week',
  source_text: 'book flights and call the plumber sometime next week',
  gap: 'when',
})

const SUGGESTED: GapSuggestions = {
  said: 'sometime next week',
  suggestions: [
    { at: '2026-11-09T09:00:00+05:30', reason: 'start of the week' },
    { at: '2026-11-09T18:00:00+05:30', reason: 'start of the week, evening' },
    { at: '2026-11-14T10:00:00+05:30', reason: 'the weekend' },
  ],
}

function setup({
  onAnswer = vi.fn(() => Promise.resolve()),
  loadSuggestions = vi.fn(() => Promise.resolve(SUGGESTED)),
  item = plumber,
} = {}) {
  const onDismiss = vi.fn()
  const onKeep = vi.fn()
  render(
    <NeedsLookCard
      item={item}
      timeZone={TZ}
      onAnswer={onAnswer}
      loadSuggestions={loadSuggestions}
      onKeep={onKeep}
      onDismiss={onDismiss}
    />,
  )
  return {
    onAnswer,
    onDismiss,
    onKeep,
    loadSuggestions,
    card: screen.getByRole('article', { name: item.text }),
  }
}

/** The first match, or a clear failure instead of a type cast. */
function first<T>(items: T[]): T {
  const [item] = items
  if (item === undefined) throw new Error('expected at least one match')
  return item
}

describe('NeedsLookCard', () => {
  it('shows the text, what was said, and the gap as a chip', () => {
    const { card } = setup()
    expect(within(card).getByText('Call the plumber sometime next week')).toBeVisible()
    expect(within(card).getByText(/You said “book flights/)).toBeVisible()
    expect(within(card).getByText('Needs a time')).toBeVisible()
    expect(within(card).getByText('When?')).toBeVisible()
  })

  it('offers the three suggested times with their reasons, and one tap answers (#128)', async () => {
    const { card, onAnswer } = setup()
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
    const list = await within(card).findByRole('list', { name: 'Suggested times' })
    const buttons = within(list).getAllByRole('button')
    expect(buttons.map((b) => b.textContent)).toEqual([
      'Mon 9 09:00start of the week',
      'Mon 9 18:00start of the week, evening',
      'Sat 14 10:00the weekend',
    ])
    expect(within(card).getByText('You said “sometime next week”')).toBeVisible()

    fireEvent.click(first(buttons))
    await waitFor(() => {
      expect(onAnswer).toHaveBeenCalledWith(plumber, '2026-11-09T09:00:00+05:30')
    })
  })

  it('keeps it under Anytime with one tap', async () => {
    const { card, onKeep } = setup()
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
    fireEvent.click(await within(card).findByRole('button', { name: 'No time · keep it' }))
    expect(onKeep).toHaveBeenCalledWith(plumber)
  })

  it('switches to a typed time behind Pick a time…, validating through the server', async () => {
    const onAnswer = vi.fn(() =>
      Promise.reject(
        new ApiError('invalid', "Couldn't read a time in that; #13 still needs a time"),
      ),
    )
    const { card } = setup({ onAnswer })
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
    fireEvent.click(await within(card).findByRole('button', { name: 'Pick a time…' }))
    const field = within(card).getByLabelText('When?')
    expect(field).toHaveFocus()
    expect(field).toHaveAttribute('placeholder', 'tomorrow at 9am')

    fireEvent.change(field, { target: { value: 'dunno' } })
    fireEvent.submit(field)
    expect(await within(card).findByRole('alert')).toHaveTextContent('still needs a time')
    expect(onAnswer).toHaveBeenCalledWith(plumber, 'dunno')
    expect(field).toHaveValue('dunno')
    expect(field).toHaveAttribute('aria-invalid', 'true')

    fireEvent.click(within(card).getByRole('button', { name: 'Cancel' }))
    expect(within(card).queryByLabelText('When?')).toBeNull()
    expect(within(card).getByRole('button', { name: 'Answer…' })).toBeVisible()
  })

  it('goes straight to the field for a gap with no times to suggest', async () => {
    const what = makeItem({ ...plumber, gap: 'what' })
    const { card } = setup({
      item: what,
      loadSuggestions: vi.fn(() => Promise.resolve({ said: what.text, suggestions: [] })),
    })
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
    expect(await within(card).findByLabelText('What did you mean?')).toHaveFocus()
    expect(within(card).queryByRole('list', { name: 'Suggested times' })).toBeNull()
    expect(within(card).queryByRole('button', { name: 'No time · keep it' })).toBeNull()
  })

  it('falls back to the field when suggestions cannot be fetched', async () => {
    const { card } = setup({ loadSuggestions: vi.fn(() => Promise.reject(new Error('down'))) })
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
    expect(await within(card).findByLabelText('When?')).toHaveFocus()
  })

  it('trims the typed answer and ignores an empty one', async () => {
    const { card, onAnswer } = setup()
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
    fireEvent.click(await within(card).findByRole('button', { name: 'Pick a time…' }))
    const field = within(card).getByLabelText('When?')
    fireEvent.submit(field)
    expect(onAnswer).not.toHaveBeenCalled()
    fireEvent.change(field, { target: { value: '  tomorrow 9am  ' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(onAnswer).toHaveBeenCalledWith(plumber, 'tomorrow 9am')
    })
  })

  it('dismisses, and says which item each button is for', () => {
    const { card, onDismiss } = setup()
    fireEvent.click(within(card).getByRole('button', { name: 'Dismiss' }))
    expect(onDismiss).toHaveBeenCalledWith(plumber)
    expect(within(card).getByRole('button', { name: 'Answer…' })).toHaveAccessibleDescription(
      'Call the plumber sometime next week',
    )
  })

  it('has no axe violations, closed and with the sheet open', async () => {
    const { container } = render(
      <NeedsLookCard
        item={plumber}
        timeZone={TZ}
        onAnswer={vi.fn()}
        loadSuggestions={vi.fn(() => Promise.resolve(SUGGESTED))}
        onKeep={vi.fn()}
        onDismiss={vi.fn()}
      />,
    )
    expect(await axeViolations(container)).toEqual([])
    fireEvent.click(first(screen.getAllByRole('button', { name: 'Answer…' })))
    await screen.findAllByRole('list', { name: 'Suggested times' })
    expect(await axeViolations(container)).toEqual([])
  })
})
