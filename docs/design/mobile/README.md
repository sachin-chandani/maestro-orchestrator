# Mobile design reference

The target phone experience for v0.5, as four prototype screens. They are the reference for the React app in `web-app/` (ADR [0014](../../adr/0014-react-web-app.md)); build from this spec, not by copying the prototype markup.

The `.dc.html` files are design-canvas sources: they open in the design canvas they were made with, not directly in a browser. Read them for layout, copy, colours and behaviour.

| File | Screen | Issues |
|---|---|---|
| [Pair.dc.html](Pair.dc.html) | Connect a phone: scan the code from `maestro pair`, allow notifications, add to the home screen | #95, #96, #24 |
| [Today.dc.html](Today.dc.html) | The app: Today, Lists and Notes tabs, an always-on capture bar with a live preview, Done and Snooze on each reminder. Interactive, with sample data. | #93, #94, #22, #88 |
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

## Behaviour the prototype shows

- **Capture bar**: fixed above the tabs on every screen. While typing, a row of chips shows what Maestro understood ("Tomorrow", "9:00 am", "Text me", "Every week", "1 hour before", "Urgent", "List: shopping", or "Note, no time"). Submitting routes the capture: list additions to Lists, untimed text to Notes, everything else to Today.
- **Reminder card**: time, text, then chips for recurrence, channel, urgency, lead time, escalation and streak. **Done** shows what it cancelled ("Any pending SMS or call for it is cancelled"); **+1 hour** says when it comes back and that escalation restarts from then. Urgent, not-done cards get an orange border.
- **Groups**: Today, Tomorrow, Later, in the user's timezone (#22).
- **Header**: the screen title, a summary ("3 left today") and a connection pill for the server.
- **Lists**: checkboxes with a count of what's left.
- **Notes**: search on top, each note with where it came from ("From a phone call · last week").
- **Pairing**: the phone gets its own key, revocable without changing the server's main token.
- **HTTPS**: installing and push both need it; the Reach screen names three ways to get it.
