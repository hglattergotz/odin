"""Tests for `odin guide` — the self-discovery authoring manual."""

from __future__ import annotations

from odin.cli import main
from odin.contract import build_system_prompt
from odin.guide import TOPICS, render


def test_full_guide_covers_every_essential():
    text = render()
    # Queue / file format — always a NAMED queue, never a bare queue/pending/.
    assert "queue/<name>/pending/" in text
    assert "NNN-slug.md" in text
    assert "no frontmatter" in text.lower()
    assert "one task = one file" in text.lower()
    # CLAUDE.md workflow guidance.
    assert "CLAUDE.md" in text
    assert "Workflow" in text
    # Protocol is present and matches what Odin actually injects.
    assert "<<<NEXT_CONTEXT>>>" in text
    assert "<<<NEEDS_INPUT>>>" in text
    assert '"questions"' in text
    # Flow + run.
    assert "Carry-context" in text
    assert "odin run" in text


def test_protocol_section_is_sourced_from_contract():
    # The injected contract text must appear verbatim in the guide (indented),
    # so the guide can never drift from runtime behaviour. Guide shows the
    # default (Claude) wording; Cursor runs get AGENTS.md via platform=.
    contract = build_system_prompt(None)
    first_real_line = next(ln for ln in contract.splitlines() if ln.strip())
    assert first_real_line in render()
    assert "CLAUDE.md" in contract
    assert "AGENTS.md" not in contract


def test_topic_subsets():
    tasks = render("tasks")
    assert "NNN-slug.md" in tasks
    assert "## Workflow" not in tasks  # claude-md section excluded

    cm = render("claude-md")
    assert "CLAUDE.md" in cm
    assert "<<<NEEDS_INPUT>>>" in cm  # includes protocol
    assert "This project is run by Odin" in cm  # pasteable marker block

    am = render("agent-md")
    assert "AGENTS.md" in am
    assert ".cursor/rules" in am
    assert "--platform cursor" in am
    assert "--platform grok" in am
    assert "Grok Build" in am
    assert "<<<NEXT_CONTEXT>>>" in am  # includes Cursor-worded protocol
    assert "AGENTS.md" in am
    assert "This project is run by Odin" in am
    # Cursor topic shows AGENTS.md in the injected contract, not CLAUDE.md
    assert "project's AGENTS.md" in am
    assert "## 1. The queue" not in am
    # Universal binary flag (not Claude-only)
    assert "--agent-bin" in am
    assert "deprecated" in am.lower() or "alias" in am.lower()

    proto = render("protocol")
    assert "<<<NEXT_CONTEXT>>>" in proto


def test_full_guide_covers_agent_md():
    text = render()
    assert "AGENTS.md" in text
    assert "target-agents-md-snippet.md" in text
    assert "--platform cursor" in text
    assert "--platform grok" in text
    assert "Grok Build" in text
    assert "Cursor CLI" in text
    assert "Claude Code" in text


def test_run_section_has_copy_paste_for_each_product():
    run = render("tasks")  # includes run
    assert "odin run <name> --platform cursor" in run
    assert "odin run <name> --platform grok" in run
    assert "--agent-bin" in run
    assert "--yes" in run


def test_cli_guide_agent_md(capsys):
    rc = main(["guide", "agent-md"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AGENTS.md" in out
    assert ".cursor/rules" in out
    assert "project's AGENTS.md" in out


def test_terminal_topic_is_agent_executable():
    term = render("terminal")
    # The per-project tab-color shell hook and its marker are present.
    assert "iTerm2 per-project tab identity" in term
    assert "PROJECT_TAB_COLOR" in term
    assert "brew install --cask iterm2" in term
    # The opt-in notification flag and the manual GUI step are called out.
    assert "--notify" in term
    assert "Send escape sequence-generated alerts" in term
    # Safety rails for the dotfile edits.
    assert "~/.zshrc" in term
    assert "~/.claude/settings.json" in term
    # The terminal-only view excludes the queue-layout section.
    assert "## 1. The queue" not in term


def test_full_guide_includes_terminal():
    text = render()
    assert "iTerm2 per-project tab identity" in text
    assert "--notify" in text


def test_cli_guide_terminal(capsys):
    rc = main(["guide", "terminal"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "iTerm2 per-project tab identity" in out
    assert "--notify" in out


def test_unknown_topic_falls_back_to_full():
    assert render("nonsense") == render("all") == render()


def test_cli_guide_default(capsys):
    rc = main(["guide"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Authoring tasks for Odin" in out
    assert "NNN-slug.md" in out


def test_cli_guide_topic(capsys):
    rc = main(["guide", "protocol"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<<<NEXT_CONTEXT>>>" in out
    # The queue-layout section should not be in the protocol-only view.
    assert "## 1. The queue" not in out


def test_cli_guide_rejects_bad_topic(capsys):
    # argparse choices → exits (SystemExit) rather than running.
    import pytest

    with pytest.raises(SystemExit):
        main(["guide", "not-a-topic"])
