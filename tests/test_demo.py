"""Tests for `odin demo` — the repeatable test-fixture scaffolder."""

from __future__ import annotations

from pathlib import Path

import pytest

from odin.cli import main
from odin.demo import DemoError, DemoExists, create_demo


EXPECTED_TASKS = [
    "001-init-pyproject.md",
    "002-add-package-skeleton.md",
    "003-add-hello-core.md",
    "004-wire-cli.md",
    "005-add-greeting-styles.md",
    "006-add-tests.md",
    "007-add-readme.md",
]


def test_create_demo_writes_fixture_and_empty_queue(tmp_path: Path):
    dest = tmp_path / "otest"
    written = create_demo(dest)
    assert len(written) == 10  # 3 fixture docs + 7 tasks

    # Fixture docs.
    assert (dest / "CLAUDE.md").exists()
    assert (dest / "task.md").exists()
    assert (dest / "readme.md").exists()
    # Demo stays Claude-only for v1; Cursor is a manual smoke-test note only.
    readme = (dest / "readme.md").read_text()
    assert "Claude-only for v1" in readme
    assert "--platform cursor" in readme
    assert not (dest / "AGENTS.md").exists()

    # All seven tasks land in pending, in order.
    pending = sorted(p.name for p in (dest / "queue" / "pending").glob("*.md"))
    assert pending == EXPECTED_TASKS

    # Other queue subdirs exist and are empty.
    for sub in ("running", "done", "failed", "held", "carry"):
        d = dest / "queue" / sub
        assert d.is_dir()
        assert list(d.iterdir()) == []

    # No run artifacts — task 001 must create pyproject.toml, 002+ create src/.
    assert not (dest / "pyproject.toml").exists()
    assert not (dest / "src").exists()


def test_task_005_is_underspecified_original(tmp_path: Path):
    dest = tmp_path / "otest"
    create_demo(dest)
    body = (dest / "queue" / "pending" / "005-add-greeting-styles.md").read_text()
    # The original body, not the resume-merged one.
    assert body.startswith("# Task: add greeting styles")
    assert "Prior questions and the user's answers" not in body


def test_create_refuses_nonempty_without_force(tmp_path: Path):
    dest = tmp_path / "otest"
    create_demo(dest)
    with pytest.raises(DemoExists):
        create_demo(dest)


def test_force_resets_existing_demo(tmp_path: Path):
    dest = tmp_path / "otest"
    create_demo(dest)
    # Simulate a partial run leaving artifacts behind.
    (dest / "pyproject.toml").write_text("generated\n")
    (dest / "src").mkdir()
    (dest / "src" / "greeter.py").write_text("x = 1\n")
    moved = dest / "queue" / "done" / "001-init-pyproject.md"
    moved.write_text("done\n")

    create_demo(dest, force=True)
    # Artifacts gone, queue reset.
    assert not (dest / "pyproject.toml").exists()
    assert not (dest / "src").exists()
    assert not moved.exists()
    pending = sorted(p.name for p in (dest / "queue" / "pending").glob("*.md"))
    assert pending == EXPECTED_TASKS


def test_force_refuses_git_repo(tmp_path: Path):
    dest = tmp_path / "real-project"
    dest.mkdir()
    (dest / ".git").mkdir()
    (dest / "important.py").write_text("do not delete me\n")
    with pytest.raises(DemoError, match="git"):
        create_demo(dest, force=True)
    assert (dest / "important.py").exists()  # untouched


def test_force_refuses_non_demo_dir(tmp_path: Path):
    dest = tmp_path / "random"
    dest.mkdir()
    (dest / "stuff.txt").write_text("unrelated\n")
    with pytest.raises(DemoError, match="doesn't look like an Odin demo"):
        create_demo(dest, force=True)
    assert (dest / "stuff.txt").exists()


def test_cli_demo_command(tmp_path: Path, capsys):
    dest = tmp_path / "otest"
    rc = main(["demo", str(dest)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "7 queued tasks" in out
    assert (dest / "queue" / "pending" / "001-init-pyproject.md").exists()


def test_cli_demo_force_flag(tmp_path: Path):
    dest = tmp_path / "otest"
    assert main(["demo", str(dest)]) == 0
    assert main(["demo", str(dest)]) == 2          # non-empty, no --force
    assert main(["demo", str(dest), "--force"]) == 0  # reset
