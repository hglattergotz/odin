"""Soft lint of a target project's CLAUDE.md for git-workflow conflicts.

Odin runs the whole queue on one branch with no pull requests. A project
CLAUDE.md that tells the agent to do otherwise (open PRs, branch per task, push,
or not commit at all) fights that model. The injected protocol takes precedence,
but a silent mismatch is confusing — so at startup Odin *warns* (never blocks)
when it spots such directives. Pure and text-only so it is trivially testable.
"""

from __future__ import annotations

import re

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


def scan_claude_md(text: str) -> list[str]:
    """Return advisory reasons the CLAUDE.md may conflict with Odin's git model.

    Empty list means no conflicts spotted.
    """
    return [reason for pattern, reason in _PATTERNS if pattern.search(text)]
