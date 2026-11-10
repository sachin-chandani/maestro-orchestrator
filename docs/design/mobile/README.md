# Mobile design reference

The target phone experience for v0.5. It is the reference for the React app in `web-app/` (ADR [0014](../../adr/0014-react-web-app.md)). Build from this spec, not by copying prototype markup.

**This spec wins over the prototypes.** The first section below (ADR [0017](../../adr/0017-save-first-ask-later.md), 10 Nov 2026) replaces the tabs, the live preview chips and the confirm-before-saving flow that `Today.dc.html` shows. The prototypes are still the reference for tokens, the reminder row, pairing, notifications and HTTPS.

## The app: say it once, and it's on your list

### Principles
- **Unified.** One kind of thing, a reminder, in one list ordered by time. A task is a reminder with no time, under Anytime. Notes live in search.
- **Conversational.** Maestro answers in one sentence ("Added 2. One needs a time.") and the motion shows where each reminder went.
- **Universal.** Speak, type, share from another app, or use a screen reader. Everything is in reach of one thumb.
- **Iconic.** One big action, the mic. Nothing else on the screen competes with it.

### Rules
- **Never block.** Sure reminders save at once. Undo is cheaper than asking, so the app never asks before saving.
- **Ask later, in place.** An unsure reminder is saved with its gap shown ("Call the plumber · when?"). One tap fills it, whenever you like.
- **Calm by default.** The next thing is big, today is normal, later days collapse. Nothing is red unless it's urgent.

### Screens
1. **The list** (#22). Header: the date, search, settings. Groups, in order: Needs a look (only when it has entries), Next up (one reminder, larger, accent border), Today, Tomorrow, This week (collapsed, "4 more ›"), Anytime. Each row: time in Plex Mono, text, and one quiet meta line ("in 2 h · text", "every Tue"). At the bottom, a 58px round mic button with "hold to talk · tap to type" under it. No tab bar.
2. **Speaking** (#127). The list dims behind a sheet with a live wave and the words as they come. "Let go to add · slide away to cancel."
3. **Just added** (#104). The new reminders slide into their places and stay highlighted with a thin accent line that runs out over 10 seconds. Unsure ones appear in Needs a look. A dark status line at the bottom: "Added 2 · 1 needs a time", a countdown, and Undo.
4. **Filling a gap** (#128). "You said 'sometime next week'". Three suggested times with a reason ("Mon 16 Nov, 09:00 · start of the week"), Pick a time…, and "No time, keep it under Anytime". One tap saves it and it moves into the list.
5. **A reminder** (#21). What you said, how it was read, and what will happen in time order (heads-up, delivery, escalation). Done, +1 hour, Edit.

### The first ten seconds
| Time | What happens |
|---|---|
| 0 s | You let go. The words go to your server, which splits them into reminders and judges each one. |
| 0.5 s | Sure ones slide into the list at their time; unsure ones go to Needs a look. All of them are already saved. |
| 0–10 s | One line says what happened, with Undo and a countdown. Undo removes exactly what this sentence added. |
| 10 s | The line goes and the highlight fades. They're just reminders; each can still be deleted from its screen. |

The server never waits for the 10 seconds. If it did, closing the app during the countdown would lose the reminder.

### Dictation
1. The keyboard's own mic always works, with no code.
2. The mic button uses the browser's speech recognition where it exists, for live words. It's off until turned on in Settings, which says where audio goes (Chrome: Google, Safari: Apple).
3. A speech model on your own server may come later, off by default.

The mic needs a secure page, so HTTPS (#96) comes early.

## Prototype screens

The `.dc.html` files are design-canvas sources: they open in the design canvas they were made with, not directly in a browser. Read them for layout, copy, colours and behaviour, except where the section above replaces them.

| File | Screen | Issues |
|---|---|---|
| [Pair.dc.html](Pair.dc.html) | Connect a phone: scan the code from `maestro pair`, allow notifications, add to the home screen | #95, #96, #24 |
| [Today.dc.html](Today.dc.html) | The earlier app design: tabs and a live preview, replaced by the section above. Still the reference for the reminder row and Done/Snooze. | #22, #104 |
| [Notify.dc.html](Notify.dc.html) | The lock screen: a push with Done/Snooze, an SMS fallback, a Telegram message, the quiet-hours rule | #24, #12, #13 |
| [Reach.dc.html](Reach.dc.html) | How a self-hosted server reaches the phone: the secure link, the server, delivery services | #96 |

## Design tokens

| Token | Value | Use |
|---|---|---|
| `ground` | `#F3F4F6` | App background |
| `surface` | `#FFFFFF` | Cards, bars |
| `line` | `#E5E7EB` | Borders between surfaces; `#D1D5DB` for controls |
| `ink` | `#111827` | Main text |
| `muted` | `#4B5563` | Secondary text (passes 4.5:1 on `surface` and `ground`) |
| `accent` | `#2F5BEA` | Primary actions, current tab, focus |
| `urgent` | `#9A3A0B` on `#FFF1E6` | Urgent reminders and escalation chips |
| `info` | `#2A3FA8` on `#EEF2FF` | Time, date and lead-time chips |
| `ok` | `#166534` on `#ECFDF3` | Connected, streaks |
| `list` | `#115E59` on `#F0FDFA` | List chips |
| Type | IBM Plex Sans 400/500/600/700; IBM Plex Mono 500 for times and commands | |
| Radius | 14px cards, 12px inputs and bars, 10px buttons, 6px chips | |

Touch targets are at least 44px. Colours that must be told apart also differ in lightness.

## Behaviour the prototypes show (earlier design)

- **Capture bar**: fixed above the tabs on every screen. While typing, a row of chips shows what Maestro understood ("Tomorrow", "9:00 am", "Text me", "Every week", "1 hour before", "Urgent", "List: shopping", or "Note, no time"). Submitting routes the capture: list additions to Lists, untimed text to Notes, everything else to Today.
- **Reminder card**: time, text, then chips for recurrence, channel, urgency, lead time, escalation and streak. **Done** shows what it cancelled ("Any pending SMS or call for it is cancelled"); **+1 hour** says when it comes back and that escalation restarts from then. Urgent, not-done cards get an orange border.
- **Groups**: Today, Tomorrow, Later, in the user's timezone (#22).
- **Header**: the screen title, a summary ("3 left today") and a connection pill for the server.
- **Lists**: checkboxes with a count of what's left.
- **Notes**: search on top, each note with where it came from ("From a phone call · last week").
- **Pairing**: the phone gets its own key, revocable without changing the server's main token.
- **HTTPS**: installing and push both need it; the Reach screen names three ways to get it.
