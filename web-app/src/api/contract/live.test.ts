// @vitest-environment node
//
// HttpClient against a real `maestro serve` (#108). Skipped unless MAESTRO_LIVE_URL names one that
// was started with a scratch database and MAESTRO_API_TOKEN=$MAESTRO_LIVE_TOKEN; CI and the normal
// test run never touch the network. Everything it adds, it removes again.
//
//   MAESTRO_DB="$(mktemp -d)/m.db" MAESTRO_API_TOKEN=live-token maestro serve --port 8765
//   MAESTRO_LIVE_URL=http://127.0.0.1:8765 MAESTRO_LIVE_TOKEN=live-token npx vitest run live

import { afterAll, describe, expect, it } from 'vitest'
import { ApiError } from '../errors.ts'
import { HttpClient } from '../http/HttpClient.ts'

const url = process.env.MAESTRO_LIVE_URL
const master = process.env.MAESTRO_LIVE_TOKEN ?? ''

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('Expected the call to fail')
}

describe.skipIf(!url)('HttpClient against a live maestro serve', () => {
  const client = new HttpClient({ baseUrl: url })
  const added: number[] = []
  let deviceId = 0
  let deviceToken = ''

  afterAll(async () => {
    const janitor = new HttpClient({ baseUrl: url, token: master })
    for (const id of added) await janitor.deleteItem(id).catch(() => undefined)
    if (deviceId) await janitor.revokeDevice(deviceId).catch(() => undefined)
  })

  it('is signed out without a token, and signs in with the master token once', async () => {
    expect((await rejection(client.listItems())).kind).toBe('unauthorized')
    expect(client.connection()).toEqual({ status: 'signed_out' })
    expect((await rejection(client.registerDevice('wrong', 'live test'))).kind).toBe('unauthorized')

    const signedIn = await client.registerDevice(master, 'live test')
    deviceId = signedIn.device.id
    deviceToken = signedIn.token
    expect(signedIn.token).toMatch(/^mdt_/)
    expect((await client.listDevices()).find((d) => d.id === deviceId)?.current).toBe(true)
    expect(client.connection()).toEqual({ status: 'connected' })
  })

  it('undoes one of several captures by its own token, leaving the others (#104)', async () => {
    const a = await client.captureSplit('live test: buy eggs and call the vet at 4pm')
    const b = await client.captureSplit('live test: renew the licence')
    const ids = (r: typeof a) => [...r.added, ...r.needs_look].map((i) => i.id)
    added.push(...ids(a), ...ids(b))
    if (!a.undo || !b.undo) throw new Error('no undo tokens')
    expect(a.undo.id).not.toBe(b.undo.id)

    await client.undoCapture(a.undo.id)
    const left = (await client.listItems()).map((i) => i.id)
    for (const id of ids(a)) expect(left).not.toContain(id)
    for (const id of ids(b)) expect(left).toContain(id)
    await client.undoCapture(b.undo.id)
    for (const id of ids(b)) expect(left).toContain(id) // the list from before; now gone:
    expect((await rejection(client.getItem(ids(b)[0] ?? -1))).kind).toBe('not_found')
  })

  it('captures and undoes, so the capture leaves nothing behind (#104)', async () => {
    const result = await client.captureSplit('live test: water the plants tomorrow at 8am')
    const saved = result.added[0]
    if (!saved || !result.undo) throw new Error('nothing saved or no undo token')
    await client.undoCapture(result.undo.id)
    expect((await rejection(client.getItem(saved.id))).kind).toBe('not_found')
    await client.undoCapture(result.undo.id) // idempotent: the server answers yes again
  })

  it('captures, lists, acts and deletes with the device token', async () => {
    const result = await client.captureSplit('live test: call the bank tomorrow at 9am')
    expect(result.added.length).toBe(1)
    const item = result.added[0]
    if (!item) throw new Error('nothing added')
    added.push(item.id)
    expect(item.kind).toBe('reminder')
    expect((await client.listItems()).some((i) => i.id === item.id)).toBe(true)

    const snoozed = await client.snooze(item.id, '10m')
    expect(snoozed.id).toBe(item.id)
    expect((await client.done(item.id)).status).toBe('done')
    await client.deleteItem(item.id)
    added.pop()
    expect((await rejection(client.getItem(item.id))).kind).toBe('not_found')
    expect((await client.health()).status).toBe('ok')
  })

  it('cannot manage devices, and is signed out at once when revoked', async () => {
    expect((await rejection(client.registerDevice(deviceToken, 'spare'))).status).toBe(403)
    await new HttpClient({ baseUrl: url, token: master }).revokeDevice(deviceId)
    deviceId = 0
    expect((await rejection(client.listItems())).kind).toBe('unauthorized')
    expect(client.connection()).toEqual({ status: 'signed_out' })
  })
})
