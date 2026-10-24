# 0005: Anything that isn't exactly a command is captured

**Status:** Accepted (#6, #8)

## Context
Chat and SMS share one text box for capturing and for commands. A loose command parser would swallow captures: "done with taxes by friday" is a reminder, not a request to mark something done.

## Decision
- Commands match only exact shapes (`done 3`, `snooze 1h`, `list`); everything else is captured.
- Durations in commands accept only units `parse_duration` understands, so "snooze 1 soon" is not a command.
- Without an item number, `done` and `snooze` act on the reminder last delivered on that channel. `rm` is never inferred.

## Consequences
- Users occasionally need the exact form; `help` lists it.
- New commands must keep to exact shapes and get "not a command" test cases.
