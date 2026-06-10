# Task: interactive `odin config` command (Batch A2b)

Add the user-facing config setter — the **only** writer of `~/.odin/config.toml`.

## Reference

`docs/multi-platform-agents-proposal.md` — §4 (`odin config`), §10 (A2b).

## Requirements

- Subcommand: `odin config` with:
  - `odin config` (no args) — interactive menu on TTY (reuse patterns from `prompts.py`): set `default_platform`, per-platform `model`, etc.
  - `odin config show` — print effective config
  - `odin config get KEY` — e.g. `platforms.claude.model`
  - `odin config set KEY VALUE` — non-interactive
- **Hand-rolled minimal TOML writer** (stdlib `tomllib` is read-only) — no new dependencies
- Atomic write (temp file → rename)
- Merge updates without clobbering unrelated keys
- Model picker: curated suggestions + "Other (type value)" + "Use platform default (unset)"
- Extend `tests/test_config.py`: round-trip set/get, hand-rolled writer output

## Non-goals

- Do not auto-create config during `odin run`
- Do not validate model names against live CLI lists (accept free text)

## Acceptance

- Full suite green
- `odin config set platforms.claude.model sonnet` persists and `odin config get` returns it

## On completion

`<<<NEXT_CONTEXT>>>` with subcommand usage examples.
