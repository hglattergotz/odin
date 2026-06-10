# Task: config loader for platform and model (Batch A2)

Add read-only configuration loading per the proposal.

## Reference

`docs/multi-platform-agents-proposal.md` — §3 (resolution order), §4 (config schema), §10 (A2).

## Requirements

- Add `src/odin/config.py`:
  - Load `$ODIN_CONFIG` or `$ODIN_HOME/config.toml` (default `~/.odin/config.toml`) via stdlib `tomllib`
  - Missing file → empty defaults (platform falls back to `"claude"`, model unset)
  - `resolve_platform(cli_flag, env ODIN_PLATFORM) -> str`
  - `resolve_model(cli_flag, env ODIN_MODEL, platform) -> str | None`
  - Resolution order exactly as proposal §3
- **Read only** — no writer in this task (A2b adds `odin config`)
- Add `tests/test_config.py` for load + resolution (use tmp_path config files)

## Acceptance

- Tests cover: no config file, partial TOML, flag/env overrides
- Full suite green
- `odin run` behaviour unchanged (config not wired to CLI yet)

## On completion

`<<<NEXT_CONTEXT>>>` with public API of `config.py` and example default TOML shape.
