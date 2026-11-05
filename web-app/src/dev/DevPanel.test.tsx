import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isApiError } from '../api/errors.ts'
import { HttpClient } from '../api/http/HttpClient.ts'
import { MockClient } from '../api/mock/MockClient.ts'
import { ClientProvider } from '../app/ClientProvider.tsx'
import { axeViolations } from '../test/axe.ts'
import DevPanel from './DevPanel.tsx'

function setup(client = new MockClient({ storage: null, timeZone: 'UTC' })) {
  const reload = vi.fn()
  const switchTo = vi.fn()
  const view = render(
    <ClientProvider client={client} kind={client instanceof MockClient ? 'mock' : 'http'}>
      <DevPanel reload={reload} switchTo={switchTo} />
    </ClientProvider>,
  )
  fireEvent.click(screen.getByText('Dev tools')) // open the <details>
  return { ...view, client, reload, switchTo }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('DevPanel', () => {
  it('resets the sample data and reloads the screen', async () => {
    const { client, reload } = setup()
    await client.deleteItem(1)

    fireEvent.click(screen.getByRole('button', { name: 'Reset sample data' }))
    expect((await client.getItem(1)).text).toBe('Call the bank about the card')
    expect(reload).toHaveBeenCalledOnce()
  })

  it('reloads the screen and empties the sample data', async () => {
    const { client, reload } = setup()

    fireEvent.click(screen.getByRole('button', { name: 'Reload screen' }))
    expect(reload).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByRole('button', { name: 'Remove all items' }))
    await vi.waitFor(() => {
      expect(reload).toHaveBeenCalledTimes(2)
    })
    expect(await client.listItems({ include_done: true })).toEqual([])
  })

  it('delays calls', async () => {
    vi.useFakeTimers()
    const { client } = setup()
    fireEvent.change(screen.getByRole('combobox', { name: 'Delay every call' }), {
      target: { value: '1000' },
    })
    let settled = false
    void client.health().then(() => (settled = true))

    await vi.advanceTimersByTimeAsync(999)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(settled).toBe(true)
  })

  it('makes calls fail, every time or only the next one', async () => {
    const { client } = setup()
    const failures = screen.getByRole('combobox', { name: 'Make calls fail' })

    fireEvent.change(failures, { target: { value: 'server' } })
    for (let i = 0; i < 2; i++) {
      await expect(client.listItems()).rejects.toSatisfy((e) => isApiError(e, 'server'))
    }

    fireEvent.click(screen.getByRole('checkbox', { name: 'Only the next call' }))
    await expect(client.listItems()).rejects.toSatisfy((e) => isApiError(e, 'server'))
    await expect(client.listItems()).resolves.toBeInstanceOf(Array)

    fireEvent.change(failures, { target: { value: 'rate_limited' } })
    await expect(client.health()).rejects.toSatisfy((e) => isApiError(e, 'rate_limited'))
    expect(client.connection()).toEqual({ status: 'rate_limited', retry_after_seconds: 30 })

    fireEvent.change(failures, { target: { value: 'none' } })
    await expect(client.health()).resolves.toMatchObject({ status: 'ok' })
  })

  it('switches clients', () => {
    const { switchTo } = setup()
    const radios = screen.getAllByRole('radio')

    expect(radios.find((r) => r.getAttribute('value') === 'mock')).toBeChecked()
    fireEvent.click(screen.getByRole('radio', { name: /^Server/ }))
    expect(switchTo).toHaveBeenCalledWith('http')
  })

  it('offers only the client switch on the real server', () => {
    setup(new HttpClient({ fetch: vi.fn() }) as unknown as MockClient)

    expect(screen.queryByRole('button', { name: 'Reset sample data' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Reload screen' })).toBeVisible()
    expect(screen.getByText('Reset, delay and failures apply to sample data only.')).toBeVisible()
    expect(screen.getByRole('radio', { name: /^Server/ })).toBeChecked()
  })

  it('has no axe violations', async () => {
    const { container } = setup()
    expect(await axeViolations(container)).toEqual([])
  })
})
