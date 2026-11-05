# Maestro web app

The React web app for Maestro (ADR [0014](../docs/adr/0014-react-web-app.md), tracked in #109). It is built from the design reference in [`docs/design/mobile/`](../docs/design/mobile/README.md).

This is the scaffold from #100. It renders a placeholder page from static sample data and needs no backend.

## Requirements

Node 22.12 or newer. Only people working on the UI need Node; the Python package and its tests don't.

## Run

```sh
cd web-app
npm ci          # install exactly what package-lock.json pins
npm run dev     # http://localhost:5173/app/ (Vite takes the next free port if 5173 is busy)
```

## Run against a scratch backend

`npm run dev` uses mock data only. `npm run dev:api` also forwards the API routes (`/health`, `/items`) to the `maestro serve` named by `MAESTRO_API_URL`. It has no default, so it never reaches a server by accident.

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

Tests sit next to the code they cover (`App.test.tsx` beside `App.tsx`) and never touch the network.

Every component and screen gets an accessibility check: `axeViolations(container)` from `src/test/axe.ts` returns one line per [axe](https://github.com/dequelabs/axe-core) violation, and tests expect it to be empty. jsdom has no layout, so that check leaves out colour contrast. Check contrast in a real browser.

## Tool versions

- **TypeScript 6.0, not 7:** typescript-eslint supports TypeScript `<6.1` so far.
- **ESLint 9, not 10:** eslint-plugin-jsx-a11y supports ESLint up to 9 so far.

Raise them when those plugins catch up.
