# Multi-platform agent backends — implementation queue

Implements [docs/multi-platform-agents-proposal.md](../../docs/multi-platform-agents-proposal.md).

Copy into a **named sub-queue** when running locally (repo root `/queue/` is gitignored):

```sh
mkdir -p queue
cp -R examples/queue/multi-platform queue/multi-platform
odin run multi-platform --branch multi-platform --no-git
```

Do **not** use `cp -r examples/queue/multi-platform queue/` when `queue/` does not exist yet — that creates `queue/pending/` at the container root instead of `queue/multi-platform/pending/`.

Or run the tracked example path directly (no copy):

```sh
odin run examples/queue/multi-platform --branch multi-platform --no-git
```

## Staging

| Location | Tasks | Batch |
|----------|-------|-------|
| `pending/` | 001–005 | **A** — backend skeleton + config (zero behaviour change) |
| `backlog/` | 006–012 | **B** (Cursor) + **C** (polish) — promote after Batch A is green |

After A drains: re-read the proposal (especially §2 success predicates and Appendix C), promote `006-*` from `backlog/` to `pending/`, then continue.

Each batch must leave the full test suite green (`uv run pytest`).
