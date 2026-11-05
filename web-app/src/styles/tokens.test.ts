// @vitest-environment node
/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

// Read as text with fs: Vite won't import files from outside web-app/, and Vitest doesn't load CSS.
const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')
const tokensCss = read('./tokens.css')
const designReadme = read('../../../docs/design/mobile/README.md')

// tokens.css is the single source of truth; these tests read it as text, so they check what ships.

type Theme = Record<string, string>

function declarations(block: string): Theme {
  const out: Theme = {}
  for (const m of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    out[m[1] ?? ''] = (m[2] ?? '').trim().toLowerCase()
  }
  return out
}

function themes(css: string): { light: Theme; dark: Theme; darkOnly: Theme } {
  const dark = /@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([^}]*)\}/.exec(css)
  const withoutDark = dark ? css.replace(dark[0], '') : css
  const light = declarations(/:root\s*\{([^}]*)\}/.exec(withoutDark)?.[1] ?? '')
  const darkOnly = declarations(dark?.[1] ?? '')
  return { light, dark: { ...light, ...darkOnly }, darkOnly }
}

const { light, dark, darkOnly } = themes(tokensCss)

// WCAG 2.2 relative luminance and contrast ratio.
function luminance(hex: string): number {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05)
}

const TEXT = 4.5 // WCAG 1.4.3, normal-size text
const UI = 3 // WCAG 1.4.11, focus rings, control boundaries, status dots

const PAIRS: [fg: string, bg: string, min: number][] = [
  ['--color-ink', '--color-surface', TEXT],
  ['--color-ink', '--color-ground', TEXT],
  ['--color-ink', '--color-surface-sunken', TEXT],
  ['--color-ink-secondary', '--color-surface', TEXT],
  ['--color-muted', '--color-surface', TEXT],
  ['--color-muted', '--color-ground', TEXT],
  ['--color-muted', '--color-surface-sunken', TEXT],
  ['--color-accent-text', '--color-surface', TEXT],
  ['--color-accent-text', '--color-ground', TEXT],
  ['--color-accent-text-hover', '--color-surface', TEXT],
  ['--color-on-accent', '--color-accent', TEXT],
  ['--color-urgent', '--color-urgent-bg', TEXT],
  ['--color-info', '--color-info-bg', TEXT],
  ['--color-ok', '--color-ok-bg', TEXT],
  ['--color-list', '--color-list-bg', TEXT],
  ['--color-neutral', '--color-neutral-bg', TEXT],
  ['--color-toast', '--color-toast-bg', TEXT],
  ['--color-focus', '--color-surface', UI],
  ['--color-focus', '--color-ground', UI],
  ['--color-ok-dot', '--color-ok-bg', UI],
  ['--color-line-control', '--color-surface', UI],
  ['--color-line-control', '--color-surface-sunken', UI],
]

/**
 * Pairs that fail on purpose, each with its reason. A fix must also remove the entry here, so this
 * list can't go stale (see "still fail" below).
 */
const KNOWN_FAILURES: Record<string, string> = {
  'light --color-line-control on --color-surface':
    "Design's #D1D5DB control border is 1.47:1; WCAG 1.4.11 wants 3:1. Awaiting a design decision.",
  'light --color-line-control on --color-surface-sunken':
    'Same design colour on the input fill (#F9FAFB).',
}

describe.each([
  ['light', light],
  ['dark', dark],
])('%s theme', (name, theme) => {
  it.each(PAIRS)(`%s on %s meets its contrast minimum`, (fg, bg, min) => {
    const key = `${name} ${fg} on ${bg}`
    const ratio = contrast(theme[fg] ?? '', theme[bg] ?? '')
    if (key in KNOWN_FAILURES) return // checked below
    expect(ratio, `${key}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(min)
  })
})

describe('known failures', () => {
  it.each(Object.keys(KNOWN_FAILURES))('%s still fail (remove the entry once fixed)', (key) => {
    const [name, fg = '', , bg = ''] = key.split(' ')
    const theme = name === 'dark' ? dark : light
    const min = PAIRS.find(([f, b]) => f === fg && b === bg)?.[2] ?? 0
    expect(contrast(theme[fg] ?? '', theme[bg] ?? '')).toBeLessThan(min)
  })
})

describe('tokens', () => {
  it('use the design README values in the light theme', () => {
    const named: Record<string, string> = {
      ground: '--color-ground',
      surface: '--color-surface',
      line: '--color-line',
      ink: '--color-ink',
      muted: '--color-muted',
      accent: '--color-accent',
    }
    const paired: Record<string, [string, string]> = {
      urgent: ['--color-urgent', '--color-urgent-bg'],
      info: ['--color-info', '--color-info-bg'],
      ok: ['--color-ok', '--color-ok-bg'],
      list: ['--color-list', '--color-list-bg'],
    }
    const rows = [
      ...designReadme.matchAll(/^\| `(\w+)` \| `(#[0-9A-F]{6})`(?: on `(#[0-9A-F]{6})`)? \|/gm),
    ]
    expect(rows.length).toBe(10)
    for (const [, token = '', fg = '', bg] of rows) {
      if (bg) {
        const [fgVar = '', bgVar = ''] = paired[token] ?? []
        expect(light[fgVar], token).toBe(fg.toLowerCase())
        expect(light[bgVar], token).toBe(bg.toLowerCase())
      } else {
        expect(light[named[token] ?? ''], token).toBe(fg.toLowerCase())
      }
    }
    expect(designReadme).toContain('`#D1D5DB` for controls')
    expect(light['--color-line-control']).toBe('#d1d5db')
  })

  it('use the design README radii and touch target', () => {
    expect(designReadme).toContain('14px cards, 12px inputs and bars, 10px buttons, 6px chips')
    expect([
      light['--radius-card'],
      light['--radius-input'],
      light['--radius-button'],
      light['--radius-chip'],
    ]).toEqual(['14px', '12px', '10px', '6px'])
    expect(designReadme).toContain('Touch targets are at least 44px')
    expect(light['--touch-target']).toBe('44px')
  })

  it('give the dark theme its own value for every colour', () => {
    const colours = Object.keys(light).filter((k) => k.startsWith('--color-'))
    expect(colours.filter((k) => !(k in darkOnly))).toEqual([])
    expect(Object.keys(darkOnly).filter((k) => !(k in light))).toEqual([]) // no dark-only typos
  })

  it('name IBM Plex first, with system fallbacks', () => {
    expect(light['--font-sans']).toMatch(/^'ibm plex sans', system-ui/)
    expect(light['--font-mono']).toMatch(/^'ibm plex mono', ui-monospace/)
  })
})

// Every other stylesheet uses tokens only: no raw colours, and no variable tokens.css doesn't define.
const stylesheets = Object.fromEntries(
  Object.keys(import.meta.glob(['/src/**/*.css', '!/src/styles/tokens.css'])).map((path) => [
    path,
    read(`../..${path}`),
  ]),
)

describe('stylesheets', () => {
  it('are found', () => {
    expect(Object.keys(stylesheets)).toEqual(
      expect.arrayContaining(['/src/App.module.css', '/src/styles/base.css']),
    )
  })

  it.each(Object.entries(stylesheets))('%s uses no raw colours', (_, css) => {
    const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '')
    expect(withoutComments.match(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/gi) ?? []).toEqual([])
  })

  // Only base.css sets page-wide element defaults; anything else styling a bare `li` or `h2`
  // restyles every component that uses one (the placeholder's list rules once boxed every chip).
  it.each(Object.entries(stylesheets).filter(([path]) => path !== '/src/styles/base.css'))(
    '%s scopes every rule to a class',
    (_, css) => {
      const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '')
      const selectors = [...withoutComments.matchAll(/([^{}@;]+)\{/g)]
        .flatMap((m) => (m[1] ?? '').split(','))
        .map((sel) => sel.trim())
        .filter((sel) => sel && !sel.startsWith('@') && !/^(from|to|\d+%)$/.test(sel))
      expect(selectors.filter((sel) => !sel.startsWith('.'))).toEqual([])
    },
  )

  it.each(Object.entries(stylesheets))('%s uses only defined tokens', (_, css) => {
    const used = [...css.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1] ?? '')
    expect(used.filter((v) => !(v in light))).toEqual([])
  })
})
