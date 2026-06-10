"""Agent backends — one per headless CLI Odin can drive.

Odin's task loop is platform-agnostic. Each backend owns the few pieces that
differ per agent CLI: how the invocation argv + prompt are built, how live
stream events render, and how a terminal event normalises into a `RunResult`
(including the success gate).

This package is the *skeleton* introduced by Batch A1 of the multi-platform
proposal (`docs/multi-platform-agents-proposal.md`). It changes no runtime
behaviour on its own: `cli.py` and the live invoke path in `runner.py` are
rewired to dispatch through a backend in later batches (A3/A4).
"""

from __future__ import annotations

from odin.backends.base import AgentBackend, AgentInvokeSpec, CapturedFields, RunOptions
from odin.backends.claude import ClaudeBackend
from odin.backends.registry import get_backend

__all__ = [
    "AgentBackend",
    "AgentInvokeSpec",
    "CapturedFields",
    "RunOptions",
    "ClaudeBackend",
    "get_backend",
]
