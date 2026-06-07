"""Tests for the startup-only git wrapper, using real temp repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from odin import git


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "init", "-b", "main")
    _run(r, "config", "user.email", "test@example.com")
    _run(r, "config", "user.name", "Test")
    (r / "README.md").write_text("hello\n")
    _run(r, "add", ".")
    _run(r, "commit", "-m", "initial")
    return r


def test_is_repo_true_and_false(repo: Path, tmp_path: Path):
    assert git.is_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git.is_repo(plain) is False


def test_current_branch(repo: Path):
    assert git.current_branch(repo) == "main"


def test_is_clean_true_on_fresh_repo(repo: Path):
    clean, text = git.is_clean(repo)
    assert clean is True
    assert text == ""


def test_is_clean_false_when_dirty(repo: Path):
    (repo / "README.md").write_text("changed\n")
    clean, text = git.is_clean(repo)
    assert clean is False
    assert "README.md" in text


def test_is_clean_ignores_queue_dir(repo: Path):
    # Untracked queue files would otherwise make the tree look dirty.
    qdir = repo / "queue" / "pending"
    qdir.mkdir(parents=True)
    (qdir / "001-a.md").write_text("task\n")
    clean_unfiltered, _ = git.is_clean(repo)
    assert clean_unfiltered is False
    clean, text = git.is_clean(repo, ignore_within=repo / "queue")
    assert clean is True
    assert text == ""


def test_is_clean_queue_outside_project_not_filtered(repo: Path, tmp_path: Path):
    (repo / "README.md").write_text("changed\n")
    # Queue path outside the repo can't be relativised; filtering is a no-op.
    clean, _ = git.is_clean(repo, ignore_within=tmp_path / "elsewhere")
    assert clean is False


def test_branch_exists(repo: Path):
    assert git.branch_exists(repo, "main") is True
    assert git.branch_exists(repo, "nope") is False


def test_create_and_checkout(repo: Path):
    git.create_and_checkout(repo, "feature/x", "main")
    assert git.current_branch(repo) == "feature/x"
    assert git.branch_exists(repo, "feature/x") is True


def test_checkout_existing(repo: Path):
    git.create_and_checkout(repo, "other", "main")
    git.checkout(repo, "main")
    assert git.current_branch(repo) == "main"


def test_checkout_missing_raises(repo: Path):
    with pytest.raises(git.GitError):
        git.checkout(repo, "does-not-exist")
