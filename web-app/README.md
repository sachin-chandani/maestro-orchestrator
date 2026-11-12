# Maestro web app

The React web app for Maestro (ADR [0014](../docs/adr/0014-react-web-app.md), tracked in #109). It is built from the design reference in [`docs/design/mobile/`](../docs/design/mobile/README.md).

It shows today's reminders (#101, #102) through one API client with two implementations: `MockClient` (sample data, in the browser) and `HttpClient` (`maestro serve`). Screens get the client from `ClientProvider` and never know which one it is.

| How you run it               | Client                                            |
| ---------------------------- | ------------------------------------------------- |
| `npm run dev`                | MockClient                                        |
| `npm run dev:api`            | HttpClient, through the proxy below               |
| `npm run build` (production) | HttpClient, against the server that serves `/app` |
| `VITE_MAESTRO_MOCK=1` or `0` | Forces MockClient or HttpClient                   |

**The list (#22, ADR 0017).** One screen: Needs a look (only when something does), Next up (the very next occurrence, shown big, whatever its day), Today, Tomorrow, This week (collapsed to "N more ›"), Anytime (tasks, oldest first), then "N more reminders after this week". It comes from `GET /feed`, one call for the next 7 days with recurring series expanded (one row per day; only the first occurrence takes Done and +1 hour, ADR 0009), snoozed ones at their snoozed time and delivered ones waiting for an answer at the time they went out. The client groups rows by calendar day in the server's timezone (`src/app/feedGroups.ts`), so the list rolls over at midnight on the minute refresh without new data. A reading that needs a look has a dashed chip naming the gap, what you said, and two buttons: **Answer…** opens a sheet (#128): what you said, three suggested times with a reason each from `GET /needs-look/{id}/suggestions` (one tap fills the gap with that exact time), **Pick a time…** for a typed phrase (`POST /needs-look/{id}`; a phrase that doesn't answer shows the server's sentence and keeps what you typed), **No time · keep it** (`POST /needs-look/{id}/keep`, the reading stays a task under Anytime) and **Dismiss** (`DELETE /needs-look/{id}`). Keyboard: `/` capture, `j`/`k` move, `d` done, `s` +1 hour, `a` answer, `?` lists them; all off while typing.

**Capturing (#104, ADR 0017).** Submitting the bar saves at once — the list is the preview. A status line above the bar says what happened ("Added 2. 'Call the plumber' needs a time."), the new rows carry a thin accent line that runs out over 10 seconds, and **Undo** (with a countdown) takes back exactly what that capture added. The 10 seconds are only how long the line stays on screen: the server's undo window is 10 minutes, and a late tap is answered with "Too late to undo". A capture made while the server is unreachable is kept in an in-memory outbox — "Saved on this phone, will send when online", counted on the connection pill — and sent, oldest first, as soon as a request succeeds; a reload loses the outbox until the service worker (#24) gives it a home. `src/test/sentences.ts` is the shared fixture of example sentences the mock and the screens both rehearse.

**Signing in (#108, ADR 0018).** The first time the app meets a 401 it asks for the server's token (`MAESTRO_API_TOKEN`), sends it once to `POST /devices`, and keeps the device token it gets back in `localStorage` under `maestro.device_token`. The server's token is never stored. A 401 later (the device was revoked, or the server was reset) clears the device token and shows sign-in again, saying so; being offline or rate-limited never signs the app out. On the mock, `new MockClient({ masterToken })` makes the wrong-token path reachable.

**Staying current.** The list is fetched again every minute while the tab is visible and whenever it becomes visible, so a sent reminder shows as awaiting and the groups roll over at midnight. A 429 pauses requests for its `Retry-After` and resumes by itself. Polling, not server-sent events: the backend has no streaming endpoint and a reminder list changes on the scale of minutes.

In development, **Dev tools** at the bottom of the page switch client and, on sample data, reset it, delay every call, or make calls fail (unreachable, 500, 401, 429), to try loading and error states. Sample data is kept in this browser's localStorage. Dev tools and the gallery are left out of production builds.

## Requirements

Node 22.12 or newer. Only people working on the UI need Node; the Python package and its tests don't.

## Run

```sh
cd web-app
npm ci          # install exactly what package-lock.json pins
npm run dev     # http://localhost:5173/app/ (Vite takes the next free port if 5173 is busy)
```

## Run against a scratch backend

`npm run dev` uses mock data only. `npm run dev:api` also forwards the API routes (`/health`, `/items`, `/capture`, `/devices`, `/needs-look`, `/captures`) to the `maestro serve` named by `MAESTRO_API_URL`. It has no default, so it never reaches a server by accident.

Start the backend with a throwaway database, never `~/.maestro/maestro.db`, on a free port:

```sh
# terminal 1, from the repository root
MAESTRO_DB="$(mktemp -d)/maestro.db" MAESTRO_API_TOKEN=dev-token maestro serve --port 8002

# terminal 2
cd web-app
MAESTRO_API_URL=http://127.0.0.1:8002 npm run dev:api
```

Then `http://localhost:5173/health` (or whichever port Vite prints) answers from the scratch server.

## Check and build

```sh
npm run check         # everything below except build, in order; run before pushing
npm run lint          # ESLint: type-aware TypeScript rules, React hooks, JSX accessibility (jsx-a11y)
npm run format:check  # Prettier; `npm run format` rewrites files
npm run typecheck     # strict TypeScript, no emit
npm test              # Vitest + Testing Library in jsdom; `npm run test:watch` while working
npm run build         # type check, then build to maestro/web/app/, served by `maestro serve` at /app
npm run preview       # serve the built files locally at /app/
```

## Tests

Tests sit next to the code they cover (`App.test.tsx` beside `App.tsx`) and never touch the network. `src/api/contract/contract.test.ts` runs the same scenarios against both clients, `HttpClient` through an in-process fake of `maestro/api.py`, so a screen built on sample data behaves the same against a server.

One test does reach a server, by choice: `src/api/contract/live.test.ts` runs `HttpClient` against a real `maestro serve` when `MAESTRO_LIVE_URL` and `MAESTRO_LIVE_TOKEN` are set, and is skipped otherwise. Start the server on a scratch database first (see above); the test removes everything it adds.

Every component and screen gets an accessibility check: `axeViolations(container)` from `src/test/axe.ts` returns one line per [axe](https://github.com/dequelabs/axe-core) violation, and tests expect it to be empty. jsdom has no layout, so that check leaves out colour contrast. Check contrast in a real browser.

## Components

`src/components/` holds the core components from the design (#102): `Chip`, `ReminderCard`, `CaptureBar`, `TabBar`, `ConnectionPill`, `Toast` and `EmptyState`. They take props and report back through callbacks; fetching data is the screens' job. With `npm run dev`, open `/app/?gallery` to see each one in every state, on mock data. The gallery is left out of production builds.

## Styles

`src/styles/tokens.css` holds the design tokens from [`docs/design/mobile/README.md`](../docs/design/mobile/README.md) as CSS variables, with a dark theme that follows the system setting. Other stylesheets use the variables only: `src/styles/tokens.test.ts` fails on a raw colour or an unknown variable, and checks every text and focus colour pair for WCAG AA contrast in both themes.

Fonts: IBM Plex is named first and used where it is installed; otherwise the system font shows. Nothing is loaded from a font service.

## Tool versions

- **TypeScript 6.0, not 7:** typescript-eslint supports TypeScript `<6.1` so far.
- **ESLint 9, not 10:** eslint-plugin-jsx-a11y supports ESLint up to 9 so far.

Raise them when those plugins catch up.
