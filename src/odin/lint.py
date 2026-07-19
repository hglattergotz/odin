"""Soft lint of a target project's instruction files for git-workflow conflicts.

Odin runs the whole queue on one branch with no pull requests. A project's
instruction file (CLAUDE.md, AGENTS.md, `.cursor/rules`, …) that tells the
agent to do otherwise (open PRs, branch per task, push, or not commit at all)
fights that model. The injected protocol takes precedence, but a silent
mismatch is confusing — so at startup Odin *warns* (never blocks) when it
spots such directives. Pure and text-only so it is trivially testable.
"""

from __future__ import annotations

import re
from pathlib import Path

# (compiled pattern, human reason). Patterns are intentionally specific to keep
# false positives low; matches are advisory only.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpull request|\bopen(?:ing)? a pr\b|\bPRs?\b", re.I), "mentions pull requests"),
    (
        re.compile(
            r"\bbranch per task\b|\bper-task branch\b|\bfeature branch\b|"
            r"\bcreate a (?:new )?branch\b|\bnew branch (?:for|per)\b",
            re.I,
        ),
        "mentions creating per-task/feature branches",
    ),
    (re.compile(r"\bgit push\b|\bpush (?:to|your|the|changes|commits)\b", re.I), "mentions pushing"),
    (
        re.compile(
            r"\bno git\b|\bdo not commit\b|\bdon't commit\b|\bnever commit\b|\bskip (?:the )?commit\b",
            re.I,
        ),
        "tells the agent not to commit",
    ),
]


def scan_instruction_text(text: str) -> list[str]:
    """Return advisory reasons the instruction text may conflict with Odin's git model.

    Empty list means no conflicts spotted. Platform-agnostic — the same patterns
    apply to CLAUDE.md, AGENTS.md, and Cursor rule files.
    """
    return [reason for pattern, reason in _PATTERNS if pattern.search(text)]


# Back-compat alias used by older call sites / docs; prefer scan_instruction_text.
scan_claude_md = scan_instruction_text


def _iter_instruction_texts(target: Path) -> list[tuple[Path, str]]:
    """Collect (path, text) for a file or every file under a directory."""
    if not target.exists():
        return []
    if target.is_file():
        return [(target, target.read_text(encoding="utf-8", errors="replace"))]
    if target.is_dir():
        out: list[tuple[Path, str]] = []
        for f in sorted(p for p in target.rglob("*") if p.is_file()):
            out.append((f, f.read_text(encoding="utf-8", errors="replace")))
        return out
    return []


def scan_project_instructions(
    project: Path, platform: str
) -> list[tuple[Path, list[str]]]:
    """Scan the platform's instruction files under `project` for git conflicts.

    Returns a list of `(path, reasons)` for each file that triggered at least
    one advisory reason. Missing files/dirs are skipped (no error). Uses the
    backend registry to resolve which relative paths the platform reads.
    """
    # Lazy import: lint is imported early by cli; keep the registry off the
    # module import path so a lean `from odin.lint import scan_instruction_text`
    # stays cheap and cycle-free.
    from odin.backends.registry import get_backend

    backend = get_backend(platform)
    findings: list[tuple[Path, list[str]]] = []
    for rel in backend.instruction_files():
        for path, text in _iter_instruction_texts(project / rel):
            reasons = scan_instruction_text(text)
            if reasons:
                findings.append((path, reasons))
    return findings
