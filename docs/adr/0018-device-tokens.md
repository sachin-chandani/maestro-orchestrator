# 0018: A phone signs in once and keeps a token of its own

Status: Accepted (2026-11-12). Tracking: #108; QR pairing builds on this in #95.

## Context
The API has one bearer token, `MAESTRO_API_TOKEN`, and the current web page keeps it in the browser's `localStorage`. On a phone that is the wrong trade: nobody wants to paste a token every day, but the one secret that opens everything shouldn't live in every browser either. Losing a phone would mean rotating the token and signing in again on every client. HttpOnly cookie sessions would hide the token from scripts but add CSRF handling and a second auth path beside bearer, and get in the way of the service worker that web push (#24) needs.

## Decision
- **Device tokens.** `POST /devices {name}` with the master token answers once with a new random token (`mdt_…`, 256 bits). The app stores that and forgets the master token. Only the token's SHA-256 is stored server side, so a copy of the database can't sign in.
- **Same auth check.** `require_token` accepts the master token (constant-time compare) or a live device token (indexed lookup of its hash). Every existing route gets this for free; a wrong device token counts towards the same lockout as a wrong master token.
- **Devices can't make devices.** `/devices` itself needs the master token. A stolen phone can't mint a spare key. `GET /devices` is the exception, so a device can see the list and which entry is itself.
- **Revoke, don't expire.** Device tokens have no timed expiry; nothing re-prompts on a schedule. `DELETE /devices/{id}` or `maestro devices revoke <id>` stops one token at once and for good. Rotating `MAESTRO_API_TOKEN` leaves devices signed in.
- **On the phone.** The device token lives in `localStorage` for now (IndexedDB when the service worker needs it). The app clears it only on a genuine 401, never on a network error, and then shows sign-in. Offline, rate-limited and server errors keep the token and the last loaded list.
- **Pairing later.** #95 adds a short-lived code shown by `maestro pair` so a phone can sign in without ever seeing the master token; it ends in the same `POST /devices`.

## Consequences
- A `devices` table (SQLite schema 14, PostgreSQL 7); `maestro devices list|add|revoke`; metric `maestro_devices_total{action}`.
- `last_seen_at` is written at most once an hour per device, so authenticating a request is normally a read alone.
- The legacy page at `/` keeps using the master token directly until it is removed; both schemes work side by side.
- An XSS in the app would still expose the device token, as it would any client-side secret. The app has one origin and no third-party scripts, and the exposure is one revocable device rather than the master key.
