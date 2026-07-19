"""Tests for the injected system-prompt contract."""

from __future__ import annotations

from odin.contract import build_system_prompt


def test_base_contract_has_protocol_essentials():
    text = build_system_prompt(None)
    assert "<<<NEXT_CONTEXT>>>" in text
    assert "<<<NEEDS_INPUT>>>" in text
    assert "<<<END>>>" in text
    # The JSON question schema is taught.
    assert '"questions"' in text
    assert '"recommended"' in text
    # No branch directive when there's no branch.
    assert "## Branch" not in text


def test_base_contract_has_precedence_clause():
    text = build_system_prompt(None)
    assert "## Precedence" in text
    # It must claim precedence over the project's instruction file for
    # task-end and git policy. Default platform still names CLAUDE.md.
    assert "wins" in text.lower()
    assert "CLAUDE.md" in text
    assert "do not manage the task queue" in text.lower()


def test_branch_directive_included_when_branch_set():
    text = build_system_prompt("odin/batch-1")
    assert "## Branch" in text
    assert "odin/batch-1" in text
    assert "do not open pull requests" in text.lower()
    # Branch directive overrides any contrary instruction-file git rules.
    assert "overrides any contrary git instructions" in text.lower()
    assert "CLAUDE.md" in text


def test_claude_platform_names_claude_md():
    text = build_system_prompt(None, platform="claude")
    assert "CLAUDE.md" in text
    assert "AGENTS.md" not in text


def test_cursor_platform_names_agents_md():
    text = build_system_prompt("feat/x", platform="cursor")
    assert "AGENTS.md" in text
    assert "CLAUDE.md" not in text
    # Branch directive also uses the Cursor instruction name.
    assert "overrides any contrary git instructions in the project's AGENTS.md" in text


def test_unknown_platform_uses_generic_label():
    text = build_system_prompt(None, platform="future-agent")
    assert "project instructions" in text
    assert "CLAUDE.md" not in text
    assert "AGENTS.md" not in text
