# Task: refactor runner to generic loop + ClaudeBackend (Batch A3)

Move platform-specific logic out of `run_claude` while keeping Claude behaviour identical.

## Reference

`docs/multi-platform-agents-proposal.md` — §2 (refactor scope, success predicates), §10 (A3).

## Requirements

- Refactor `src/odin/runner.py`:
  - Generic subprocess loop stays here (stdin, stderr drain thread, NDJSON loop, wall timing)
  - `run_agent(prompt, project_dir, backend, ...)` replaces direct `run_claude` usage
  - `ClaudeBackend` implements: `build_invoke`, `handle_stream_event`, `normalise_result`
  - Move Claude argv building (`--append-system-prompt`, permission flags, etc.) into `ClaudeBackend`
  - **Claude success predicate** (relocated from `runner.py:174-179`):
    `exit_code == 0 and error is None and stop_reason in {end_turn, stop_sequence} and bool(final_text)`
- **Switch `cli.py:630` to `run_agent(..., backend=ClaudeBackend())`** — tests must pass before A4
- Fix stale `runner.py` docstring (`:5-6`) and generic stderr label (`:170` → not hard-coded `[claude stderr]`)
- Extend `tests/test_runner.py` — all existing scenarios still pass via fake claude script

## Acceptance

- Zero behaviour diff for default `odin run` (still Claude)
- Full `uv run pytest` green

## On completion

`<<<NEXT_CONTEXT>>>` noting `run_agent` signature and what moved vs stayed in runner.
