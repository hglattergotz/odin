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


def test_claude_invoke_methods_are_stubs():
    backend = get_backend()
    opts = RunOptions()
    with pytest.raises(NotImplementedError):
        backend.build_invoke("p", Path("."), None, opts)
    with pytest.raises(NotImplementedError):
        backend.handle_stream_event({}, out=None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        backend.normalise_result(None, 0, 0, "")


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
