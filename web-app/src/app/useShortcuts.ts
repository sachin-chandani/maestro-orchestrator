import { useEffect } from 'react'

/** The list's keyboard shortcuts (#22). Each is one key, with nothing held down. */
export interface Shortcuts {
  /** "/": focus the capture field. */
  capture: () => void
  /** "j" and "k": move the row focus down and up. */
  down: () => void
  up: () => void
  /** "d" and "s": Done and +1 hour on the focused row; "a" answers a Needs-a-look row. */
  done: () => void
  snooze: () => void
  answer: () => void
  /** "?": show the shortcuts; Escape closes them. */
  help: () => void
  escape: () => void
}

export const SHORTCUT_LIST: [string, string][] = [
  ['/', 'Capture'],
  ['j', 'Next reminder'],
  ['k', 'Previous reminder'],
  ['d', 'Done'],
  ['s', 'Snooze an hour'],
  ['a', 'Answer what a reminder needs'],
  ['?', 'These shortcuts'],
]

/** Whether the key press happened while typing, in which case the letters are just letters. */
export function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  // jsdom leaves isContentEditable undefined on ordinary elements; the attribute is what counts.
  return (
    target.getAttribute('contenteditable') === 'true' ||
    target.getAttribute('contenteditable') === ''
  )
}

/** Wires the shortcuts to the document while the component is mounted; off while typing. */
export function useShortcuts(handlers: Shortcuts, enabled = true): void {
  useEffect(() => {
    if (!enabled) return
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key === 'Escape') {
        handlers.escape()
        return
      }
      if (isTyping(event.target)) return
      const action = {
        '/': handlers.capture,
        j: handlers.down,
        k: handlers.up,
        d: handlers.done,
        s: handlers.snooze,
        a: handlers.answer,
        '?': handlers.help,
      }[event.key]
      if (!action) return
      event.preventDefault()
      action()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
    }
  }, [handlers, enabled])
}
