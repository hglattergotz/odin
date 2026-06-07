#!/usr/bin/env python
"""Regenerate src/odin/_demo_files.py from a pristine demo project directory.

Workflow for changing the bundled `odin demo` fixture:

    odin demo /tmp/otest                 # scaffold a fresh demo
    # edit files in /tmp/otest (CLAUDE.md, task.md, readme.md, queue/pending/*)
    uv run python scripts/regen_demo_files.py /tmp/otest
    uv run pytest                        # confirm fidelity tests pass

The source dir must be PRISTINE — task files in queue/pending/ (not consumed
into done/held). `odin demo --force` gives you a clean one.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "src" / "odin" / "_demo_files.py"
DOC_FIXTURES = ("CLAUDE.md", "task.md", "readme.md")

_HEADER = '''\
"""Embedded otest demo fixture — GENERATED, do not edit by hand.

Regenerate with: uv run python scripts/regen_demo_files.py <pristine-demo-dir>
Maps destination-relative paths to file content.
"""

from __future__ import annotations

# Empty queue subdirs every demo needs.
QUEUE_SUBDIRS = ("pending", "running", "done", "failed", "held", "carry")

FILES: dict[str, str] = {
'''


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: regen_demo_files.py <pristine-demo-dir>", file=sys.stderr)
        return 2
    src = Path(argv[0]).resolve()
    pending = src / "queue" / "pending"
    if not pending.is_dir() or not any(pending.glob("*.md")):
        print(
            f"error: {pending} has no task files — point at a PRISTINE demo "
            "(odin demo --force gives you one).",
            file=sys.stderr,
        )
        return 2

    files: dict[str, str] = {r: (src / r).read_text(encoding="utf-8") for r in DOC_FIXTURES}
    for p in sorted(pending.glob("*.md")):
        files[f"queue/pending/{p.name}"] = p.read_text(encoding="utf-8")

    lines = [_HEADER]
    lines += [f"    {k!r}: {v!r},\n" for k, v in files.items()]
    lines.append("}\n")
    TARGET.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {TARGET} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
