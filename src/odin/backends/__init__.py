"""Agent backends — one per headless coding-agent product Odin can drive.

Odin's task loop is platform-agnostic. Each backend owns the few pieces that
differ per agent CLI: how the invocation argv + prompt are built, how live
stream events render (including which event is terminal / text deltas), and
how a terminal event normalises into a `RunResult` (including the success
gate). Peers today (public product → `--platform` → binary):

- **Claude Code** → `claude` → `claude` (`ClaudeBackend`)
- **Cursor CLI** → `cursor` → `agent` (`CursorBackend`)
- **Grok Build** → `grok` → `grok` (`GrokBackend`)

See `docs/agent-backends.md`.
"""

from __future__ import annotations

from odin.backends.base import AgentBackend, AgentInvokeSpec, CapturedFields, RunOptions
from odin.backends.claude import ClaudeBackend
from odin.backends.cursor import CursorBackend
from odin.backends.grok import GrokBackend
from odin.backends.registry import get_backend

__all__ = [
    "AgentBackend",
    "AgentInvokeSpec",
    "CapturedFields",
    "RunOptions",
    "ClaudeBackend",
    "CursorBackend",
    "GrokBackend",
    "get_backend",
]
