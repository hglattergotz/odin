"""The `COMPLETED.md` mailbox — the Odin→Claude handoff (opt-in).

On every run exit, Odin can drop a metadata-only completion record into the
queue dir so the paired interactive Claude session — which runs in the same
project cwd — can read it deterministically on your next prompt, instead of
guessing whether Odin finished.

Design constraints (mirror metrics.py):
  - **Metadata only.** Run id, queue name, branch, outcome, per-state counts,
    per-task stem + final state, timestamps, and token/cost totals. **Never**
    task bodies, prompts, carry-context, or agent output (they can carry
    secrets).
  - **Best-effort.** The write is wrapped and swallowed — a failure must never
    change the run's exit code (same posture as runner._safe_write).
  - **Pairing is by directory, not PID.** The file lives in the project's queue
    dir; the project's Claude runs in that cwd. Project = directory = identity,
    so there is no process matching and no cross-project mix-ups.

Gated behind `--completed-file` / `ODIN_COMPLETED`; skipped on `--dry-run`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

FILENAME = "COMPLETED.md"

# Human outcome label per Odin exit code (the codes that reach the run finally:
# the loop returns 0/1/10/11; an unexpected code is rendered verbatim).
_OUTCOME = {
    0: "drained",
    1: "failed",
    10: "held",
    11: "halted-urgent",
}

# Order tasks are listed in the record (most-actionable first).
_TASK_STATES = ("done", "failed", "held", "running", "pending", "backlog")


def now() -> datetime:
    """Timezone-aware current time (injectable start/end stamp for the record)."""
    return datetime.now(timezone.utc)


def outcome_for(exit_code: int) -> str:
    return _OUTCOME.get(exit_code, f"exit-{exit_code}")


def _task_rows(q: object) -> list[tuple[str, str]]:
    """[(state, stem)] across every working subdir, most-actionable first."""
    getters = {
        "done": q.done,
        "failed": q.failed,
        "held": q.held,
        "running": q.running,
        "pending": q.pending,
        "backlog": q.backlog,
    }
    rows: list[tuple[str, str]] = []
    for state in _TASK_STATES:
        for t in getters[state]():
            rows.append((state, t.stem))
    return rows


def _tokens_line(tokens: dict) -> str:
    def n(key: str) -> str:
        v = tokens.get(key)
        return f"{v:,}" if isinstance(v, (int, float)) else "0"
    return (
        f"in {n('input')}, out {n('output')}, "
        f"cache-read {n('cache_read')}, cache-write {n('cache_creation')}"
    )


def render(
    *,
    run_id: str,
    queue_name: str,
    branch: str | None,
    exit_code: int,
    counts: dict,
    tasks: list[tuple[str, str]],
    started: datetime,
    ended: datetime,
    tokens: dict,
    cost: float,
) -> str:
    """Render the metadata-only completion record (pure; no I/O)."""
    lines = [
        "# Odin run complete",
        "",
        "Metadata-only handoff written by Odin for the paired Claude session in "
        "this project. No task bodies or agent output are included.",
        "",
        f"- run_id: {run_id}",
        f"- queue: {queue_name}",
        f"- branch: {branch or '(none)'}",
        f"- exit_code: {exit_code}",
        f"- outcome: {outcome_for(exit_code)}",
        f"- started: {started.isoformat()}",
        f"- ended: {ended.isoformat()}",
        "",
        "## Counts",
        "",
    ]
    for state in ("done", "failed", "held", "pending", "running", "backlog"):
        lines.append(f"- {state}: {counts.get(state, 0)}")
    lines += [
        "",
        "## Totals",
        "",
        f"- tokens: {_tokens_line(tokens)}",
        f"- cost_usd: {cost:.4f}",
        "",
        "## Tasks",
        "",
    ]
    if tasks:
        width = max(len(state) for state, _ in tasks)
        for state, stem in tasks:
            lines.append(f"- {state.ljust(width)}  {stem}")
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def write_record(
    q: object,
    *,
    run_id: str,
    branch: str | None,
    exit_code: int,
    started: datetime,
    acc: object,
    ended: datetime | None = None,
) -> None:
    """Write `<queue.root>/COMPLETED.md`, overwriting any prior record.

    Best-effort: any failure is swallowed so it can never change the exit code.
    Token/cost totals are read off the accumulator, which tallies them whether
    or not metrics are enabled.
    """
    try:
        text = render(
            run_id=run_id,
            queue_name=q.root.name,
            branch=branch,
            exit_code=exit_code,
            counts=q.counts(),
            tasks=_task_rows(q),
            started=started,
            ended=ended or now(),
            tokens=dict(getattr(acc, "tokens_total", {}) or {}),
            cost=float(getattr(acc, "cost_total", 0.0) or 0.0),
        )
        (Path(q.root) / FILENAME).write_text(text, encoding="utf-8")
    except Exception:
        # The mailbox must never sink a run.
        pass
