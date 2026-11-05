import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Item } from '../api/types.ts'
import { axeViolations } from '../test/axe.ts'
import { makeItem, TZ } from '../test/items.ts'
import { ReminderCard } from './ReminderCard.tsx'
import styles from './ReminderCard.module.css'

function renderCard(item: Item, props: Partial<Parameters<typeof ReminderCard>[0]> = {}) {
  const onDone = vi.fn()
  const onSnooze = vi.fn()
  const view = render(
    <ReminderCard item={item} timeZone={TZ} onDone={onDone} onSnooze={onSnooze} {...props} />,
  )
  return { ...view, onDone, onSnooze, card: screen.getByRole('article', { name: item.text }) }
}

describe('ReminderCard', () => {
  it('shows the time in the server timezone, the text and the chips', () => {
    const { card } = renderCard(
      makeItem({ recurrence: 'weekly:3', recurrence_label: 'every Thursday', streak: 6 }),
    )

    expect(within(card).getByText('17:45')).toBeInTheDocument()
    expect(within(card).getByText('Pick up the kids from practice')).toBeInTheDocument()
    const chips = within(card).getByRole('list', { name: 'Details' })
    expect(
      within(chips)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Every Thursday', '6-week streak'])
  })

  it('calls back with the item on Done and Snooze', () => {
    const item = makeItem()
    const { onDone, onSnooze } = renderCard(item)

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    fireEvent.click(screen.getByRole('button', { name: '+1 hour' }))
    expect(onDone).toHaveBeenCalledWith(item)
    expect(onSnooze).toHaveBeenCalledWith(item)
  })

  it('says which reminder each button is for, for screen readers', () => {
    renderCard(makeItem())

    for (const name of ['Done', '+1 hour']) {
      expect(screen.getByRole('button', { name })).toHaveAccessibleDescription(
        'Pick up the kids from practice',
      )
    }
  })

  it('lets the screen choose the snooze wording and the time column', () => {
    renderCard(makeItem(), { snoozeLabel: 'Snooze 10 min', timeLabel: 'Fri' })

    expect(screen.getByRole('button', { name: 'Snooze 10 min' })).toBeInTheDocument()
    expect(screen.getByText('Fri')).toBeInTheDocument()
  })

  it('disables both buttons while busy', () => {
    renderCard(makeItem(), { busy: true })

    expect(screen.getByRole('button', { name: 'Done' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '+1 hour' })).toBeDisabled()
  })

  it.each([
    [{ awaiting_acknowledgement: true }, 'Sent · waiting for Done or Snooze'],
    [{ held_until: '2026-11-06T07:00:00+05:30' }, 'Held for quiet hours · goes out at 07:00'],
    [{ snoozed_until: '2026-11-05T18:45:00+05:30' }, 'Snoozed to 18:45'],
  ] as const)('says in words when it is %o', (overrides, line) => {
    const { card } = renderCard(makeItem(overrides))
    expect(within(card).getByText(line)).toBeInTheDocument()
  })

  it('shows the snoozed time in the time column', () => {
    const { card } = renderCard(makeItem({ snoozed_until: '2026-11-05T18:45:00+05:30' }))
    expect(within(card).getByText('18:45', { selector: 'span' })).toBeInTheDocument()
  })

  it('marks urgent reminders that are not done', () => {
    expect(renderCard(makeItem({ urgent: true })).card.className).toContain(styles.urgent)
  })

  it('shows a done reminder as done, with nothing left to press', () => {
    const { card } = renderCard(makeItem({ status: 'done', urgent: true }))

    expect(within(card).getByText('Done')).toBeInTheDocument()
    expect(within(card).queryByRole('button')).toBeNull()
    expect(card.className).toContain(styles.done)
    expect(card.className).not.toContain(styles.urgent)
  })

  it.each([
    ['pending', makeItem()],
    [
      'with every chip and state',
      makeItem({
        urgent: true,
        channels: ['twilio_sms'],
        lead_time_label: '1 hour before',
        escalation_label: 'Call if not done in 15m',
        awaiting_acknowledgement: true,
      }),
    ],
    ['done', makeItem({ status: 'done' })],
  ])('has no axe violations when %s', async (_, item) => {
    const { container } = renderCard(item)
    expect(await axeViolations(container)).toEqual([])
  })
})
