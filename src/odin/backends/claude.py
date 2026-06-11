"""`ClaudeBackend` — drives `claude -p`, Odin's original (and default) platform.

This owns the Claude-specific pieces of an `odin run`: building the `claude -p`
argv (permission flags, `--append-system-prompt`, optional `--model` /
`--max-turns` / tool allowlists), rendering each NDJSON stream event for the
terminal, and normalising the terminal `result` event into a `RunResult`. The
generic subprocess loop lives in `odin.runner.run_agent`.

The success gate that used to live in `runner.py` lives here now (in
`normalise_result`):

    succeeded = (exit_code == 0 and error is None
                 and stop_reason in {"end_turn", "stop_sequence"}
                 and bool(final_text))
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from odin import style
from odin.backends.base import AgentBackend, AgentInvokeSpec, CapturedFields, RunOptions
from odin.runner import (
    RunResult,
    _assistant_text,
    _render_agent_text,
    _safe_write,
    _short_session,
    _tool_calls,
)

#: Claude stop reasons that count as a clean, complete turn.
_GOOD_STOPS = {"end_turn", "stop_sequence"}


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
        binary = run_options.binary or self.default_binary()
        argv = [
            binary,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", run_options.permission_mode,
        ]
        if run_options.model:
            argv += ["--model", run_options.model]
        # No turn cap by default — an arbitrary limit can kill a healthy,
        # in-progress session (and isn't imposed on an interactive run). Only cap
        # when explicitly asked via max_turns.
        if run_options.max_turns is not None:
            argv += ["--max-turns", str(run_options.max_turns)]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        if run_options.allowed_tools:
            argv += ["--allowed-tools", ",".join(run_options.allowed_tools)]
        if run_options.disallowed_tools:
            argv += ["--disallowed-tools", ",".join(run_options.disallowed_tools)]
        # Prompt rides on stdin (no prepend for Claude — the protocol goes in via
        # --append-system-prompt above).
        return AgentInvokeSpec(argv=argv, prompt=prompt, cwd=project_dir)

    def handle_stream_event(
        self,
        event: dict,
        out: TextIO,
        project_dir: Path | None = None,
    ) -> CapturedFields | None:
        """Render one Claude NDJSON event live; return captured fields or None.

        `project_dir` abbreviates path-type tool details relative to the project;
        it's optional so unit tests can call this without it.
        """
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            _safe_write(out, "  " + style.dim(f"[session {_short_session(sid)}]", out) + "\n")
            return {"session_id": sid}

        if etype == "assistant":
            text = _assistant_text(event)
            if text:
                # Blank line + cyan bullet frames the block. Markdown emphasis is
                # rendered and the <<<...>>> handoff fences are prettified for the
                # terminal (cosmetic only — the protocol is parsed from `result`).
                rendered = _render_agent_text(text, out)
                _safe_write(out, "\n" + style.bullet(style.GLYPH_BULLET, out) + " " + rendered)
                if not rendered.endswith("\n"):
                    _safe_write(out, "\n")
            for name, detail in _tool_calls(event, project_dir):
                line = "   " + style.tool(f"{style.GLYPH_ARROW} {name}", out)
                if detail:
                    line += "  " + style.dim(detail, out)
                _safe_write(out, line + "\n")
            return None

        if etype == "user":
            # Tool results — keep the terminal quiet unless there's an error.
            return None

        if etype == "result":
            # Terminal event. Fields per Claude Code docs:
            #   subtype: "success" | "error_max_turns" | "error_during_execution"
            #   result: final assistant text
            #   stop_reason: end_turn | max_turns | tool_use | ...
            #   is_error: bool
            #   session_id, usage, total_cost_usd
            captured: CapturedFields = {
                "final_text": event.get("result") or "",
                "stop_reason": event.get("stop_reason"),
                "session_id": event.get("session_id"),
            }
            if event.get("is_error") or event.get("subtype") != "success":
                captured["error"] = event.get("subtype") or "unknown error"
            return captured

        return None

    def normalise_result(
        self,
        terminal_event: dict | None,
        exit_code: int,
        wall_ms: int,
        stderr: str,
    ) -> RunResult:
        """Turn the terminal `result` event (or its absence) into a `RunResult`.

        The success gate lives here: Claude is successful when the process exited
        cleanly, the result event reported no error, the stop reason is a clean
        terminal one, and there is final text to parse for a sentinel.
        """
        final_text = ""
        stop_reason: str | None = None
        error: str | None = None
        session_id: str | None = None
        usage: dict | None = None
        cost_usd: float | None = None
        duration_ms: int | None = None
        api_ms: int | None = None
        num_turns: int | None = None

        if terminal_event is not None:
            final_text = terminal_event.get("result") or ""
            stop_reason = terminal_event.get("stop_reason")
            session_id = terminal_event.get("session_id")
            usage = terminal_event.get("usage")
            cost_usd = terminal_event.get("total_cost_usd")
            duration_ms = terminal_event.get("duration_ms")
            api_ms = terminal_event.get("duration_api_ms")
            num_turns = terminal_event.get("num_turns")
            if terminal_event.get("is_error") or terminal_event.get("subtype") != "success":
                error = terminal_event.get("subtype") or "unknown error"

        succeeded = (
            exit_code == 0
            and error is None
            and (stop_reason in _GOOD_STOPS if stop_reason else False)
            and bool(final_text)
        )
        return RunResult(
            succeeded=succeeded,
            final_text=final_text,
            stop_reason=stop_reason,
            error=error or (stderr.strip() or None if exit_code != 0 else None),
            exit_code=exit_code,
            session_id=session_id,
            platform=self.name,
            wall_ms=wall_ms,
            duration_ms=duration_ms,
            api_ms=api_ms,
            num_turns=num_turns,
            usage=usage,
            cost_usd=cost_usd,
        )
