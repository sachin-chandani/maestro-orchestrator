// Example sentences and what a split capture makes of them (#104): one fixture shared by the mock
// client's tests and the screen's, so the mock can never drift from what the screens rehearse.
// The real split happens on the server (#130); the mock's stand-in parser covers these.

export interface Sentence {
  text: string
  /** The texts saved and actionable at once, in order. */
  added: string[]
  /** The texts saved but needing a look, with their gap. */
  needsLook: [text: string, gap: 'when' | 'what' | 'channel'][]
}

export const SENTENCES: Sentence[] = [
  {
    text: 'call mom tomorrow at 5pm',
    added: ['Call mom'],
    needsLook: [],
  },
  {
    text: 'the wifi password is on the router',
    added: ['The wifi password is on the router'],
    needsLook: [],
  },
  {
    text: 'renew the passport',
    added: ['Renew the passport'],
    needsLook: [],
  },
  {
    text: 'call the plumber sometime next week',
    added: [],
    needsLook: [['Call the plumber sometime next week', 'when']],
  },
]
