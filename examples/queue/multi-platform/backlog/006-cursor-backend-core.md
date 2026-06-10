# Task: CursorBackend invoke + result normalisation (Batch B1–B2)

**Promote from backlog only after tasks 001–005 are done and the suite is green.**

Re-read `docs/multi-platform-agents-proposal.md` §2, §5, §6, and Appendix C before starting — confirm nothing drifted.

## Requirements

- Implement `CursorBackend` in `src/odin/backends/cursor.py`:
  - `build_invoke`: `agent -p --output-format stream-json --force --trust --workspace …` + optional model/sandbox/approve_mcps from config/flags
  - **Protocol prepend** (no `--append-system-prompt`) with ODIN_PROTOCOL delimiters
  - **`normalise_result`** with pinned Cursor success predicate (§2):
    `exit_code == 0 and terminal_result_present and event.get("is_error") is not True and bool(final_text)`
  - Token usage mapped to internal keys: `input`, `output`, `cache_read`, `cache_creation`
  - `cost_usd: null` (no CLI cost field)
- Wire `--platform cursor` to use `CursorBackend`

## Acceptance

- Fake-agent tests (next task) pass; manual smoke with real `agent` optional
- Claude path unchanged

## On completion

`<<<NEXT_CONTEXT>>>` with example argv and sample normalised `RunResult`.
