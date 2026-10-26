# 0008: Trust an email's sender only with DMARC from the receiving server

**Status:** Accepted (#9)

## Context
Email-in captures whatever an allowed sender writes. The `From` header is chosen by whoever sends the message, so an allowlist alone lets anyone who knows the owner's address create reminders.

## Decision
- A message is captured only if its `From` address is in `MAESTRO_EMAIL_FROM` **and** the topmost `Authentication-Results` header written by **your provider** reports `dmarc=pass`. The provider is named by its authserv-id (`MAESTRO_EMAIL_AUTHSERV_ID`, the first word of the header, e.g. `mx.google.com`), which is required while the check is on.
- Why the id: the provider adds its header above everything the sender wrote, so the first header carrying the provider's id is genuine, even if the sender forged one with the same id further down. Without the id, a provider that adds no header at all would leave a forged header topmost. (Found in review of the first version.)
- `MAESTRO_EMAIL_REQUIRE_DMARC=false` turns the check off for private mail servers that don't add the header; `maestro doctor` warns when it's off.
- Ignored messages are marked read; messages that fail unexpectedly stay unread and are retried, up to 3 polls, then given up on (`maestro_email_given_up_total`). Captures are idempotent per `Message-ID`, or a hash of the message when there is none (IMAP UIDs are reused across folders and mailbox rebuilds).
- IMAP runs in a background worker, one session per poll, so a slow mail server never delays the scheduler or its lease.

## Consequences
- Senders whose domain has no DMARC policy can't capture by email unless the check is disabled. Most mail providers (Gmail, Outlook, iCloud, Fastmail) publish DMARC.
- The inbox should be dedicated to Maestro: anything an allowed sender sends to it becomes an item.
