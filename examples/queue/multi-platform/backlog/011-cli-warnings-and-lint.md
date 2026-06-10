# Task: platform-aware startup warnings and lint (Batch C3)

## Reference

`docs/multi-platform-agents-proposal.md` — §1 (`cli.py:313`, `:747`), §7, §9.

## Requirements

- `cli.py:313-318`: warn based on active platform's instruction files (`CLAUDE.md` vs `AGENTS.md` / `.cursor/rules`)
- Generalise `_warn_claude_md_conflicts` → platform-aware path (`cli.py:747-761`)
- `lint.scan_project_instructions(path, platform)` (rename/generalise `scan_claude_md`)
- Tests for both platforms' warning paths

## Acceptance

- AGENTS.md-only project + `--platform cursor` → no spurious CLAUDE.md warning
- Full suite green

## On completion

`<<<NEXT_CONTEXT>>>` with warning matrix (platform × files present).
