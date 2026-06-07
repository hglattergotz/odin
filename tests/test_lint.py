"""Tests for the CLAUDE.md git-workflow conflict lint."""

from __future__ import annotations

import pytest

from odin.lint import scan_claude_md


def test_clean_claude_md_has_no_warnings():
    text = (
        "# My project\n\n## Workflow\n- Run `uv run pytest` before finishing.\n"
        "- One task = one focused change.\n"
    )
    assert scan_claude_md(text) == []


@pytest.mark.parametrize(
    "text, expect",
    [
        ("Always open a pull request for each change.", "mentions pull requests"),
        ("Submit a PR when done.", "mentions pull requests"),
        ("Create a new branch for every task.", "mentions creating per-task/feature branches"),
        ("Use a feature branch per ticket.", "mentions creating per-task/feature branches"),
        ("Remember to git push when green.", "mentions pushing"),
        ("Push your commits to origin.", "mentions pushing"),
        ("This demo does no git operations.", "tells the agent not to commit"),
        ("Do not commit anything in this repo.", "tells the agent not to commit"),
    ],
)
def test_conflict_phrases_are_flagged(text, expect):
    assert expect in scan_claude_md(text)


def test_multiple_reasons_collected():
    text = "Create a new branch, open a pull request, and git push the result."
    reasons = scan_claude_md(text)
    assert "mentions pull requests" in reasons
    assert "mentions creating per-task/feature branches" in reasons
    assert "mentions pushing" in reasons


def test_case_insensitive():
    assert scan_claude_md("OPEN A PULL REQUEST") == ["mentions pull requests"]
