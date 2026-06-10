# Task: platform-aware metrics (Batch C1)

Depends on Batch B (or can run after 006 if metrics fields are wired in normalise_result).

## Reference

`docs/multi-platform-agents-proposal.md` — §8 (R3-2 null cost).

## Requirements

- Task records: add `platform`; rename `claude_duration_ms`/`claude_api_ms` → `agent_duration_ms`/`agent_api_ms` (accept old names when reading)
- **`RunAccumulator`:** add `_any_cost` flag in `record_task`; emit `cost_usd_total: null` in `finish` when no numeric cost seen (not `0.0`)
- `_norm_usage` accepts backend-normalised token dict OR maps per-platform keys via config
- Update `odin metrics` text/html renderers if needed for null cost totals
- Tests in `tests/test_metrics.py`

## Acceptance

- Full suite green; backward compatible JSONL read

## On completion

`<<<NEXT_CONTEXT>>>` with sample task/run JSONL lines for claude vs cursor.
