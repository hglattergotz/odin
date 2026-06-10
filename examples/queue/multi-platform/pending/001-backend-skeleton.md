# Task: add AgentBackend package skeleton (Batch A1)

Implement the backend abstraction **without changing runtime behaviour** yet.

## Reference

`docs/multi-platform-agents-proposal.md` — §2 (design), §10 (A1).

## Requirements

- Add `src/odin/backends/`:
  - `base.py` — `AgentBackend` protocol/ABC, `AgentInvokeSpec` dataclass
  - `claude.py` — `ClaudeBackend` stub (methods may delegate to existing code in a follow-up task)
  - `registry.py` — `get_backend(name: str) -> AgentBackend`, default `"claude"`
- No changes to `cli.py` or the live invoke path in this task — skeleton + registry only.
- Add `tests/test_backends.py` with minimal registry tests (unknown platform raises; `"claude"` resolves).

## Acceptance

- `uv run pytest tests/test_backends.py` passes.
- Full suite still green.
- No user-visible behaviour change.

## On completion

Emit `<<<NEXT_CONTEXT>>>` summarising files added and the `AgentBackend` method signatures you defined.
