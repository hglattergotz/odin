"""Tests for the agent-backend skeleton + registry (Batch A1).

These cover only the wiring introduced by A1: the registry resolves the
default Claude backend and rejects unknown platforms, and the Claude stub
exposes its metadata. The invoke/stream/result methods are not yet implemented
(Batch A3), so we only assert they exist and raise `NotImplementedError`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from odin.backends import AgentBackend, ClaudeBackend, get_backend
from odin.backends.base import AgentInvokeSpec, RunOptions
from odin.backends.registry import DEFAULT_PLATFORM, available_platforms


def test_default_resolves_to_claude():
    backend = get_backend()
    assert isinstance(backend, ClaudeBackend)
    assert isinstance(backend, AgentBackend)
    assert backend.name == "claude"


def test_explicit_claude_resolves():
    assert isinstance(get_backend("claude"), ClaudeBackend)


def test_platform_name_is_case_insensitive():
    assert isinstance(get_backend("Claude"), ClaudeBackend)
    assert isinstance(get_backend("  CLAUDE  "), ClaudeBackend)


def test_unknown_platform_raises():
    with pytest.raises(ValueError, match="unknown platform"):
        get_backend("cursor")


def test_unknown_platform_message_lists_available():
    with pytest.raises(ValueError) as excinfo:
        get_backend("nope")
    assert "claude" in str(excinfo.value)


def test_each_call_yields_a_fresh_instance():
    assert get_backend() is not get_backend()


def test_default_platform_is_available():
    assert DEFAULT_PLATFORM in available_platforms()


def test_claude_metadata():
    backend = get_backend()
    assert backend.default_binary() == "claude"
    assert backend.instruction_files() == [Path("CLAUDE.md")]


def test_build_invoke_baseline_argv():
    backend = ClaudeBackend()
    spec = backend.build_invoke("the prompt", Path("/proj"), None, RunOptions())
    assert spec.prompt == "the prompt"        # prompt rides on stdin, unchanged
    assert spec.cwd == Path("/proj")
    assert spec.argv[0] == "claude"           # default binary
    assert spec.argv[1] == "-p"
    assert "--output-format" in spec.argv and "stream-json" in spec.argv
    assert "--verbose" in spec.argv
    i = spec.argv.index("--permission-mode")
    assert spec.argv[i + 1] == "bypassPermissions"
    # No optional flags unless asked.
    for flag in ("--model", "--max-turns", "--append-system-prompt",
                 "--allowed-tools", "--disallowed-tools"):
        assert flag not in spec.argv


def test_build_invoke_optional_flags():
    backend = ClaudeBackend()
    opts = RunOptions(
        binary="/path/to/claude",
        model="claude-sonnet-4-6",
        max_turns=42,
        allowed_tools=["Read", "Bash"],
        disallowed_tools=["WebFetch"],
        permission_mode="acceptEdits",
    )
    spec = backend.build_invoke("p", Path("/proj"), "CONTRACT", opts)
    assert spec.argv[0] == "/path/to/claude"
    i = spec.argv.index("--model")
    assert spec.argv[i + 1] == "claude-sonnet-4-6"
    j = spec.argv.index("--max-turns")
    assert spec.argv[j + 1] == "42"
    k = spec.argv.index("--append-system-prompt")
    assert spec.argv[k + 1] == "CONTRACT"
    assert spec.argv[spec.argv.index("--allowed-tools") + 1] == "Read,Bash"
    assert spec.argv[spec.argv.index("--disallowed-tools") + 1] == "WebFetch"
    assert spec.argv[spec.argv.index("--permission-mode") + 1] == "acceptEdits"


def test_normalise_result_success():
    backend = ClaudeBackend()
    event = {
        "type": "result", "subtype": "success", "stop_reason": "end_turn",
        "is_error": False, "result": "done\n<<<NEXT_CONTEXT>>>\nx\n<<<END>>>",
        "session_id": "sess-9", "usage": {"input_tokens": 3},
        "total_cost_usd": 0.5, "duration_ms": 100, "duration_api_ms": 90,
        "num_turns": 4,
    }
    r = backend.normalise_result(event, exit_code=0, wall_ms=123, stderr="")
    assert r.succeeded is True
    assert r.platform == "claude"
    assert r.stop_reason == "end_turn"
    assert r.session_id == "sess-9"
    assert r.cost_usd == 0.5
    assert r.api_ms == 90 and r.duration_ms == 100 and r.num_turns == 4
    assert r.usage == {"input_tokens": 3}
    assert r.wall_ms == 123
    assert r.error is None


def test_normalise_result_no_terminal_event_with_nonzero_exit():
    backend = ClaudeBackend()
    r = backend.normalise_result(None, exit_code=2, wall_ms=5, stderr="boom\n")
    assert r.succeeded is False
    assert r.exit_code == 2
    assert r.error == "boom"          # falls back to stderr on a failed exit
    assert r.final_text == ""


def test_normalise_result_error_subtype():
    backend = ClaudeBackend()
    event = {"type": "result", "subtype": "error_max_turns",
             "stop_reason": "max_turns", "is_error": True, "result": "hit limit"}
    r = backend.normalise_result(event, exit_code=0, wall_ms=1, stderr="")
    assert r.succeeded is False
    assert r.error == "error_max_turns"
    assert r.stop_reason == "max_turns"


def test_normalise_result_bad_stop_reason_fails():
    backend = ClaudeBackend()
    # Clean exit, no error, but a non-terminal stop reason and text present:
    # tool_use is not a "done" stop, so this is not a success.
    event = {"type": "result", "subtype": "success", "stop_reason": "tool_use",
             "is_error": False, "result": "still going"}
    r = backend.normalise_result(event, exit_code=0, wall_ms=1, stderr="")
    assert r.succeeded is False


def test_invoke_spec_is_frozen():
    spec = AgentInvokeSpec(argv=["claude", "-p"], prompt="hi", cwd=Path("."))
    with pytest.raises(Exception):
        spec.argv = []  # type: ignore[misc]  # frozen dataclass


def test_run_options_defaults():
    opts = RunOptions()
    assert opts.permission_mode == "bypassPermissions"
    assert opts.allowed_tools == []
    assert opts.disallowed_tools == []
    assert opts.model is None
    # mutable defaults must not be shared across instances
    assert RunOptions().allowed_tools is not opts.allowed_tools
