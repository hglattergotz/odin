"""Tests for the project-instruction git-workflow conflict lint."""

from __future__ import annotations

from pathlib import Path

import pytest

from odin.lint import scan_claude_md, scan_instruction_text, scan_project_instructions


def test_clean_instruction_text_has_no_warnings():
    text = (
        "# My project\n\n## Workflow\n- Run `uv run pytest` before finishing.\n"
        "- One task = one focused change.\n"
    )
    assert scan_instruction_text(text) == []


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
    assert expect in scan_instruction_text(text)


def test_multiple_reasons_collected():
    text = "Create a new branch, open a pull request, and git push the result."
    reasons = scan_instruction_text(text)
    assert "mentions pull requests" in reasons
    assert "mentions creating per-task/feature branches" in reasons
    assert "mentions pushing" in reasons


def test_case_insensitive():
    assert scan_instruction_text("OPEN A PULL REQUEST") == ["mentions pull requests"]


def test_scan_claude_md_alias_still_works():
    """Back-compat alias kept for older call sites / docs."""
    assert scan_claude_md is scan_instruction_text
    assert scan_claude_md("open a PR") == ["mentions pull requests"]


def test_scan_project_instructions_claude(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("Always open a pull request.\n")
    findings = scan_project_instructions(tmp_path, "claude")
    assert len(findings) == 1
    path, reasons = findings[0]
    assert path.name == "CLAUDE.md"
    assert "mentions pull requests" in reasons


def test_scan_project_instructions_cursor_agents_md(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Create a new branch for every task.\n")
    findings = scan_project_instructions(tmp_path, "cursor")
    assert len(findings) == 1
    path, reasons = findings[0]
    assert path.name == "AGENTS.md"
    assert "mentions creating per-task/feature branches" in reasons


def test_scan_project_instructions_cursor_rules_dir(tmp_path: Path):
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "git.mdc").write_text("Remember to git push when green.\n")
    findings = scan_project_instructions(tmp_path, "cursor")
    assert len(findings) == 1
    path, reasons = findings[0]
    assert path.name == "git.mdc"
    assert "mentions pushing" in reasons


def test_scan_project_instructions_skips_missing(tmp_path: Path):
    assert scan_project_instructions(tmp_path, "claude") == []
    assert scan_project_instructions(tmp_path, "cursor") == []


def test_scan_project_instructions_ignores_other_platform_file(tmp_path: Path):
    # Cursor should not lint CLAUDE.md; Claude should not lint AGENTS.md.
    (tmp_path / "CLAUDE.md").write_text("Always open a pull request.\n")
    (tmp_path / "AGENTS.md").write_text("Always open a pull request.\n")
    assert scan_project_instructions(tmp_path, "cursor")[0][0].name == "AGENTS.md"
    assert scan_project_instructions(tmp_path, "claude")[0][0].name == "CLAUDE.md"
