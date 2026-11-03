# 0014: The web UI becomes a React app, built in CI and served by `maestro serve`

Status: Accepted (2026-11-03). Supersedes the "Server-rendered (Jinja + HTMX), no build step" row of the ROADMAP architecture table.

## Context
The web UI is one hand-written page (`maestro/web/index.html`). The v0.5 phone-first work (#93–#98, #24) needs tabs, offline captures, a live parse preview, pairing and settings screens, and a service worker. That is an app, not a page, and server-rendered fragments would make offline use and optimistic updates hard. The design reference is in `docs/design/mobile/`.

## Decision
- A React + TypeScript app lives in `web-app/`, built with Vite. Vitest for unit tests; the e2e tests (#23) drive the built app.
- It is developed first against **mock data** behind one API client interface, so screens can be built before their endpoints exist. Switching a screen from mocks to the real API changes the client, not the screen.
- The API client's types come from the FastAPI OpenAPI schema, so a backend change that breaks the app fails CI.
- CI builds the app; the wheel and the Docker image ship the built files, and `maestro serve` serves them. Python users never need Node. Running from a source checkout without a build falls back to the current page.
- The current page stays at `/` until the React app reaches parity, then is removed.

## Consequences
- Contributors working on the UI need Node; everyone else doesn't.
- CI gains a Node job (lint, type check, tests, build) and the Docker build gains a build stage.
- Dependency audits now cover npm packages too.
- The same components could later back a native shell if the mobile spike (#27) goes that way.
