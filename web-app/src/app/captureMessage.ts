import type { CaptureResult, Item } from '../api/types.ts'

// What a capture is confirmed as. A reminder's message names its time, so it isn't in here (#114).
const SAVED: Partial<Record<Item['kind'], string>> = {
  note: 'Saved as a note.',
  task: 'Saved as a task.',
}

/** What a reading that needs a look is missing (maestro.gaps.WANTED). */
const WANTED: Record<string, string> = {
  when: 'a time',
  what: 'a description',
  channel: 'a channel',
}

/** One sentence for what a capture did (ADR 0017). The undo countdown itself is #104. */
export function captureMessage(result: CaptureResult): string {
  if (result.proposal) return `Waiting for confirmation: ${result.proposal.lines.join('; ')}`
  const parts: string[] = []
  const [only] = result.added
  if (result.added.length === 1 && only)
    parts.push(SAVED[only.kind] ?? `Reminder set for ${only.when}.`)
  else if (result.added.length > 1) parts.push(`Added ${String(result.added.length)}.`)
  for (const item of result.needs_look) {
    parts.push(`"${item.text}" needs ${WANTED[item.gap ?? ''] ?? 'a look'}.`)
  }
  return parts.join(' ') || 'Saved.'
}
