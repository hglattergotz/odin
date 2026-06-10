# Task: guide, docs, dry-run, demo notes (Batch C4–C6)

## Requirements

- Add `examples/target-agents-md-snippet.md`
- `odin guide agent-md` topic (or extend guide) for cross-platform instruction files
- Update Odin repo `CLAUDE.md`: multi-platform architecture + **two write surfaces** (metrics + user-initiated config via `odin config`) — owner-approved wording from proposal B1
- Ensure dry-run uses `backend.build_invoke` if not done in task 008
- Demo (`_demo_files.py` / readme): **keep Claude-only for v1** (B3); document manual `odin run --platform cursor` testing in demo readme text only — do not regenerate otest fixture unless explicitly needed

## Acceptance

- `odin guide agent-md` prints useful content
- Full suite green

## On completion

`<<<NEXT_CONTEXT>>>` listing doc files touched.
