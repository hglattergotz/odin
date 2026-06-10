"""`ClaudeBackend` — drives `claude -p`, Odin's original (and default) platform.

This is the Batch A1 *stub*. The method bodies that build the invocation,
render the stream, and normalise the result are wired up in Batch A3, when the
generic loop in `runner.py` is split out and the Claude-specific logic that
lives in `run_claude` / `_handle_event` today moves here verbatim. The success
predicate to relocate is `runner.py`'s:

    succeeded = (exit_code == 0 and error is None
                 and stop_reason in {"end_turn", "stop_sequence"}
                 and bool(final_text))

Until then only the metadata methods (`default_binary`, `instruction_files`)
are live so the backend is concrete and the registry can resolve it without
changing any runtime behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from odin.backends.base import AgentBackend, AgentInvokeSpec, CapturedFields, RunOptions

if TYPE_CHECKING:
    from odin.runner import RunResult


class ClaudeBackend(AgentBackend):
    """Backend for Anthropic's Claude Code CLI (`claude`)."""

    name = "claude"

    def default_binary(self) -> str:
        return "claude"

    def instruction_files(self) -> list[Path]:
        return [Path("CLAUDE.md")]

    def build_invoke(
        self,
        prompt: str,
        project_dir: Path,
        system_prompt: str | None,
        run_options: RunOptions,
    ) -> AgentInvokeSpec:
        raise NotImplementedError("ClaudeBackend.build_invoke lands in Batch A3")

    def handle_stream_event(
        self,
        event: dict,
        out: TextIO,
        project_dir: Path | None = None,
    ) -> CapturedFields | None:
        raise NotImplementedError("ClaudeBackend.handle_stream_event lands in Batch A3")

    def normalise_result(
        self,
        terminal_event: dict | None,
        exit_code: int,
        wall_ms: int,
        stderr: str,
    ) -> "RunResult":
        raise NotImplementedError("ClaudeBackend.normalise_result lands in Batch A3")
