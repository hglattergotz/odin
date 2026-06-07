"""Tests for the central metrics module."""

from __future__ import annotations

import json
from pathlib import Path

from odin import metrics


class _FakeResult:
    """Stand-in for runner.RunResult carrying just the metrics fields."""

    def __init__(self, **kw):
        self.stop_reason = kw.get("stop_reason", "end_turn")
        self.error = kw.get("error")
        self.wall_ms = kw.get("wall_ms", 1000)
        self.duration_ms = kw.get("duration_ms", 900)
        self.api_ms = kw.get("api_ms", 700)
        self.num_turns = kw.get("num_turns", 5)
        self.usage = kw.get("usage", {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 10,
        })
        self.cost_usd = kw.get("cost_usd", 0.25)
        self.session_id = kw.get("session_id", "sess-1")


def test_config_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ODIN_HOME", str(tmp_path / "h"))
    assert metrics.events_path() == tmp_path / "h" / "metrics" / "events.jsonl"
    monkeypatch.setenv("ODIN_METRICS_FILE", str(tmp_path / "x.jsonl"))
    assert metrics.events_path() == tmp_path / "x.jsonl"


def test_enabled_toggle(monkeypatch):
    monkeypatch.delenv("ODIN_NO_METRICS", raising=False)
    assert metrics.enabled() is True
    monkeypatch.setenv("ODIN_NO_METRICS", "1")
    assert metrics.enabled() is False
    monkeypatch.setenv("ODIN_NO_METRICS", "0")
    assert metrics.enabled() is True


def test_accumulator_writes_task_and_run(tmp_path):
    path = tmp_path / "events.jsonl"
    acc = metrics.RunAccumulator(
        run_id="r1", project="/code/proj", queue="/code/proj/queue",
        branch="feat", enabled=True, path=path,
    )
    acc.record_task(task_stem="001-a", outcome="completed", result=_FakeResult())
    acc.record_task(
        task_stem="002-b", outcome="failed",
        result=_FakeResult(cost_usd=0.10, usage={"input_tokens": 1, "output_tokens": 2}),
    )
    acc.finish(1)

    events = metrics.read_events(path)
    tasks = [e for e in events if e["type"] == "task"]
    runs = [e for e in events if e["type"] == "run"]
    assert len(tasks) == 2
    assert len(runs) == 1
    assert tasks[0]["tokens"] == {
        "input": 100, "output": 50, "cache_read": 200, "cache_creation": 10,
    }
    assert tasks[0]["cost_usd"] == 0.25
    run = runs[0]
    assert run["tasks_completed"] == 1
    assert run["tasks_failed"] == 1
    assert run["tasks_total"] == 2
    assert run["cost_usd_total"] == 0.35
    assert run["tokens_total"]["input"] == 101
    assert run["stop"] == "failed"


def test_disabled_writes_nothing(tmp_path):
    path = tmp_path / "events.jsonl"
    acc = metrics.RunAccumulator(
        run_id="r", project="p", queue="q", branch=None, enabled=False, path=path,
    )
    acc.record_task(task_stem="001", outcome="completed", result=_FakeResult())
    acc.finish(0)
    assert not path.exists()


def test_empty_run_writes_no_summary(tmp_path):
    path = tmp_path / "events.jsonl"
    acc = metrics.RunAccumulator(
        run_id="r", project="p", queue="q", branch=None, enabled=True, path=path,
    )
    acc.finish(0)  # no tasks recorded
    assert metrics.read_events(path) == []


def test_finish_is_idempotent(tmp_path):
    path = tmp_path / "events.jsonl"
    acc = metrics.RunAccumulator(
        run_id="r", project="p", queue="q", branch=None, enabled=True, path=path,
    )
    acc.record_task(task_stem="001", outcome="completed", result=_FakeResult())
    acc.finish(0)
    acc.finish(0)
    runs = [e for e in metrics.read_events(path) if e["type"] == "run"]
    assert len(runs) == 1


def test_read_events_skips_corrupt_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"type":"task","outcome":"completed"}\n'
        "not json at all\n"
        "\n"
        '{"type":"run","tasks_total":1}\n',
        encoding="utf-8",
    )
    events = metrics.read_events(path)
    assert len(events) == 2


def test_aggregate_and_concurrency(tmp_path):
    path = tmp_path / "events.jsonl"
    events = [
        {"type": "task", "project": "/a", "outcome": "completed", "wall_ms": 1000,
         "tokens": {"input": 10, "output": 5}, "cost_usd": 0.1},
        {"type": "task", "project": "/a", "outcome": "held", "wall_ms": 3000,
         "tokens": {"input": 20}, "cost_usd": 0.2},
        {"type": "task", "project": "/b", "outcome": "failed", "wall_ms": 2000},
        # Two runs that overlap in time -> peak concurrency 2.
        {"type": "run", "project": "/a", "ts_start": "2026-05-31T10:00:00+00:00",
         "ts_end": "2026-05-31T10:10:00+00:00", "tasks_total": 2,
         "tasks_completed": 1, "tasks_failed": 0, "tasks_held": 1,
         "wall_ms": 600000, "cost_usd_total": 0.3, "stop": "held"},
        {"type": "run", "project": "/b", "ts_start": "2026-05-31T10:05:00+00:00",
         "ts_end": "2026-05-31T10:15:00+00:00", "tasks_total": 1,
         "tasks_completed": 0, "tasks_failed": 1, "tasks_held": 0,
         "wall_ms": 600000, "cost_usd_total": 0.0, "stop": "failed"},
    ]
    agg = metrics.aggregate(events, path=path)
    assert agg["n_tasks"] == 3
    assert agg["n_runs"] == 2
    assert agg["outcomes"] == {"completed": 1, "held": 1, "failed": 1}
    assert abs(agg["cost_total"] - 0.3) < 1e-9
    assert agg["tokens_total"]["input"] == 30
    assert agg["peak_concurrency"] == 2
    assert len(agg["projects"]) == 2
    # /a has more tasks, sorts first.
    assert agg["projects"][0]["project"] == "/a"
    assert agg["projects"][0]["tasks"] == 2


def test_aggregate_project_filter(tmp_path):
    events = [
        {"type": "task", "project": "/code/alpha", "outcome": "completed", "wall_ms": 1},
        {"type": "task", "project": "/code/beta", "outcome": "completed", "wall_ms": 1},
    ]
    agg = metrics.aggregate(events, project_filter="alpha")
    assert agg["n_tasks"] == 1
    assert agg["projects"][0]["project"] == "/code/alpha"


def test_render_text_empty():
    out = metrics.render_text(metrics.aggregate([]))
    assert "No metrics recorded yet" in out


def test_by_project_total_row():
    events = [
        {"type": "task", "project": "/a", "outcome": "completed", "wall_ms": 1000, "cost_usd": 0.1},
        {"type": "task", "project": "/a", "outcome": "held", "wall_ms": 3000, "cost_usd": 0.2},
        {"type": "task", "project": "/b", "outcome": "failed", "wall_ms": 2000, "cost_usd": 0.3},
        {"type": "run", "project": "/a", "tasks_total": 2, "tasks_completed": 1,
         "tasks_held": 1, "tasks_failed": 0, "wall_ms": 1, "cost_usd_total": 0.3, "stop": "held"},
        {"type": "run", "project": "/b", "tasks_total": 1, "tasks_completed": 0,
         "tasks_held": 0, "tasks_failed": 1, "wall_ms": 1, "cost_usd_total": 0.3, "stop": "failed"},
    ]
    agg = metrics.aggregate(events)
    ps = agg["projects"]
    # text: a TOTAL line summing each numeric column across projects
    total = next(l for l in metrics.render_text(agg).splitlines() if l.strip().startswith("TOTAL"))
    nums = total.split()
    assert int(nums[1]) == sum(p["runs"] for p in ps)        # runs
    assert int(nums[2]) == sum(p["tasks"] for p in ps)       # tasks
    assert int(nums[3]) == sum(p["completed"] for p in ps)   # done
    assert int(nums[4]) == sum(p["held"] for p in ps)        # held
    assert int(nums[5]) == sum(p["failed"] for p in ps)      # fail
    # html: a tfoot total row
    html = metrics.render_html(agg)
    assert "<tfoot>" in html and "TOTAL" in html


def test_render_text_and_html_smoke():
    events = [
        {"type": "task", "project": "/a", "outcome": "completed", "wall_ms": 1500,
         "tokens": {"input": 10, "output": 5}, "cost_usd": 0.1},
        {"type": "run", "project": "/a", "ts_start": "2026-05-31T10:00:00+00:00",
         "ts_end": "2026-05-31T10:10:00+00:00", "tasks_total": 1,
         "tasks_completed": 1, "tasks_failed": 0, "tasks_held": 0,
         "wall_ms": 600000, "cost_usd_total": 0.1, "stop": "drained_or_capped",
         "branch": "main"},
    ]
    agg = metrics.aggregate(events)
    text = metrics.render_text(agg)
    assert "Overall" in text
    assert "By project" in text
    html = metrics.render_html(agg)
    assert html.startswith("<!doctype html>")
    assert "Odin metrics" in html
    assert "</body></html>" in html


def test_run_records_metrics_end_to_end(tmp_path, monkeypatch):
    """An actual `odin run` should append task+run records to the central log."""
    from odin.cli import main

    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("ODIN_METRICS_FILE", str(events_file))

    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# target\n")
    qdir = tmp_path / "queue"
    (qdir / "pending").mkdir(parents=True)
    (qdir / "pending" / "001-a.md").write_text("do a thing")

    fake = tmp_path / "fake-claude.sh"
    fake.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '" + json.dumps({
            "type": "result", "subtype": "success", "stop_reason": "end_turn",
            "is_error": False, "result": "<<<NEXT_CONTEXT>>>\ncarry\n<<<END>>>",
            "usage": {"input_tokens": 100, "output_tokens": 40},
            "total_cost_usd": 0.42, "duration_ms": 1234, "num_turns": 3,
            "session_id": "s1",
        }) + "'\n"
    )
    fake.chmod(0o755)

    rc = main(["run", str(qdir), "--project", str(project), "--no-git",
               "--claude-bin", str(fake)])
    assert rc == 0

    events = metrics.read_events(events_file)
    tasks = [e for e in events if e["type"] == "task"]
    runs = [e for e in events if e["type"] == "run"]
    assert len(tasks) == 1
    assert tasks[0]["outcome"] == "completed"
    assert tasks[0]["cost_usd"] == 0.42
    assert tasks[0]["tokens"]["input"] == 100
    assert tasks[0]["num_turns"] == 3
    assert tasks[0]["wall_ms"] is not None
    assert len(runs) == 1
    assert runs[0]["tasks_completed"] == 1


def test_run_no_metrics_flag_writes_nothing(tmp_path, monkeypatch):
    from odin.cli import main

    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("ODIN_METRICS_FILE", str(events_file))

    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# target\n")
    qdir = tmp_path / "queue"
    (qdir / "pending").mkdir(parents=True)
    (qdir / "pending" / "001-a.md").write_text("do a thing")

    fake = tmp_path / "fake-claude.sh"
    fake.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '" + json.dumps({
            "type": "result", "subtype": "success", "stop_reason": "end_turn",
            "is_error": False, "result": "<<<NEXT_CONTEXT>>>\nc\n<<<END>>>",
        }) + "'\n"
    )
    fake.chmod(0o755)

    rc = main(["run", str(qdir), "--project", str(project), "--no-git",
               "--no-metrics", "--claude-bin", str(fake)])
    assert rc == 0
    assert not events_file.exists()
