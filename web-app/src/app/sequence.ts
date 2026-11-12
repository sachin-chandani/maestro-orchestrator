import { useState } from 'react'

/** Hands out increasing numbers and says whether one is still the latest. */
export interface Sequence {
  next(): number
  isLatest(n: number): boolean
}

export function sequence(): Sequence {
  let n = 0
  return {
    next: () => ++n,
    isLatest: (k) => k === n,
  }
}

/**
 * One Sequence for the life of a component: a screen numbers its fetches so a slow older response
 * can't overwrite a newer one, and its notices so two with the same words are told apart (#112).
 * Counting doesn't render anything, so this is neither state nor a ref.
 */
export function useSequence(): Sequence {
  const [seq] = useState(sequence)
  return seq
}
