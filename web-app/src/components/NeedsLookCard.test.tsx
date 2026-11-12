import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors.ts'
import { axeViolations } from '../test/axe.ts'
import { makeItem } from '../test/items.ts'
import { NeedsLookCard } from './NeedsLookCard.tsx'

const plumber = makeItem({
  id: 13,
  kind: 'task',
  due_at: null,
  text: 'Call the plumber sometime next week',
  source_text: 'book flights and call the plumber sometime next week',
  gap: 'when',
})

function setup(onAnswer = vi.fn(() => Promise.resolve()), item = plumber) {
  const onDismiss = vi.fn()
  render(<NeedsLookCard item={item} onAnswer={onAnswer} onDismiss={onDismiss} />)
  return { onAnswer, onDismiss, card: screen.getByRole('article', { name: item.text }) }
}

describe('NeedsLookCard', () => {
  it('shows the text, what was said, and the gap as a chip', () => {
    const { card } = setup()
    expect(within(card).getByText('Call the plumber sometime next week')).toBeVisible()
    expect(within(card).getByText(/You said “book flights/)).toBeVisible()
    expect(within(card).getByText('Needs a time')).toBeVisible()
    expect(within(card).getByText('When?')).toBeVisible()
  })

  it.each([
    ['what', 'Needs a description', 'What did you mean?'],
    ['channel', 'Needs a channel', 'Which channel?'],
  ] as const)('names a %s gap', (gap, chip, ask) => {
    const { card } = setup(undefined, makeItem({ ...plumber, gap }))
    expect(within(card).getByText(chip)).toBeVisible()
    expect(within(card).getByText(ask)).toBeVisible()
  })

  it('answers with a phrase and reports when the answer did not take', async () => {
    const onAnswer = vi.fn(() =>
      Promise.reject(
        new ApiError('invalid', "Couldn't read a time in that; #13 still needs a time"),
      ),
    )
    const { card } = setup(onAnswer)
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
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

  it('trims the answer and ignores an empty one', async () => {
    const { card, onAnswer } = setup()
    fireEvent.click(within(card).getByRole('button', { name: 'Answer…' }))
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

  it('has no axe violations, closed and open', async () => {
    const { container } = render(
      <NeedsLookCard item={plumber} onAnswer={vi.fn()} onDismiss={vi.fn()} />,
    )
    expect(await axeViolations(container)).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Answer…' }))
    expect(await axeViolations(container)).toEqual([])
  })
})
