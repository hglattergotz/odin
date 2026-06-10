# Task: platform-neutral contract strings (Batch C2)

## Reference

`docs/multi-platform-agents-proposal.md` — §5 (A1), §9.

## Requirements

- Parameterise `contract.build_system_prompt(branch, platform=…)` — stop hard-coding "CLAUDE.md" in runtime injected strings (`contract.py:16-17,21,24,107`)
- Use per-platform instruction name (`CLAUDE.md` / `AGENTS.md`) or generic "project instructions"
- Update `tests/test_contract.py` and `tests/test_guide.py`
- Regenerate guide protocol section if needed

## Acceptance

- Full suite green
- Cursor prepend text no longer tells agent to defer to CLAUDE.md only

## On completion

`<<<NEXT_CONTEXT>>>` with before/after snippet of injected text.
