import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ChipKind } from '../api/types.ts'
import { axeViolations } from '../test/axe.ts'
import { Chip } from './Chip.tsx'
import styles from './Chip.module.css'

const KINDS: ChipKind[] = [
  'time',
  'recurrence',
  'channel',
  'urgent',
  'escalation',
  'lead',
  'streak',
  'list',
  'note',
]

describe('Chip', () => {
  it.each(KINDS)('renders a %s chip with its label and style', (kind) => {
    render(<Chip kind={kind} label={`A ${kind} chip`} />)
    const chip = screen.getByText(`A ${kind} chip`)

    expect(chip).toHaveAttribute('data-kind', kind)
    expect(chip.className).toContain(styles[kind])
  })

  it('has a style for every kind', () => {
    for (const kind of KINDS) expect(styles[kind], kind).toBeTruthy()
  })

  it('has no axe violations', async () => {
    const { container } = render(
      <div>
        {KINDS.map((kind) => (
          <Chip key={kind} kind={kind} label={kind} />
        ))}
      </div>,
    )
    expect(await axeViolations(container)).toEqual([])
  })
})
