# Multi-platform agent backends — implementation queue

Implements [docs/multi-platform-agents-proposal.md](../../docs/multi-platform-agents-proposal.md).

Copy or symlink to `./queue/multi-platform/` when running locally (repo root `/queue/` is gitignored).

## Staging

| Location | Tasks | Batch |
|----------|-------|-------|
| `pending/` | 001–005 | **A** — backend skeleton + config (zero behaviour change) |
| `backlog/` | 006–012 | **B** (Cursor) + **C** (polish) — promote after Batch A is green |

**Run Batch A only first:**

```sh
odin run examples/queue/multi-platform --branch multi-platform --no-git
# or: cp -r examples/queue/multi-platform queue/ && odin run multi-platform --no-git
```

After A drains: re-read the proposal (especially §2 success predicates and Appendix C), promote `006-*` from `backlog/` to `pending/`, then continue.

Each batch must leave the full test suite green (`uv run pytest`).
