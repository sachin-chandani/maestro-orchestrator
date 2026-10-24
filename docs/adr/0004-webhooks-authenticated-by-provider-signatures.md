# 0004: Authenticate webhooks by provider signatures

**Status:** Accepted (#6)

## Context
Providers such as Twilio call our webhooks but can't send our bearer token. Leaving webhooks unauthenticated would let anyone create or delete items by posting to them.

## Decision
- Each webhook verifies the provider's signature (Twilio: HMAC-SHA1 over URL and sorted parameters) with a constant-time comparison, and rejects failures with 403.
- The signed URL is reconstructed from `MAESTRO_PUBLIC_URL` when set, since proxies change the host and scheme the app sees.
- Even with a valid signature, only the owner's identity (phone number, chat id) is acted on.
- A webhook whose provider isn't configured returns 404.

## Consequences
- Misconfigured `MAESTRO_PUBLIC_URL` shows up as 403s; the warning log includes the URL that was checked, and `maestro doctor` prints the URL to configure.
- Webhooks are public routes and need rate limiting (#54).
