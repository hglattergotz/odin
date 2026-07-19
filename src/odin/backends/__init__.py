"""Agent backends — one per headless CLI Odin can drive.

Odin's task loop is platform-agnostic. Each backend owns the few pieces that
differ per agent CLI: how the invocation argv + prompt are built, how live
stream events render (including which event is terminal / text deltas), and
how a terminal event normalises into a `RunResult` (including the success
gate). Claude, Cursor, and grok-build are peers registered in
`odin.backends.registry`.
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
