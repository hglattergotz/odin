"""Tests for the agent backends + registry.

Covers the registry (default resolution, unknown-platform errors), the Claude
backend (argv building, stream rendering hooks, the relocated success gate),
and the Cursor backend (Batch B1–B2: `--workspace`/autonomy argv, protocol
prepend instead of `--append-system-prompt`, and the pinned Cursor success
predicate with camelCase-usage normalisation; Batch B3: one-line `tool_call`
activity rendering).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from odin.backends import AgentBackend, ClaudeBackend, CursorBackend, get_backend
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
        get_backend("kiro")


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
    # Cursor autonomy knobs are tri-state: None = not set on the CLI.
    assert opts.sandbox is None
    assert opts.approve_mcps is None
    # mutable defaults must not be shared across instances
    assert RunOptions().allowed_tools is not opts.allowed_tools


# ----------------------------------------------------------------------
# CursorBackend (Batch B1–B2)
# ----------------------------------------------------------------------

# The terminal `result` event verbatim from the proposal's empirical run
# (Appendix C.3): no stop_reason, no total_cost_usd, no num_turns; camelCase
# usage; is_error present-and-false on success.
_CURSOR_RESULT = {
    "type": "result",
    "subtype": "success",
    "duration_ms": 4993,
    "duration_api_ms": 4993,
    "is_error": False,
    "result": "pong",
    "session_id": "7bfeef23-655d-4191-bdf4-61b9bdc0621f",
    "request_id": "0cc5c6e7-6e8e-4035-851c-d888633106d8",
    "usage": {
        "inputTokens": 10760,
        "outputTokens": 46,
        "cacheReadTokens": 448,
        "cacheWriteTokens": 0,
    },
}


def test_cursor_resolves_from_registry():
    backend = get_backend("cursor")
    assert isinstance(backend, CursorBackend)
    assert isinstance(backend, AgentBackend)
    assert "cursor" in available_platforms()


def test_cursor_metadata():
    backend = CursorBackend()
    assert backend.name == "cursor"
    assert backend.default_binary() == "agent"
    assert backend.instruction_files() == [Path("AGENTS.md")]


def test_cursor_build_invoke_baseline_argv():
    spec = CursorBackend().build_invoke("the prompt", Path("/proj"), None, RunOptions())
    assert spec.argv[0] == "agent"            # default binary
    assert spec.argv[1] == "-p"
    assert "--output-format" in spec.argv and "stream-json" in spec.argv
    # Full-autonomy posture is always on — a trust prompt would hang headless.
    assert "--force" in spec.argv and "--trust" in spec.argv
    assert spec.argv[spec.argv.index("--workspace") + 1] == "/proj"
    # Optional/foreign flags absent unless configured.
    for flag in ("--model", "--sandbox", "--approve-mcps",
                 "--append-system-prompt", "--permission-mode",
                 "--max-turns", "--verbose", "--resume", "--continue"):
        assert flag not in spec.argv
    assert spec.prompt == "the prompt"        # no protocol → no prepend
    assert spec.cwd == Path("/proj")


def test_cursor_protocol_is_prepended_not_a_flag():
    spec = CursorBackend().build_invoke(
        "task body", Path("/proj"), "THE CONTRACT", RunOptions()
    )
    assert "--append-system-prompt" not in spec.argv
    assert "THE CONTRACT" not in " ".join(spec.argv)
    assert spec.prompt.startswith("<!-- ODIN_PROTOCOL")
    assert "<!-- END ODIN_PROTOCOL -->" in spec.prompt
    assert spec.prompt.index("THE CONTRACT") < spec.prompt.index("task body")
    assert spec.prompt.endswith("task body")


def test_cursor_model_flag():
    opts = RunOptions(model="composer-2.5-fast")
    spec = CursorBackend().build_invoke("p", Path("/proj"), None, opts)
    assert spec.argv[spec.argv.index("--model") + 1] == "composer-2.5-fast"


def test_cursor_config_supplies_binary_sandbox_approve_mcps(monkeypatch):
    # conftest points ODIN_HOME at a fresh temp dir, so this config is isolated.
    monkeypatch.delenv("ODIN_CONFIG", raising=False)
    home = Path(os.environ["ODIN_HOME"])
    (home / "config.toml").write_text(
        "[platforms.cursor]\n"
        'binary = "/opt/cursor/agent"\n'
        'sandbox = "disabled"\n'
        "approve_mcps = true\n",
        encoding="utf-8",
    )
    spec = CursorBackend().build_invoke("p", Path("/proj"), None, RunOptions())
    assert spec.argv[0] == "/opt/cursor/agent"
    assert spec.argv[spec.argv.index("--sandbox") + 1] == "disabled"
    assert "--approve-mcps" in spec.argv


def test_cursor_run_options_sandbox_and_approve_mcps_beat_config(monkeypatch):
    # CLI flags (threaded through RunOptions, B4) win over the config section.
    monkeypatch.delenv("ODIN_CONFIG", raising=False)
    home = Path(os.environ["ODIN_HOME"])
    (home / "config.toml").write_text(
        '[platforms.cursor]\nsandbox = "enabled"\n', encoding="utf-8"
    )
    opts = RunOptions(sandbox="disabled", approve_mcps=True)
    spec = CursorBackend().build_invoke("p", Path("/proj"), None, opts)
    assert spec.argv[spec.argv.index("--sandbox") + 1] == "disabled"
    assert "--approve-mcps" in spec.argv


def test_cursor_run_options_none_defers_to_config(monkeypatch):
    # Tri-state: an unset CLI flag (None) falls back to the config value.
    monkeypatch.delenv("ODIN_CONFIG", raising=False)
    home = Path(os.environ["ODIN_HOME"])
    (home / "config.toml").write_text(
        "[platforms.cursor]\napprove_mcps = true\n", encoding="utf-8"
    )
    opts = RunOptions(sandbox=None, approve_mcps=None)
    spec = CursorBackend().build_invoke("p", Path("/proj"), None, opts)
    assert "--approve-mcps" in spec.argv
    assert "--sandbox" not in spec.argv


def test_cursor_binary_override_beats_config(monkeypatch):
    monkeypatch.delenv("ODIN_CONFIG", raising=False)
    home = Path(os.environ["ODIN_HOME"])
    (home / "config.toml").write_text(
        '[platforms.cursor]\nbinary = "/opt/cursor/agent"\n', encoding="utf-8"
    )
    opts = RunOptions(binary="/explicit/agent")
    spec = CursorBackend().build_invoke("p", Path("/proj"), None, opts)
    assert spec.argv[0] == "/explicit/agent"


def test_cursor_normalise_success_without_stop_reason():
    r = CursorBackend().normalise_result(
        dict(_CURSOR_RESULT), exit_code=0, wall_ms=5100, stderr=""
    )
    assert r.succeeded is True
    assert r.platform == "cursor"
    assert r.final_text == "pong"
    assert r.stop_reason is None              # absent on Cursor; not faked
    assert r.error is None
    assert r.session_id == "7bfeef23-655d-4191-bdf4-61b9bdc0621f"
    assert r.usage == {
        "input": 10760, "output": 46, "cache_read": 448, "cache_creation": 0,
    }
    assert r.cost_usd is None                 # no CLI cost field
    assert r.num_turns is None
    assert r.duration_ms == 4993 and r.api_ms == 4993
    assert r.wall_ms == 5100


def test_cursor_normalise_missing_is_error_counts_as_not_an_error():
    event = {k: v for k, v in _CURSOR_RESULT.items() if k != "is_error"}
    r = CursorBackend().normalise_result(event, exit_code=0, wall_ms=1, stderr="")
    assert r.succeeded is True


def test_cursor_normalise_is_error_true_fails():
    event = dict(_CURSOR_RESULT, is_error=True, subtype="error")
    r = CursorBackend().normalise_result(event, exit_code=0, wall_ms=1, stderr="")
    assert r.succeeded is False
    assert r.error == "error"


def test_cursor_normalise_no_terminal_event_fails():
    # Invalid --model: exit 1, error on stderr only, no result event (C.4).
    r = CursorBackend().normalise_result(
        None, exit_code=1, wall_ms=2, stderr="Cannot use this model: nope\n"
    )
    assert r.succeeded is False
    assert r.error == "Cannot use this model: nope"
    assert r.final_text == ""


def test_cursor_normalise_empty_final_text_fails():
    event = dict(_CURSOR_RESULT, result="")
    r = CursorBackend().normalise_result(event, exit_code=0, wall_ms=1, stderr="")
    assert r.succeeded is False


def test_cursor_normalise_nonzero_exit_fails():
    r = CursorBackend().normalise_result(
        dict(_CURSOR_RESULT), exit_code=1, wall_ms=1, stderr=""
    )
    assert r.succeeded is False


def test_cursor_stream_thinking_and_unrecognisable_tool_call_are_quiet():
    import io

    backend = CursorBackend()
    sink = io.StringIO()
    assert backend.handle_stream_event({"type": "thinking", "text": "hmm"}, sink) is None
    # Empty / malformed payloads: nothing worth a line, and never a crash.
    assert backend.handle_stream_event(
        {"type": "tool_call", "subtype": "started", "tool_call": {}}, sink
    ) is None
    assert backend.handle_stream_event(
        {"type": "tool_call", "subtype": "started", "tool_call": "junk"}, sink
    ) is None
    assert backend.handle_stream_event(
        {"type": "tool_call", "subtype": "started"}, sink
    ) is None
    assert sink.getvalue() == ""


def _cursor_tool_event(kind: str, args: dict, subtype: str = "started") -> dict:
    return {
        "type": "tool_call",
        "subtype": subtype,
        "call_id": "call-1",
        "tool_call": {kind: {"args": args}},
    }


def test_cursor_stream_read_tool_call_renders_relative_path():
    import io

    sink = io.StringIO()
    CursorBackend().handle_stream_event(
        _cursor_tool_event("readToolCall", {"path": "/proj/src/app.py"}),
        sink,
        project_dir=Path("/proj"),
    )
    assert sink.getvalue() == "   → Read  src/app.py\n"


def test_cursor_stream_edit_and_write_tool_calls_render():
    import io

    sink = io.StringIO()
    backend = CursorBackend()
    backend.handle_stream_event(
        _cursor_tool_event("editToolCall", {"path": "notes.md"}), sink
    )
    backend.handle_stream_event(
        _cursor_tool_event("writeToolCall", {"path": "out.txt"}), sink
    )
    assert sink.getvalue() == "   → Edit  notes.md\n   → Write  out.txt\n"


def test_cursor_stream_shell_tool_call_renders_command():
    import io

    sink = io.StringIO()
    CursorBackend().handle_stream_event(
        _cursor_tool_event("shellToolCall", {"command": "ls  -la\n"}), sink
    )
    # Whitespace collapses to one line; commands are not path-abbreviated.
    assert sink.getvalue() == "   → Shell  ls -la\n"


def test_cursor_stream_unknown_tool_kind_degrades_to_generic_line():
    import io

    sink = io.StringIO()
    CursorBackend().handle_stream_event(
        _cursor_tool_event("grepToolCall", {"pattern": "TODO"}), sink
    )
    assert sink.getvalue() == "   → Grep  TODO\n"


def test_cursor_stream_tool_call_completed_is_quiet():
    import io

    sink = io.StringIO()
    CursorBackend().handle_stream_event(
        _cursor_tool_event(
            "shellToolCall", {"command": "ls"}, subtype="completed"
        ),
        sink,
    )
    assert sink.getvalue() == ""


def test_cursor_stream_tool_call_without_key_arg_falls_back_to_first_string():
    import io

    sink = io.StringIO()
    CursorBackend().handle_stream_event(
        _cursor_tool_event("shellToolCall", {"cwd": "/tmp", "timeout": 5}), sink
    )
    assert sink.getvalue() == "   → Shell  /tmp\n"


def test_cursor_stream_result_captures_fields():
    import io

    captured = CursorBackend().handle_stream_event(dict(_CURSOR_RESULT), io.StringIO())
    assert captured == {
        "final_text": "pong",
        "session_id": "7bfeef23-655d-4191-bdf4-61b9bdc0621f",
    }
