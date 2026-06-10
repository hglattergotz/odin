# Task: Cursor stream display for tool_call events (Batch B3)

Depends on task 006.

## Reference

`docs/multi-platform-agents-proposal.md` — §6 (live display).

## Requirements

- Extend `CursorBackend.handle_stream_event` (or shared renderer) for `tool_call` started/completed events
- Map `readToolCall`, `editToolCall`, `writeToolCall`, `shellToolCall` to one-line activity summaries (mirror Claude tool lines in `runner.py`)
- Ignore `thinking` events for display
- Tests for event rendering (StringIO sink, like existing runner display tests)

## Acceptance

- Full suite green; no change to protocol routing

## On completion

`<<<NEXT_CONTEXT>>>` with example rendered lines.
