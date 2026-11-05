import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ConnectionState } from '../api/types.ts'
import { axeViolations } from '../test/axe.ts'
import { ConnectionPill } from './ConnectionPill.tsx'
import styles from './ConnectionPill.module.css'

const STATES: [ConnectionState, string][] = [
  [{ status: 'connected' }, 'Home server'],
  [{ status: 'offline', queued: 1 }, 'Offline · 1 waiting to send'],
  [{ status: 'signed_out' }, 'Signed out'],
  [{ status: 'rate_limited', retry_after_seconds: 30 }, 'Too many attempts · try in 30s'],
]

describe('ConnectionPill', () => {
  it.each(STATES)('says %o in a status region', (state, label) => {
    render(<ConnectionPill state={state} serverName="Home server" />)
    const pill = screen.getByRole('status')

    expect(pill).toHaveTextContent(label)
    expect(pill.className).toContain(styles[state.status])
  })

  it('says "Connected" when the server has no name', () => {
    render(<ConnectionPill state={{ status: 'connected' }} />)
    expect(screen.getByRole('status')).toHaveTextContent('Connected')
  })

  it.each(STATES)('has no axe violations when %o', async (state) => {
    const { container } = render(<ConnectionPill state={state} />)
    expect(await axeViolations(container)).toEqual([])
  })
})
