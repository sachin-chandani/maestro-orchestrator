# 0008: Trust an email's sender only with DMARC from the receiving server

**Status:** Accepted (#9)

## Context
Email-in captures whatever an allowed sender writes. The `From` header is chosen by whoever sends the message, so an allowlist alone lets anyone who knows the owner's address create reminders.

## Decision
- A message is captured only if its `From` address is in `MAESTRO_EMAIL_FROM` **and** the topmost `Authentication-Results` header reports `dmarc=pass`.
- Only the topmost header counts: the receiving mail provider adds it last, above everything else. Headers further down were written by earlier hops, including the sender, and are ignored.
- `MAESTRO_EMAIL_REQUIRE_DMARC=false` turns the check off for private mail servers that don't add the header; `maestro doctor` warns when it's off.
- Ignored messages are marked read; messages that fail unexpectedly stay unread and are retried. Captures are idempotent per `Message-ID`.

## Consequences
- Senders whose domain has no DMARC policy can't capture by email unless the check is disabled. Most mail providers (Gmail, Outlook, iCloud, Fastmail) publish DMARC.
- The inbox should be dedicated to Maestro: anything an allowed sender sends to it becomes an item.
