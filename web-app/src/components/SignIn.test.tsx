import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors.ts'
import { axeViolations } from '../test/axe.ts'
import { SignIn } from './SignIn.tsx'

function setup(onSignIn = vi.fn(() => Promise.resolve()), reason?: string) {
  render(<SignIn onSignIn={onSignIn} reason={reason} />)
  return {
    onSignIn,
    token: screen.getByLabelText('Server token'),
    name: screen.getByLabelText('Name this device'),
    button: screen.getByRole('button', { name: 'Sign in' }),
  }
}

describe('SignIn', () => {
  it('needs a token before it will try, and names the device when none is given', async () => {
    const { onSignIn, token, button } = setup()
    expect(button).toBeDisabled()
    expect(token).toHaveAttribute('type', 'password')

    fireEvent.change(token, { target: { value: '  t0ken  ' } })
    expect(button).toBeEnabled()
    fireEvent.click(button)

    await waitFor(() => {
      expect(onSignIn).toHaveBeenCalledWith('t0ken', 'This device')
    })
    expect(token).toHaveValue('') // the server token is not kept once it has done its job
  })

  it('passes the device name as typed', async () => {
    const { onSignIn, token, name, button } = setup()
    fireEvent.change(token, { target: { value: 't0ken' } })
    fireEvent.change(name, { target: { value: "Sachin's iPhone" } })
    fireEvent.click(button)
    await waitFor(() => {
      expect(onSignIn).toHaveBeenCalledWith('t0ken', "Sachin's iPhone")
    })
  })

  it.each([
    [new ApiError('unauthorized', 'Invalid API token'), "That isn't the server's token"],
    [
      new ApiError('rate_limited', 'Too many', { retryAfterSeconds: 42 }),
      'Try again in 42 seconds',
    ],
    [new ApiError('offline', 'x'), "Can't reach the server"],
    [new ApiError('server', 'Database is locked'), "Couldn't sign in: Database is locked"],
    [new Error('plain'), "Couldn't sign in."],
  ])('explains a failure (%s) and keeps the token typed', async (error, words) => {
    const { token, button } = setup(vi.fn(() => Promise.reject(error)))
    fireEvent.change(token, { target: { value: 't0ken' } })
    fireEvent.click(button)

    expect(await screen.findByRole('alert')).toHaveTextContent(words)
    expect(token).toHaveValue('t0ken')
    expect(button).toBeEnabled()
  })

  it('says why it is asking, when told', () => {
    setup(undefined, 'This device was signed out.')
    expect(screen.getByText('This device was signed out.')).toBeInTheDocument()
  })

  it('has no axe violations', async () => {
    const { container } = render(<SignIn onSignIn={vi.fn()} reason="This device was signed out." />)
    expect(await axeViolations(container)).toEqual([])
  })
})
