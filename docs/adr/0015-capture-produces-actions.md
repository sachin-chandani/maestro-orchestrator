# 0015: Capture produces typed actions; a language model is an optional interpreter

Status: Accepted (2026-11-07). Settles the direction of #33. Tracking: #120.

## Context
Every capture path calls the rule-based `parse()` and saves one item. That is fast, free, offline and well tested (a 217-phrase corpus at 98.6%), but it can't read messy speech, several requests in one message ("book flights, add sunscreen to the packing list, the hotel code is 4471"), or the dates hidden in a forwarded booking email. Language models are good at exactly that. They also misread, cost money, add latency, and with a hosted provider they send the user's text off their server.

## Decision
- **Actions are the contract.** Every capture becomes a list of typed actions (`CreateReminder`, `CreateTask`, `CreateNote`, `AddToList`, `Done`, `Snooze`, `Stop`, `Ask`, …) with one JSON schema. One function applies them to the store. Nothing else turns text into store calls.
- **Interpreters produce actions.** `RulesInterpreter` (today's parser) is the default. `ModelInterpreter` is optional and off by default. It uses the user's own key for Anthropic or any OpenAI-compatible endpoint, including local models through Ollama, so nothing has to leave the server.
- **The model proposes; Maestro decides.** Model output is validated against the schema. Anything invalid falls back to the rules, and so does a timeout or an error, so a capture is never lost. The model never calls the store, channels or network. Message content, including forwarded emails, is data, never instructions.
- **Ask when unsure.** Unsure interpretations become proposals that the user confirms in the channel they used, and every capture can be undone for a short time.
- **Three kinds of things:**
  - **reminders**: time-sensitive, delivered and escalated
  - **tasks**: to do, with no time, never notified
  - **notes**: information to keep, organised into tracks
- **The rest is unchanged.** Scheduling, delivery, escalation, quiet hours and acknowledgements stay deterministic. The model is never in the delivery path.

## Consequences
- One refactor (#113) routes every channel through `interpret` and `apply`, and later features plug in there.
- Self-hosting stays the default and the selling point. Using a hosted provider is an explicit choice, and `maestro doctor` tells the user plainly what leaves the server.
- Tests never call a model. Provider responses are recorded fakes behind an injectable transport (ADR 0002).
- The parser corpus remains the measure of the rules. Model behaviour is covered by schema validation and fallback tests, not by grading models.
