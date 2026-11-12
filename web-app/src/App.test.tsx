import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MaestroClient } from './api/client.ts'
import { ApiError } from './api/errors.ts'
import { MockClient, failWith } from './api/mock/MockClient.ts'
import App, { REFRESH_MS } from './App.tsx'
import { ClientProvider } from './app/ClientProvider.tsx'
import { captureMessage } from './app/captureMessage.ts'
import { TOKEN_KEY } from './app/session.ts'
import { makeItem } from './test/items.ts'
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

/**
 * Holds one of `client`'s methods until released (or failed) for the calls `matches` picks, so a
 * test can have a slow save or a slow Done while everything else answers at once.
 */
function hold(
  client: MockClient,
  method: 'done' | 'capture' | 'listItems',
  matches: (arg: unknown) => boolean = () => true,
) {
  let release: () => void = () => undefined
  let fail: (error: unknown) => void = () => undefined
  const gate = new Promise<void>((resolve, reject) => {
    release = resolve
    fail = reject
  })
  type Method = (...args: unknown[]) => Promise<unknown>
  const original = (client[method] as Method).bind(client)
  const spy = vi.spyOn(client, method) as unknown as { mockImplementation(fn: Method): void }
  spy.mockImplementation(async (...args) => {
    if (matches(args[0])) await gate
    return original(...args)
  })
  return {
    release: () => {
      release()
    },
    fail: (error: unknown) => {
      fail(error)
    },
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  localStorage.removeItem(TOKEN_KEY)
})

describe('App', () => {
  it('shows loading, then the one list in the design order (#22)', async () => {
    setup()

    expect(screen.getByText('Loading reminders…')).toHaveAttribute('role', 'status')
    expect(await screen.findByRole('region', { name: 'Today' })).toBeInTheDocument()
    const groups = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(groups.slice(0, 4)).toEqual(['Needs a look', 'Next up', 'Today', 'Tomorrow'])
    expect(groups[4]).toMatch(/^This week \d+ more ›$/) // how many depends on the hour it runs
    // Next up is the very next thing: the delivered reminder nobody has answered yet
    const next = screen.getByRole('region', { name: 'Next up' })
    expect(
      within(next).getByRole('article', { name: 'Call the bank about the card' }),
    ).toBeVisible()
    expect(within(next).getByText(/^Sent · waiting for Done or Snooze/)).toBeInTheDocument()
    // the seed's reading that needs a time sits at the top, with its gap and what was said
    const needs = screen.getByRole('region', { name: 'Needs a look' })
    expect(within(needs).getByText('Call the plumber sometime next week')).toBeVisible()
    expect(within(needs).getByText('Needs a time')).toBeVisible()
    expect(within(needs).getByText(/^You said “book flights/)).toBeVisible()
    expect(screen.queryByText('Book the car service')).toBeNull() // done items aren't listed
    expect(screen.queryByText('Gift idea for Sam', { exact: false })).toBeNull() // notes live in search
  })

  it('collapses this week to a count and opens it on request', async () => {
    setup()
    const toggle = await screen.findByRole('button', { name: /^This week \d+ more ›$/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('article', { name: 'Put the bins out' })).toBeNull()

    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'This week hide' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    expect(screen.getByRole('article', { name: 'Put the bins out' })).toBeVisible()
    // a later occurrence of a series is shown without Done or +1 hour (ADR 0009)
    const week = screen.getByRole('region', { name: /^This week/ })
    const [stretch] = within(week).getAllByRole('article', { name: 'Stretch for ten minutes' })
    if (!stretch) throw new Error('the daily series should be in This week')
    expect(within(stretch).queryByRole('button', { name: 'Done' })).toBeNull()
  })

  it('says how many are left today, what needs a look, and that the data is sample data', async () => {
    setup()
    await screen.findByRole('region', { name: 'Today' })

    expect(screen.getByText(/^\d+ left today · 1 needs a look$/)).toBeInTheDocument()
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
    // next week is past the 7-day window, so the series is counted rather than listed
    await waitFor(() => {
      expect(screen.queryByRole('article', { name: 'Water the plants' })).toBeNull()
    })
    expect(screen.getByText('1 more reminder after this week')).toBeVisible()
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

  it('puts the text back in the box when a capture fails', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    client.setFailure(failWith('unauthorized', { times: 1, message: 'Missing API token' }))
    fireEvent.change(field, { target: { value: 'call mom at 5pm' } })
    fireEvent.submit(field)
    expect(
      await screen.findByText('Couldn\'t save "call mom at 5pm": Missing API token'),
    ).toBeVisible()
    expect(field).toHaveValue('call mom at 5pm')
    expect(screen.getByText('Signed out')).toBeInTheDocument()
  })

  it('clears the box as soon as a capture is sent and keeps what is typed during a slow save (#112)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    const save = hold(client, 'capture')
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'call mom at 5pm' } })
    fireEvent.submit(field)
    expect(field).toHaveValue('') // not after the save: now
    fireEvent.change(field, { target: { value: 'buy milk' } })

    save.fail(new ApiError('server', 'Database is locked', { status: 500 }))
    expect(await screen.findByText(/Couldn't save "call mom at 5pm"/)).toBeVisible()
    expect(field).toHaveValue('buy milk') // what was typed meanwhile is not thrown away
  })

  it('keeps each card busy on its own (#112)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    const slow = hold(client, 'done', (id) => id === 1) // "Call the bank about the card"
    setup(client)
    const bank = await screen.findByRole('article', { name: 'Call the bank about the card' })
    const plants = card('Water the plants')

    fireEvent.click(within(bank).getByRole('button', { name: 'Done' }))
    fireEvent.click(within(plants).getByRole('button', { name: 'Done' }))
    expect(await screen.findByText(/^Done for this time/)).toBeVisible() // the plants, at once
    // The bank's Done is still in flight, so its card is still busy and a second tap does nothing.
    expect(
      within(card('Call the bank about the card')).getByRole('button', { name: 'Done' }),
    ).toBeDisabled()

    slow.release()
    await waitFor(() => {
      expect(screen.queryByRole('article', { name: 'Call the bank about the card' })).toBeNull()
    })
  })

  it('drops a response from an older fetch that arrives after a newer one (#112)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    let first = true
    const slow = hold(client, 'listItems', () => {
      const mine = first
      first = false
      return mine
    })
    let reload: () => void = () => undefined
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
    expect(screen.getByText('Loading reminders…')).toBeInTheDocument()

    await client.deleteItem(1) // so the two fetches differ: the slow one would still list it
    act(() => {
      reload()
    })
    await screen.findByRole('region', { name: 'Today' })
    expect(screen.queryByRole('article', { name: 'Call the bank about the card' })).toBeNull()

    slow.release()
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.queryByRole('article', { name: 'Call the bank about the card' })).toBeNull()
  })

  it('fetches again on a timer and when the tab becomes visible (#112)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    const list = vi.spyOn(client, 'feed')
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    expect(list).toHaveBeenCalledTimes(1)

    act(() => {
      vi.advanceTimersByTime(REFRESH_MS)
    })
    await waitFor(() => {
      expect(list).toHaveBeenCalledTimes(2)
    })

    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => {
      expect(list).toHaveBeenCalledTimes(3)
    })
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

  it('asks to sign in on a 401, keeps the server token out of storage, and loads once signed in (ADR 0018)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC', masterToken: 't0ken' })
    client.setFailure(failWith('unauthorized', { times: 1, message: 'Missing API token' }))
    setup(client)

    const form = await screen.findByRole('form', { name: 'Sign in to your server' })
    expect(screen.queryByText('Loading reminders…')).toBeNull()
    expect(screen.queryByText('This device was signed out.')).toBeNull() // first time here
    expect(screen.getByText('Signed out')).toBeInTheDocument()
    const tokenField = within(form).getByLabelText('Server token')
    const button = within(form).getByRole('button', { name: 'Sign in' })
    expect(button).toBeDisabled()

    fireEvent.change(tokenField, { target: { value: 'wrong' } })
    fireEvent.click(button)
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      "That isn't the server's token",
    )
    expect(tokenField).toHaveValue('wrong') // kept, to fix a typo

    fireEvent.change(tokenField, { target: { value: 't0ken' } })
    fireEvent.change(within(form).getByLabelText('Name this device'), {
      target: { value: "Sachin's iPhone" },
    })
    fireEvent.click(button)
    expect(await screen.findByRole('region', { name: 'Today' })).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Sign in to your server' })).toBeNull()
    expect(localStorage.getItem(TOKEN_KEY)).toMatch(/^mdt_/)
    expect(JSON.stringify(localStorage)).not.toContain('t0ken')
    expect((await client.listDevices()).map((d) => d.name)).toEqual(["Sachin's iPhone"])
  })

  it('signs out when its token stops working, forgets it, and says so', async () => {
    localStorage.setItem(TOKEN_KEY, 'mdt_old')
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    const bank = await screen.findByRole('article', { name: 'Call the bank about the card' })

    client.setFailure(failWith('unauthorized', { times: 1, message: 'Invalid API token' }))
    fireEvent.click(within(bank).getByRole('button', { name: 'Done' }))

    await screen.findByRole('form', { name: 'Sign in to your server' })
    expect(screen.getByText('This device was signed out.')).toBeInTheDocument()
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
  })

  it('keeps its token when the server is merely unreachable', async () => {
    localStorage.setItem(TOKEN_KEY, 'mdt_keep')
    const client = new MockClient({ storage: null, timeZone: 'UTC', failure: failWith('offline') })
    setup(client)

    await screen.findByRole('alert')
    expect(screen.queryByRole('form', { name: 'Sign in to your server' })).toBeNull()
    expect(localStorage.getItem(TOKEN_KEY)).toBe('mdt_keep')
  })

  it('says what a capture did, in one sentence', () => {
    const reminder = makeItem({ when: 'Thu 05 Nov 17:45' })
    const note = makeItem({ id: 2, kind: 'note', when: 'note' })
    const plumber = makeItem({ id: 3, kind: 'task', text: 'Call the plumber', gap: 'when' })
    const undo = { id: 1, expires_at: '2026-11-05T14:10:00+05:30' }

    expect(captureMessage({ added: [reminder], needs_look: [], undo, proposal: null })).toBe(
      'Reminder set for Thu 05 Nov 17:45.',
    )
    expect(captureMessage({ added: [note], needs_look: [], undo, proposal: null })).toBe(
      'Saved as a note.',
    )
    expect(
      captureMessage({ added: [reminder, note], needs_look: [plumber], undo, proposal: null }),
    ).toBe('Added 2. "Call the plumber" needs a time.')
    expect(captureMessage({ added: [], needs_look: [plumber], undo, proposal: null })).toBe(
      '"Call the plumber" needs a time.',
    )
    expect(
      captureMessage({
        added: [],
        needs_look: [],
        undo: null,
        proposal: { id: 4, lines: ['Call mom at 17:00'] },
      }),
    ).toBe('Waiting for confirmation: Call mom at 17:00')
    expect(captureMessage({ added: [], needs_look: [], undo: null, proposal: null })).toBe('Saved.')
  })

  it('answers what a reading needs, in place, and it moves into the list (#22, #126)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    const needs = await screen.findByRole('region', { name: 'Needs a look' })
    const plumber = within(needs).getByRole('article', {
      name: 'Call the plumber sometime next week',
    })

    fireEvent.click(within(plumber).getByRole('button', { name: 'Answer…' }))
    const field = within(plumber).getByLabelText('When?')
    expect(field).toHaveFocus()
    expect(within(plumber).getByRole('button', { name: 'Save' })).toBeDisabled()

    fireEvent.change(field, { target: { value: 'no idea' } })
    fireEvent.click(within(plumber).getByRole('button', { name: 'Save' }))
    expect(await within(plumber).findByRole('alert')).toHaveTextContent('still needs a time')
    expect(field).toHaveValue('no idea') // kept, to try again

    fireEvent.change(field, { target: { value: 'tomorrow at 9am' } })
    fireEvent.click(within(plumber).getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/^Set for /)).toBeVisible()
    await waitFor(() => {
      expect(screen.queryByRole('region', { name: 'Needs a look' })).toBeNull()
    })
    const tomorrow = screen.getByRole('region', { name: 'Tomorrow' })
    expect(
      within(tomorrow).getByRole('article', { name: 'Call the plumber sometime next week' }),
    ).toBeVisible()
    expect(screen.getByText(/^\d+ left today$/)).toBeInTheDocument() // nothing needs a look now
  })

  it('dismisses a reading that needs a look', async () => {
    setup()
    const needs = await screen.findByRole('region', { name: 'Needs a look' })
    fireEvent.click(within(needs).getByRole('button', { name: 'Dismiss' }))
    expect(
      await screen.findByText('Dismissed "Call the plumber sometime next week".'),
    ).toBeVisible()
    await waitFor(() => {
      expect(screen.queryByRole('region', { name: 'Needs a look' })).toBeNull()
    })
  })

  it('shows the empty state for a list with nothing scheduled but something to answer', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    for (const item of await client.listItems({ include_done: true })) {
      if (!item.gap) await client.deleteItem(item.id)
    }
    setup(client)
    expect(await screen.findByText('Nothing scheduled')).toBeVisible()
    expect(screen.getByRole('region', { name: 'Needs a look' })).toBeInTheDocument()
  })

  it('walks the rows with j and k, acts with d, answers with a, and captures with / (#22)', async () => {
    setup()
    await screen.findByRole('region', { name: 'Today' })

    fireEvent.keyDown(document.body, { key: 'j' })
    const plumber = screen.getByRole('article', { name: 'Call the plumber sometime next week' })
    expect(plumber).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'a' })
    const field = within(plumber).getByLabelText('When?')
    expect(field).toHaveFocus()
    fireEvent.keyDown(field, { key: 'j' }) // typing: just a letter
    expect(field).toHaveFocus()
    fireEvent.click(within(plumber).getByRole('button', { name: 'Cancel' }))

    fireEvent.keyDown(document.body, { key: 'j' })
    expect(screen.getByRole('article', { name: 'Call the bank about the card' })).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'k' })
    expect(plumber).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'j' })
    fireEvent.keyDown(document.body, { key: 'd' })
    expect(
      await screen.findByText('Done. Any pending SMS or call for it is cancelled.'),
    ).toBeVisible()

    fireEvent.keyDown(document.body, { key: '/' })
    const capture = screen.getByRole('textbox', { name: 'Capture a reminder or note' })
    expect(capture).toHaveFocus()
    fireEvent.keyDown(capture, { key: '?' })
    expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).toBeNull() // typing

    fireEvent.keyDown(document.body, { key: '?' })
    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).toBeNull()
  })

  it('confirms a capture with Undo and a countdown, and undo takes it back (#104)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'renew the passport' } })
    fireEvent.submit(field)
    expect(await screen.findByText('Saved as a task.')).toBeVisible()
    const undoButton = await screen.findByRole('button', { name: /^Undo · 10s$/ })
    // the new row is in the list, highlighted while the line is up
    await waitFor(() => {
      expect(screen.getByRole('article', { name: 'Renew the passport' })).toBeVisible()
    })

    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(screen.getByRole('button', { name: /^Undo · 7s$/ })).toBeVisible()

    fireEvent.click(undoButton)
    expect(await screen.findByText('Took it back.')).toBeVisible()
    await waitFor(() => {
      expect(screen.queryByRole('article', { name: 'Renew the passport' })).toBeNull()
    })
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
  })

  it('counts down 10 seconds and the line goes; the reminder stays (#104)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'renew the passport' } })
    fireEvent.submit(field)
    await screen.findByRole('button', { name: /^Undo · 10s$/ })
    await act(() => Promise.resolve()) // settle the mount, so the countdown's interval exists

    act(() => {
      vi.advanceTimersByTime(11_000)
    })
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
    })
    expect(screen.queryByText('Saved as a task.')).toBeNull()
    expect(screen.getByRole('article', { name: 'Renew the passport' })).toBeVisible() // still saved
  })

  it('names what a multi-part capture did, including what needs a look (#104)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'book the mot sometime next week' } })
    fireEvent.submit(field)
    expect(await screen.findByText('"Book the mot sometime next week" needs a time.')).toBeVisible()
    const needs = await screen.findByRole('region', { name: 'Needs a look' })
    expect(
      within(needs).getByRole('article', { name: 'Book the mot sometime next week' }),
    ).toBeVisible()
    // undo takes back the needs-a-look reading too
    fireEvent.click(screen.getByRole('button', { name: /^Undo/ }))
    await waitFor(() => {
      expect(screen.queryByRole('article', { name: 'Book the mot sometime next week' })).toBeNull()
    })
  })

  it('explains an undo that comes too late, and keeps the reminder (#104)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'renew the passport' } })
    fireEvent.submit(field)
    const undoButton = await screen.findByRole('button', { name: /^Undo/ })

    client.setFailure(
      failWith('conflict', { times: 1, message: 'The undo window for capture #1 has passed' }),
    )
    fireEvent.click(undoButton)
    expect(
      await screen.findByText("Too late to undo — it's been more than 10 minutes."),
    ).toBeVisible()
    expect(screen.getByRole('article', { name: 'Renew the passport' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull() // the line gave way
  })

  it('replaces the line when a second capture follows (#104)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    fireEvent.change(field, { target: { value: 'renew the passport' } })
    fireEvent.submit(field)
    await screen.findByText('Saved as a task.')
    fireEvent.change(field, { target: { value: 'the gate code is 4321' } })
    fireEvent.submit(field)
    expect(await screen.findByText('Saved as a note.')).toBeVisible()
    expect(screen.queryByText('Saved as a task.')).toBeNull() // only the newest line shows
    expect(screen.getAllByRole('button', { name: /^Undo/ })).toHaveLength(1)
  })

  it('queues a capture made offline and sends it when the server answers again (#104)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    client.setFailure(failWith('offline'))
    fireEvent.change(field, { target: { value: 'renew the passport' } })
    fireEvent.submit(field)
    expect(await screen.findByText('Saved on this phone, will send when online.')).toBeVisible()
    expect(field).toHaveValue('') // kept in the outbox, not the box
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull() // nothing saved yet
    expect(screen.getByText('Offline · 1 waiting to send')).toBeInTheDocument()

    client.setFailure(null) // the server answers again; the next refresh flushes the outbox
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(await screen.findByText('Sent the capture saved on this phone.')).toBeVisible()
    await waitFor(() => {
      expect(screen.getByRole('article', { name: 'Renew the passport' })).toBeVisible()
    })
    expect(screen.queryByText(/waiting to send/)).toBeNull()
  })

  it('reports a queued capture the server refuses, without retrying it forever (#104)', async () => {
    const client = new MockClient({ storage: null, timeZone: 'UTC' })
    setup(client)
    await screen.findByRole('region', { name: 'Today' })
    const field = screen.getByRole('textbox', { name: 'Capture a reminder or note' })

    client.setFailure(failWith('offline'))
    fireEvent.change(field, { target: { value: '   ' } })
    fireEvent.submit(field) // CaptureBar trims: nothing sent
    fireEvent.change(field, { target: { value: 'renew the passport' } })
    fireEvent.submit(field)
    await screen.findByText('Saved on this phone, will send when online.')

    client.setFailure(
      failWith('invalid', {
        methods: ['capture'],
        message: 'A capture needs between 1 and 2000 characters',
      }),
    )
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(
      await screen.findByText(
        'Couldn\'t save "renew the passport": A capture needs between 1 and 2000 characters',
      ),
    ).toBeVisible()
    expect(screen.queryByText(/waiting to send/)).toBeNull() // dropped, not retried forever
  })

  it('has no axe violations once loaded', async () => {
    setup()
    await screen.findByRole('region', { name: 'Today' })
    expect(await axeViolations(document.body)).toEqual([])
  })
})
