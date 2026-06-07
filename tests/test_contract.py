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
    # It must claim precedence over CLAUDE.md for task-end and git policy.
    assert "wins" in text.lower()
    assert "do not manage the task queue" in text.lower()


def test_branch_directive_included_when_branch_set():
    text = build_system_prompt("odin/batch-1")
    assert "## Branch" in text
    assert "odin/batch-1" in text
    assert "do not open pull requests" in text.lower()
    # Branch directive overrides any contrary CLAUDE.md git rules.
    assert "overrides any contrary git instructions" in text.lower()
