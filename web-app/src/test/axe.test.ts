import { describe, expect, it } from 'vitest'
import { axeViolations } from './axe.ts'

// Guards the guard: if axe were misconfigured, every accessibility test would pass silently.
describe('axeViolations', () => {
  it('reports inaccessible markup', async () => {
    const el = document.createElement('div')
    el.innerHTML = '<img src="x.png"><button></button>'
    document.body.append(el)

    const violations = await axeViolations(el)

    expect(violations).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^image-alt:/),
        expect.stringMatching(/^button-name:/),
      ]),
    )
    el.remove()
  })

  it('passes accessible markup', async () => {
    const el = document.createElement('div')
    el.innerHTML = '<img src="x.png" alt="Logo"><button type="button">Save</button>'
    document.body.append(el)

    expect(await axeViolations(el)).toEqual([])
    el.remove()
  })
})
