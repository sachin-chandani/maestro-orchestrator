import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { MaestroClient } from './api/client.ts'
import { MockClient, failWith } from './api/mock/MockClient.ts'
import App from './App.tsx'
import { ClientProvider } from './app/ClientProvider.tsx'
import { axeViolations } from './test/axe.ts'

// The app on MockClient, in memory. The clock is the real one: the seed places its sample reminders
// around now, so the screen shows every group whatever time the tests run.

function setup(client: MaestroClient = new MockClient({ storage: null, timeZone: 'UTC' })) {
  render(
    <ClientProvider client={client} kind="mock">
      <App />
    </ClientProvider>,
  )
  return client
}

const card = (text: string) => screen.getByRole('article', { name: text })

describe('App', () => {
  it('shows loading, then the reminders grouped by day', async () => {
    setup()

    expect(screen.getByText('Loading reminders…')).toHaveAttribute('role', 'status')
    expect(await screen.findByRole('region', { name: 'Today' })).toBeInTheDocument()
    const groups = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(groups[0]).toBe('Today')
    expect(groups).toContain('Later')
    expect(screen.getAllByRole('article').length).toBe(8) // the seed's open reminders; no notes
    expect(screen.queryByText('Book the car service')).toBeNull() // done items aren't listed
  })

  it('says how many are left today and that the data is sample data', async () => {
    setup()
    const today = await screen.findByRole('region', { name: 'Today' })
    const left = within(today).getAllByRole('article').length

    expect(screen.getByText(new RegExp(`· ${String(left)} left today$`))).toBeInTheDocument()
    expect(screen.getByText('Sample data')).toBeInTheDocument()
  })

  it('marks a reminder done and says so', async () => {
    setup()
    const bank = await screen.findByRole('article', { name: 'Call the bank about the card' })

    fireEvent.click(within(bank).getByRole('button', { name: 'Done' }))
    expect(
      await screen.findByText('Done. Any pending SMS or call for it is cancelled.'),
    ).toBeVisible()
    expect(screen.queryByRole('article', { name: 'Call the bank about the card' })).toBeNull()
  })

  it('keeps a recurring reminder after done and says when it is next (ADR 0009)', async () => {
    setup()
    const plants = await screen.findByRole('article', { name: 'Water the plants' })

    fireEvent.click(within(plants).getByRole('button', { name: 'Done' }))
    expect(await screen.findByText(/^Done for this time\. Next: .+, every \w+day\.$/)).toBeVisible()
    expect(card('Water the plants')).toBeInTheDocument()
  })

  it('snoozes for an hour and says until when', async () => {
    setup()
    const kids = await screen.findByRole('article', { name: 'Pick up the kids from practice' })

    fireEvent.click(within(kids).getByRole('button', { name: '+1 hour' }))
    expect(
      await screen.findByText(/^Snoozed to \d\d:\d\d\. Escalation restarts from then\.$/),
    ).toBeVisible()
  })

  it('captures a reminder from the bar and lists it', async () => {
    setup()
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'text me tomorrow at 9am to call the plumber' } })
    fireEvent.submit(field)
    expect(await screen.findByText(/^Reminder set for .+ 09:00\.$/)).toBeVisible()
    expect(
      within(screen.getByRole('region', { name: 'Tomorrow' })).getByText('Call the plumber'),
    ).toBeVisible()
    expect(field).toHaveValue('')
  })

  it('saves untimed text as a note', async () => {
    setup()
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'the wifi password is on the router' } })
    fireEvent.submit(field)
    expect(await screen.findByText('Saved as a note.')).toBeVisible()
  })

  it('shows the empty state when there is nothing coming up', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    for (const item of await client.listItems({ include_done: true })) {
      await client.deleteItem(item.id)
    }
    setup(client)

    expect(await screen.findByText('Nothing coming up')).toBeVisible()
    expect(screen.getByText(/Try “text me tomorrow at 9am/)).toBeVisible()
    expect(screen.getByText(/0 left today$/)).toBeInTheDocument()
  })

  it('explains a failed load and loads again on retry', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC', failure: failWith('offline') })
    setup(client)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load your reminders: Injected offline failure")
    expect(screen.getByText('Offline')).toBeInTheDocument() // the connection pill

    client.setFailure(null)
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('region', { name: 'Today' })).toBeInTheDocument()
    expect(screen.getByText('Sample data')).toBeInTheDocument()
  })

  it('keeps the reminder and says why when done fails', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    const bank = await screen.findByRole('article', { name: 'Call the bank about the card' })

    client.setFailure(
      failWith('server', { methods: ['done'], times: 1, message: 'Database is locked' }),
    )
    fireEvent.click(within(bank).getByRole('button', { name: 'Done' }))
    expect(await screen.findByText("Couldn't mark it done: Database is locked")).toBeVisible()
    expect(
      within(card('Call the bank about the card')).getByRole('button', { name: 'Done' }),
    ).toBeEnabled()
  })

  it('keeps the text in the box when a capture fails', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    client.setFailure(failWith('unauthorized', { times: 1, message: 'Missing API token' }))
    fireEvent.change(field, { target: { value: 'call mom at 5pm' } })
    fireEvent.submit(field)
    expect(await screen.findByText("Couldn't save it: Missing API token")).toBeVisible()
    expect(field).toHaveValue('call mom at 5pm')
    expect(screen.getByText('Signed out')).toBeInTheDocument()
  })

  it('gives dev tools a reload that goes through loading, so failures show', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    let reload: () => void = () => {
      throw new Error('App never rendered its dev tools')
    }
    render(
      <ClientProvider client={client} kind="mock">
        <App
          devTools={(r) => {
            reload = r
            return null
          }}
        />
      </ClientProvider>,
    )
    await screen.findByRole('region', { name: 'Today' })

    client.setFailure(failWith('server', { message: 'Database is locked' }))
    act(() => {
      reload()
    })
    expect(screen.getByText('Loading reminders…')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('Database is locked')
  })

  it('has no axe violations once loaded', async () => {
    setup()
    await screen.findByRole('region', { name: 'Today' })
    expect(await axeViolations(document.body)).toEqual([])
  })
})
