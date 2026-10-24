# Architecture decision records

Short records of decisions that constrain future work: the context, what was decided, and what it costs.
Add a new numbered file for each decision; supersede rather than edit old ones.

| # | Decision | Status |
|---|---|---|
| [0001](0001-store-timestamps-in-utc.md) | Store timestamps in UTC, compute in the user's timezone | Accepted |
| [0002](0002-stdlib-http-transports.md) | Talk to providers over stdlib HTTP with injectable transports, no SDKs | Accepted |
| [0003](0003-single-threaded-sqlite.md) | One SQLite connection, used from one thread | Accepted |
| [0004](0004-webhooks-authenticated-by-provider-signatures.md) | Authenticate webhooks by provider signatures, not the API token | Accepted |
| [0005](0005-capture-by-default.md) | Anything that isn't exactly a command is captured | Accepted |
