import axe from 'axe-core'

// Runs axe on rendered markup and returns one readable line per violation, so a failing test says
// what is wrong and where. jsdom has no layout or canvas, so colour contrast can't be measured here
// and that rule is off; contrast has to be checked in a real browser.
export async function axeViolations(container: Element): Promise<string[]> {
  const results = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } })
  return results.violations.flatMap((v) =>
    v.nodes.map((node) => `${v.id}: ${v.help} (${node.target.join(' ')})`),
  )
}
