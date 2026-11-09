// A small stand-in for the server's parser (maestro/parser.py), so MockClient.capture() can turn
// text into a reminder, a task or a note. It understands the phrases the design uses, not everything the
// real parser does; the real one always runs on the server. It is not the parse preview (#94).

import type { ItemKind } from '../types.ts'
import { WEEKDAYS, addDays, fromWallTime, wallTime, weekdayOf, type Ymd } from './time.ts'
import { textSaysUrgent } from './rules.ts'

export interface Captured {
  text: string
  kind: ItemKind
  dueAt: Date | null
  recurrence: string | null
  channels: string[] | null
  urgent: boolean
  sourceText: string
}

/** Time used when a day is named without a time ("dentist tomorrow"). */
const DEFAULT_HOUR = 9

// Untimed things to do are tasks (#114). A rough stand-in for maestro/parser.py's rules: a leading
// "todo:" or a to-do verb, unless the text states a fact ("call centre number is 0800…").
const TODO_MARKER = /^\s*to\s?do\s*:\s*/i
const TODO_VERB =
  /^\s*(?:pick up|drop off|fill in|follow up|reply to|sort out|set up|buy|call|email|send|pay|renew|book|order|cancel|fix|collect|print|submit|finish|write|check|ask|get|take|schedule|update|arrange|confirm|replace)\b/i
const STATEMENT = /\b(?:is|are|was|were|means?|lives?|costs?|has|have|meets?)\b/i
const SUBORDINATOR = /\b(?:that|if|whether|when|why|how|where|what|who|which)\b/i
const LABEL = /^[^:]{1,40}:\s*\S/

function isTask(text: string, marked: boolean): boolean {
  if (marked) return true
  if (LABEL.test(text)) return false
  if (!TODO_VERB.test(text)) return false
  const said = STATEMENT.exec(text)
  if (!said) return true
  const sub = SUBORDINATOR.exec(text)
  return sub !== null && sub.index < said.index
}

const CHANNELS: [RegExp, string][] = [
  [/\b(?:text|sms) me\b/i, 'twilio_sms'],
  [/\bcall me\b/i, 'twilio_call'],
  [/\bemail me\b/i, 'email'],
  [/\b(?:on|via) telegram\b/i, 'telegram'],
]

export function parseCapture(source: string, now: Date, tz: string): Captured {
  let text = source
  const bang = text.trimStart().startsWith('!')
  if (bang) text = text.trimStart().slice(1)
  const marked = TODO_MARKER.test(text)
  if (marked) text = text.replace(TODO_MARKER, '')
  const urgent = textSaysUrgent(source)

  const take = (re: RegExp): RegExpExecArray | null => {
    const m = re.exec(text)
    if (m) text = text.slice(0, m.index) + ' ' + text.slice(m.index + m[0].length)
    return m
  }

  const channels: string[] = []
  for (const [re, name] of CHANNELS) if (take(re)) channels.push(name)

  const today = wallTime(now, tz)
  let dueAt: Date | null = null
  let recurrence: string | null = null

  const inMatch = take(/\bin (\d+) (minutes?|mins?|hours?|days?)\b/i)
  if (inMatch) {
    const n = Number(inMatch[1])
    const unit = (inMatch[2] ?? 'm').charAt(0).toLowerCase()
    const ms = n * { m: 60_000, h: 3_600_000, d: 86_400_000 }[unit as 'm' | 'h' | 'd']
    dueAt = new Date(now.getTime() + ms)
  }

  const rec = take(
    new RegExp(`\\bevery (day|weekday|week|month|${WEEKDAYS.join('|')})\\b|\\bdaily\\b`, 'i'),
  )
  const day = take(new RegExp(`\\b(?:on )?(today|tonight|tomorrow|${WEEKDAYS.join('|')})\\b`, 'i'))
  const time = take(/\b(?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\bat (\d{1,2}):(\d{2})\b/i)

  if (!dueAt && (rec || day || time)) {
    let hour = DEFAULT_HOUR
    let minute = 0
    if (time) {
      if (time[4] !== undefined) {
        hour = Number(time[4])
        minute = Number(time[5])
      } else {
        hour = (Number(time[1]) % 12) + ((time[3] ?? '').toLowerCase() === 'pm' ? 12 : 0)
        minute = Number(time[2] ?? 0)
      }
    } else if (day?.[1]?.toLowerCase() === 'tonight') {
      hour = 20
    }
    const at = (d: Ymd) => fromWallTime({ ...d, hour, minute, second: 0 }, tz)

    let date: Ymd = today
    const dayWord = day?.[1]?.toLowerCase()
    const recWord = rec?.[1]?.toLowerCase()
    const weekday = WEEKDAYS.indexOf((recWord ?? dayWord ?? '') as (typeof WEEKDAYS)[number])
    if (dayWord === 'tomorrow') date = addDays(today, 1)
    else if (weekday >= 0) date = addDays(today, (weekday - today.weekday + 7) % 7)
    // A time that has already passed today means the next one.
    if (at(date) <= now && dayWord !== 'today' && dayWord !== 'tonight') {
      date = weekday >= 0 ? addDays(date, 7) : addDays(date, 1)
    }
    if (recWord === 'weekday') while (weekdayOf(date) >= 5) date = addDays(date, 1)
    dueAt = at(date)

    if (rec) {
      const dueDay = wallTime(dueAt, tz)
      if (!recWord || recWord === 'day') recurrence = 'daily'
      else if (recWord === 'weekday') recurrence = 'weekdays'
      else if (recWord === 'month') recurrence = `monthly:${String(dueDay.day)}`
      else recurrence = `weekly:${String(dueDay.weekday)}`
    }
  }

  text = text
    .replace(/^\s*remind me (?:to |about )?/i, '')
    .replace(/^\s*to\s+/i, '')
    .replace(/\s+/g, ' ')
    .replace(/[\s,.;:-]+$/, '')
    .trim()
  text = text.charAt(0).toUpperCase() + text.slice(1)

  return {
    text,
    kind: dueAt ? 'reminder' : isTask(text, marked) ? 'task' : 'note',
    dueAt,
    recurrence,
    channels: channels.length ? channels : null,
    urgent,
    sourceText: source,
  }
}
