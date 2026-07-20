"""`odin exec` — single-shot headless dispatch (no queue).

The backend/runner is exercised elsewhere (test_backends / test_runner); here we
verify the `exec` command wiring: prompt sourcing, that the agent's final text
lands on stdout (capturable), and exit-code / error handling. `run_agent` is
stubbed so no real backend is spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from odin.cli import main
from odin.runner import RunResult


def _ok(text: str = "<<<NEXT_CONTEXT>>>\nok\n<<<END>>>") -> RunResult:
    return RunResult(True, text, "end_turn", None, 0, None, "grok")


def _fail() -> RunResult:
    return RunResult(False, "boom", "error", "error", 1, None, "grok")


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_exec_prompt_file_final_text_on_stdout(monkeypatch, project_dir: Path, tmp_path: Path, capsys):
    monkeypatch.setattr("odin.cli.run_agent", lambda *a, **k: _ok())
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing")
    code = main([
        "exec", "--platform", "grok",
        "--prompt-file", str(spec),
        "--project", str(project_dir),
        "--no-metrics",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "<<<NEXT_CONTEXT>>>" in out   # final text is on stdout for the caller


def test_exec_positional_prompt_failure_exit(monkeypatch, project_dir: Path):
    monkeypatch.setattr("odin.cli.run_agent", lambda *a, **k: _fail())
    code = main([
        "exec", "do the thing",
        "--platform", "grok",
        "--project", str(project_dir),
        "--no-metrics",
    ])
    assert code == 1   # backend failure -> non-zero exit


def test_exec_bad_prompt_file_is_error(project_dir: Path, tmp_path: Path):
    code = main([
        "exec", "--platform", "grok",
        "--prompt-file", str(tmp_path / "does-not-exist.md"),
        "--project", str(project_dir),
        "--no-metrics",
    ])
    assert code == 2   # unreadable prompt file, before any backend call
