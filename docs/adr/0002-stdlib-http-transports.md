# 0002: Stdlib HTTP with injectable transports, no provider SDKs

**Status:** Accepted (#5, #6, and the ntfy and Twilio channels)

## Context
Each provider (Twilio, Telegram, ntfy, later SMTP and S3) ships an SDK. Pulling them in grows the install, the attack surface and the dependency audit, and makes tests depend on network mocking libraries.

## Decision
- Channels and clients build requests with `urllib` and take an injectable `opener` / `transport` callable.
- Tests pass fakes that record requests; no test touches the network.
- Where a protocol detail is subtle (webhook signatures), we cross-check our implementation against the official SDK once, outside the project, and keep the SDK-computed values as fixed test vectors.

## Consequences
- The core install depends only on `dateparser`; the server adds FastAPI and uvicorn.
- We own a little protocol code per provider and must read their docs carefully.
- Real credentials are only exercised by `maestro doctor --send-test`.
