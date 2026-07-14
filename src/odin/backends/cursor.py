"""`CursorBackend` — drives Cursor's headless agent CLI (`agent -p`).

This owns the Cursor-specific pieces of an `odin run`: building the `agent -p`
argv (autonomy flags, `--workspace`, optional `--model` / `--sandbox` /
`--approve-mcps` from config), delivering the injected protocol by **prepending
it to the stdin prompt** (the agent CLI has no `--append-system-prompt` —
proposal §5 / Appendix C.7), and normalising the terminal `result` event into a
`RunResult`. The generic subprocess loop lives in `odin.runner.run_agent`.

The pinned success predicate (proposal §2): Cursor emits **no `stop_reason`**
on success, so the gate keys off the terminal event's presence instead:

    succeeded = (exit_code == 0 and terminal_result_present
                 and event.get("is_error") is not True
                 and bool(final_text))

A missing `is_error` counts as not-an-error. The stream carries no
`total_cost_usd` or `num_turns` (Appendix C.3), so those stay None; the
camelCase `usage` block is mapped to Odin's internal token keys.
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
)

#: Delimiters framing the injected protocol at the top of the stdin prompt
#: (approach A of proposal §5 — no system-prompt flag exists on this CLI).
PROTOCOL_HEADER = (
    "<!-- ODIN_PROTOCOL (injected; takes precedence for task termination "
    "and git policy) -->"
)
PROTOCOL_FOOTER = "<!-- END ODIN_PROTOCOL -->"


def _norm_usage(raw: object) -> dict | None:
    """Map Cursor's camelCase `usage` block to Odin's internal token keys."""
    if not isinstance(raw, dict):
        return None
    return {
        "input": raw.get("inputTokens"),
        "output": raw.get("outputTokens"),
        "cache_read": raw.get("cacheReadTokens"),
        "cache_creation": raw.get("cacheWriteTokens"),
    }


class CursorBackend(AgentBackend):
    """Backend for Cursor's headless agent CLI (`agent`)."""

    name = "cursor"

    def default_binary(self) -> str:
        return "agent"

    def instruction_files(self) -> list[Path]:
        return [Path("AGENTS.md")]

    def build_invoke(
        self,
        prompt: str,
        project_dir: Path,
        system_prompt: str | None,
        run_options: RunOptions,
    ) -> AgentInvokeSpec:
        # Lazy import: odin.config imports the backend registry (for
        # DEFAULT_PLATFORM) and the registry imports this module, so a
        # module-level import here would complete an import cycle.
        from odin import config

        cfg = config.load_config()

        def _cfg(key: str):
            return config.get_in(cfg, f"platforms.{self.name}.{key}")

        cfg_binary = _cfg("binary")
        binary = (
            run_options.binary
            or (cfg_binary.strip() if isinstance(cfg_binary, str) and cfg_binary.strip() else None)
            or self.default_binary()
        )
        argv = [
            binary,
            "-p",
            "--output-format", "stream-json",
            # Full-autonomy posture (proposal §5): a trust/approval prompt in a
            # headless run has no one to answer it and blocks Odin forever, so
            # --force/--trust are always on. Restrict via ~/.cursor config.
            "--force",
            "--trust",
            "--workspace", str(project_dir),
        ]
        if run_options.model:
            argv += ["--model", run_options.model]
        sandbox = _cfg("sandbox")
        if isinstance(sandbox, str) and sandbox.strip():
            argv += ["--sandbox", sandbox.strip()]
        if _cfg("approve_mcps") is True:
            argv.append("--approve-mcps")
        # No system-prompt flag on this CLI — the protocol rides at the top of
        # the stdin prompt instead, framed so the agent (and anyone reading a
        # transcript) can tell injected contract from task body.
        final_prompt = prompt
        if system_prompt:
            final_prompt = (
                f"{PROTOCOL_HEADER}\n{system_prompt}\n{PROTOCOL_FOOTER}\n\n{prompt}"
            )
        return AgentInvokeSpec(argv=argv, prompt=final_prompt, cwd=project_dir)

    def handle_stream_event(
        self,
        event: dict,
        out: TextIO,
        project_dir: Path | None = None,
    ) -> CapturedFields | None:
        """Render one Cursor NDJSON event live; return captured fields or None.

        Init and assistant events share Claude's shape (proposal §6), so the
        rendering matches ClaudeBackend. Cursor's `thinking` events stay off
        the terminal; `tool_call` rendering lands in a later batch (B3) — runs
        work without it, the output is just quieter.
        """
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            _safe_write(out, "  " + style.dim(f"[session {_short_session(sid)}]", out) + "\n")
            return {"session_id": sid}

        if etype == "assistant":
            text = _assistant_text(event)
            if text:
                rendered = _render_agent_text(text, out)
                _safe_write(out, "\n" + style.bullet(style.GLYPH_BULLET, out) + " " + rendered)
                if not rendered.endswith("\n"):
                    _safe_write(out, "\n")
            return None

        if etype == "result":
            # Terminal event (Appendix C.3): no stop_reason/cost/turns; carries
            # is_error, result text, session_id, camelCase usage, durations.
            captured: CapturedFields = {
                "final_text": event.get("result") or "",
                "session_id": event.get("session_id"),
            }
            if event.get("is_error") is True:
                captured["error"] = event.get("subtype") or "agent error"
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

        The pinned Cursor success gate lives here (see module docstring). A
        missing terminal event (e.g. invalid `--model`: exit 1, stderr only —
        Appendix C.4) is a failure, same as Claude silence.
        """
        final_text = ""
        error: str | None = None
        session_id: str | None = None
        usage: dict | None = None
        duration_ms: int | None = None
        api_ms: int | None = None

        if terminal_event is not None:
            final_text = terminal_event.get("result") or ""
            session_id = terminal_event.get("session_id")
            usage = _norm_usage(terminal_event.get("usage"))
            duration_ms = terminal_event.get("duration_ms")
            api_ms = terminal_event.get("duration_api_ms")
            if terminal_event.get("is_error") is True:
                subtype = terminal_event.get("subtype")
                error = subtype if subtype and subtype != "success" else "agent reported is_error"

        succeeded = (
            exit_code == 0
            and terminal_event is not None
            and terminal_event.get("is_error") is not True
            and bool(final_text)
        )
        return RunResult(
            succeeded=succeeded,
            final_text=final_text,
            stop_reason=None,  # absent on Cursor; no synthetic value
            error=error or (stderr.strip() or None if exit_code != 0 else None),
            exit_code=exit_code,
            session_id=session_id,
            platform=self.name,
            wall_ms=wall_ms,
            duration_ms=duration_ms,
            api_ms=api_ms,
            num_turns=None,  # not reported by the agent CLI
            usage=usage,
            cost_usd=None,  # no cost field in the stream (proposal §6)
        )
