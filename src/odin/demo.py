"""Scaffold the `otest` demo target project — a repeatable Odin test fixture.

`odin demo <dir>` writes a throwaway target project (the `greeter` CLI build)
whose queue exercises Odin end-to-end: sequential carry-context, a held →
resume cycle (task 005 is deliberately underspecified), and a final task. Run
it, watch it build `greeter`, then re-scaffold with `--force` to start over.

The fixture is **Claude Code-only for v1** (`CLAUDE.md`). Drive it with
`--platform claude` (platform is never assumed). Cursor smoke-testing is
documented as a manual step in the demo readme
(`odin run --platform cursor`), not baked into the scaffold.

The fixture content lives in `_demo_files.py` (generated). This module is the
logic: where to write, and how to safely reset an existing demo dir.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ._demo_files import FILES, QUEUE_SUBDIRS


class DemoError(RuntimeError):
    """Scaffolding failed for a reason worth showing the user."""


class DemoExists(DemoError):
    """Destination exists and is non-empty; --force needed to reset it."""


def create_demo(dest: Path, *, force: bool = False) -> list[Path]:
    """Write the demo project into `dest`. Returns the files written.

    If `dest` is non-empty, raises DemoExists unless `force` is set, in which
    case its contents are wiped first (after the safety guard) so a re-run is a
    clean reset.
    """
    dest = dest.resolve()

    if dest.exists() and not dest.is_dir():
        raise DemoError(f"{dest} exists and is not a directory")
    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise DemoExists(
                f"{dest} already exists and is not empty. "
                "Re-run with --force to wipe and recreate it."
            )
        _guard_force_target(dest)
        _wipe(dest)

    dest.mkdir(parents=True, exist_ok=True)
    for sub in QUEUE_SUBDIRS:
        (dest / "queue" / sub).mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for relpath, content in FILES.items():
        p = dest / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p)
    return written


# ----------------------------------------------------------------------
# safety
# ----------------------------------------------------------------------

def _guard_force_target(dest: Path) -> None:
    """Refuse to --force-wipe anything that isn't an obvious throwaway demo dir.

    The demo is never a git repo and is small; if the target looks like a real
    project (a .git, or no demo marker), bail rather than destroy work.
    """
    if dest == Path(dest.anchor) or dest == Path.home():
        raise DemoError(f"refusing to wipe {dest}: too dangerous a target")
    if (dest / ".git").exists():
        raise DemoError(
            f"refusing to --force {dest}: it contains a .git directory "
            "(looks like a real repo, not a demo). Remove it by hand if intended."
        )
    looks_like_demo = (dest / "CLAUDE.md").exists() or (dest / "queue").exists()
    if not looks_like_demo:
        raise DemoError(
            f"refusing to --force {dest}: it doesn't look like an Odin demo "
            "(no CLAUDE.md or queue/). Point --force at an existing demo, or "
            "use a fresh/empty directory without --force."
        )


def _wipe(dest: Path) -> None:
    for child in dest.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
