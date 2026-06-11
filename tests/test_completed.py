"""Tests for the COMPLETED.md mailbox (Component 4 — the Odin→Claude handoff).

We drive the full `_cmd_run` (with --no-git/--no-metrics) and a scripted fake
`run_claude` to each terminal state, then assert the written COMPLETED.md
carries the right outcome + counts and *no* task-body text. Gating (flag off,
--dry-run) and best-effort (a forced write failure) are covered too. Pure
`render`/`outcome_for` get focused unit tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

from odin import completed
from odin.cli import _build_parser, _cmd_run
from odin.queue import Queue
from odin.runner import RunResult

_SECRET = "SECRET-TASK-BODY-DO-NOT-LEAK"


# ----------------------------------------------------------------------
# scripted results
# ----------------------------------------------------------------------

def _completed() -> RunResult:
    return RunResult(True, "<<<NEXT_CONTEXT>>>\ncarry\n<<<END>>>", "end_turn", None, 0, None)


def _completed_urgent() -> RunResult:
    text = (
        "<<<NEXT_CONTEXT>>>\ncarry\n<<<END>>>\n"
        '<<<FOLLOW_UP>>>\n[{"title":"do x","body":"why","urgent":true}]\n<<<END>>>'
    )
    return RunResult(True, text, "end_turn", None, 0, None)


def _held() -> RunResult:
    j = ('{"questions":[{"problem":"p","question":"Which db?",'
         '"options":[{"key":"a","label":"PG"}],"recommended":"a","why":"y"}]}')
    return RunResult(True, f"<<<NEEDS_INPUT>>>\n{j}\n<<<END>>>", "end_turn", None, 0, None)


def _failed() -> RunResult:
    return RunResult(False, "boom", "max_turns", "error_max_turns", 1, None)


# ----------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------

def _run_cmd(tmp_path, monkeypatch, *, tasks, results, extra_args=()):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "CLAUDE.md").write_text("x", encoding="utf-8")
    qdir = tmp_path / "queue" / "myq"
    q = Queue(qdir)
    for name in tasks:
        (q.root / "pending" / name).write_text(_SECRET, encoding="utf-8")

    seq = list(results)
    monkeypatch.setattr("odin.cli.run_agent", lambda *a, **k: seq.pop(0))
    argv = ["run", str(qdir), "--project", str(project), "--no-git",
            "--no-metrics", *extra_args]
    args = _build_parser().parse_args(argv)
    rc = _cmd_run(args)
    return rc, q


def _record(q: Queue) -> str | None:
    p = q.root / completed.FILENAME
    return p.read_text(encoding="utf-8") if p.exists() else None


# ----------------------------------------------------------------------
# lands on every terminal state with correct outcome + counts
# ----------------------------------------------------------------------

def test_drained_record(tmp_path, monkeypatch):
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_completed()], extra_args=["--completed-file"])
    assert rc == 0
    text = _record(q)
    assert text is not None
    assert "outcome: drained" in text
    assert "exit_code: 0" in text
    assert "- done: 1" in text
    assert "- done  001-a" in text
    assert _SECRET not in text   # no task-body text leaks


def test_failed_record(tmp_path, monkeypatch):
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_failed()], extra_args=["--completed-file"])
    assert rc == 1
    text = _record(q)
    assert "outcome: failed" in text
    assert "- failed: 1" in text
    assert "- failed  001-a" in text
    assert _SECRET not in text


def test_held_record(tmp_path, monkeypatch):
    # stdin isn't a TTY under pytest → non-interactive held path, exit 10.
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_held()], extra_args=["--completed-file"])
    assert rc == 10
    text = _record(q)
    assert "outcome: held" in text
    assert "- held: 1" in text
    assert _SECRET not in text


def test_halted_urgent_record(tmp_path, monkeypatch):
    # A completed task discovers an urgent follow-up; unattended → halt (11).
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_completed_urgent()], extra_args=["--completed-file"])
    assert rc == 11
    text = _record(q)
    assert "outcome: halted-urgent" in text
    assert "exit_code: 11" in text
    assert "- done: 1" in text
    assert "- pending: 1" in text   # the inserted urgent follow-up
    assert _SECRET not in text


def test_record_overwritten_each_run(tmp_path, monkeypatch):
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_failed()], extra_args=["--completed-file"])
    assert "outcome: failed" in _record(q)
    # Re-run the (now failed) queue: empty pending → drained, record replaced.
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=[],
                     results=[], extra_args=["--completed-file"])
    text = _record(q)
    assert "outcome: drained" in text
    assert "outcome: failed" not in text   # prior record was truncated, not appended


# ----------------------------------------------------------------------
# gating: off by default, and never on --dry-run
# ----------------------------------------------------------------------

def test_absent_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_COMPLETED", raising=False)
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_completed()])
    assert rc == 0
    assert _record(q) is None


def test_absent_on_dry_run(tmp_path, monkeypatch):
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_completed()], extra_args=["--completed-file", "--dry-run"])
    assert rc == 0
    assert _record(q) is None


def test_env_enables_record(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_COMPLETED", "1")
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_completed()])
    assert rc == 0
    assert _record(q) is not None


# ----------------------------------------------------------------------
# best-effort: a forced write failure never changes the exit code
# ----------------------------------------------------------------------

def test_write_failure_does_not_break_run(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise OSError("forced render failure")
    monkeypatch.setattr("odin.completed.render", boom)
    rc, q = _run_cmd(tmp_path, monkeypatch, tasks=["001-a.md"],
                     results=[_completed()], extra_args=["--completed-file"])
    assert rc == 0          # run still drained cleanly
    assert _record(q) is None   # the write was swallowed


# ----------------------------------------------------------------------
# pure render / outcome_for units
# ----------------------------------------------------------------------

def test_outcome_for_known_and_unknown():
    assert completed.outcome_for(0) == "drained"
    assert completed.outcome_for(1) == "failed"
    assert completed.outcome_for(10) == "held"
    assert completed.outcome_for(11) == "halted-urgent"
    assert completed.outcome_for(7) == "exit-7"


def test_render_is_metadata_only():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    text = completed.render(
        run_id="abc", queue_name="myq", branch="feat", exit_code=0,
        counts={"done": 2, "failed": 0, "held": 0, "pending": 0,
                "running": 0, "backlog": 1},
        tasks=[("done", "001-a"), ("done", "002-b"), ("backlog", "003-c")],
        started=t0, ended=t1,
        tokens={"input": 1000, "output": 200, "cache_read": 50, "cache_creation": 5},
        cost=0.1234,
    )
    assert "run_id: abc" in text
    assert "branch: feat" in text
    assert "started: 2026-01-01T00:00:00+00:00" in text
    assert "ended: 2026-01-01T00:05:00+00:00" in text
    assert "tokens: in 1,000, out 200, cache-read 50, cache-write 5" in text
    assert "cost_usd: 0.1234" in text
    assert "+N" not in text


def test_render_branch_none_and_no_tasks():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    text = completed.render(
        run_id="x", queue_name="q", branch=None, exit_code=1,
        counts={"done": 0, "failed": 0, "held": 0, "pending": 0,
                "running": 0, "backlog": 0},
        tasks=[], started=t, ended=t, tokens={}, cost=0.0,
    )
    assert "branch: (none)" in text
    assert "- (none)" in text
