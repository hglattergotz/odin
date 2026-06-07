"""Startup-only git helpers.

Odin's git footprint is deliberately tiny and confined to *startup*: verify the
working tree is clean and put the repo on the one branch the whole queue will
land on. Odin never commits, pushes, merges, or opens PRs — per-task commits
stay the target project's CLAUDE.md's job.

Every function shells out to `git` (must be on PATH) with `cwd` set to the
target project. Non-zero exits raise GitError so the CLI can print a clean
message instead of a traceback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A git invocation failed."""


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(project),
        capture_output=True,
        text=True,
    )


def _checked(project: Path, *args: str) -> str:
    proc = _git(project, *args)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def is_repo(project: Path) -> bool:
    proc = _git(project, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def current_branch(project: Path) -> str:
    """Short name of the current branch, or "" when HEAD is detached."""
    proc = _git(project, "symbolic-ref", "--quiet", "--short", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_clean(project: Path, *, ignore_within: Path | None = None) -> tuple[bool, str]:
    """Return (clean, porcelain_text).

    `ignore_within`, when set and located inside the project, drops status
    entries beneath it — this is how the queue directory (untracked
    pending/done/… files) is excluded so it doesn't make the tree look dirty.
    """
    out = _checked(project, "status", "--porcelain")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if ignore_within is not None:
        rel = _rel_or_none(project, ignore_within)
        if rel is not None:
            lines = [ln for ln in lines if not _entry_within(ln, rel)]
    return (not lines, "\n".join(lines))


def branch_exists(project: Path, name: str) -> bool:
    proc = _git(project, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    return proc.returncode == 0


def checkout(project: Path, name: str) -> None:
    _checked(project, "switch", name)


def create_and_checkout(project: Path, name: str, base: str | None = None) -> None:
    args = ["switch", "-c", name]
    if base:
        args.append(base)
    _checked(project, *args)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _rel_or_none(project: Path, target: Path) -> str | None:
    try:
        return str(target.resolve().relative_to(project.resolve()))
    except ValueError:
        return None  # queue lives outside the project; nothing to filter


def _entry_within(porcelain_line: str, rel_prefix: str) -> bool:
    """True if a `git status --porcelain` entry points inside `rel_prefix`.

    Porcelain v1 lines are "XY <path>" or "XY <orig> -> <path>" for renames.
    """
    payload = porcelain_line[3:] if len(porcelain_line) > 3 else porcelain_line
    path = payload.split(" -> ")[-1].strip().strip('"')
    prefix = rel_prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")
