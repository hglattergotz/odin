# Task: wire `--platform` and `--model` CLI flags (Batch A4)

Connect config resolution to the run path; default remains Claude.

## Reference

`docs/multi-platform-agents-proposal.md` — §3, §10 (A4).

## Requirements

- `odin run` additions:
  - `--platform {claude,cursor}` — default via config/env/fallback chain
  - `--model MODEL` — platform-agnostic; maps to `claude --model` / `agent --model` when set
- Wire `config.resolve_platform` / `resolve_model` into `_cmd_run`
- Select backend via `registry.get_backend(platform)` — only `ClaudeBackend` is functional; `cursor` may error clearly "not implemented yet" OR no-op if B1 follows immediately in backlog
- Keep `--claude-bin` working (B4 decision: never remove)
- Add/adjust CLI tests in `tests/test_cli.py`

## Acceptance

- `odin run --platform claude` behaves identically to today
- `odin run --model X` passes `--model X` to claude when set
- Full suite green

## On completion

`<<<NEXT_CONTEXT>>>` with flag resolution examples and note whether `--platform cursor` is stubbed or deferred to task 006.
