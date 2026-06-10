# Task: Cursor CLI flags + fake-agent integration tests (Batch B4–B5)

Depends on tasks 006–007.

## Requirements

- CLI flags for Cursor platform: `--agent-bin`, `--force`, `--trust`, `--sandbox`, `--approve-mcps` (warn/ignore when platform is claude)
- Extend `tests/test_runner.py` pattern: fake `agent` shell script with `FAKE_AGENT_SCENARIO` env var emitting Cursor-shaped NDJSON (completed, held, invalid model / no result, tool_call stream)
- Dry-run prints resolved backend + argv from `build_invoke` (not hard-coded `claude -p`) — proposal C5 can land here or in task 011

## Acceptance

- End-to-end test: `--platform cursor` + fake agent → completed sentinel → queue `done/`
- Full suite green

## On completion

`<<<NEXT_CONTEXT>>>` with fake-agent scenario list.
