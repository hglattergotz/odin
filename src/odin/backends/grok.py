"""`GrokBackend` — drives grok-build's headless CLI (`grok`).

grok-build does **not** read the prompt from stdin. Odin writes the task prompt
to a temp file and the generic loop appends `--prompt-file <path>` (see
`AgentInvokeSpec.prompt_via`). Assistant text arrives as ``{"type":"text",
"data":…}`` chunk deltas (accumulated by the loop); the terminal event is
``{"type":"end", …}`` with camelCase ``stopReason``/``sessionId`` and
snake_case ``usage.*`` (no ``cache_creation`` field). Protocol injection uses
``--rules`` (grok's append-to-system-prompt); the tool allowlist flag is
``--tools``.

Success gate (mirrors the non-Claude predicate in the multi-platform proposal):

    succeeded = (exit_code == 0 and terminal_end_present
                 and error is None and bool(final_text))

Verified against grok-build's streaming-json contract (see
`docs/agent-backends.md`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from odin.backends.base import AgentBackend, AgentInvokeSpec, CapturedFields, RunOptions
from odin.runner import RunResult, _safe_write


class GrokBackend(AgentBackend):
    """Backend for the grok-build headless CLI (`grok`)."""

    name = "grok"

    def default_binary(self) -> str:
        return "grok"

    def instruction_files(self) -> list[Path]:
        # grok-build historically followed Claude-shaped project instructions;
        # warn on CLAUDE.md until a grok-specific convention is documented.
        return [Path("CLAUDE.md")]

    def build_invoke(
        self,
        prompt: str,
        project_dir: Path,
        system_prompt: str | None,
        run_options: RunOptions,
    ) -> AgentInvokeSpec:
        from odin import config

        cfg = config.load_config()
        cfg_binary = config.get_in(cfg, f"platforms.{self.name}.binary")
        binary = (
            run_options.binary
            or (cfg_binary.strip() if isinstance(cfg_binary, str) and cfg_binary.strip() else None)
            or self.default_binary()
        )
        argv = [
            binary,
            "--output-format", "streaming-json",
            "--permission-mode", run_options.permission_mode,
        ]
        if run_options.model:
            argv += ["--model", run_options.model]
        if run_options.max_turns is not None:
            argv += ["--max-turns", str(run_options.max_turns)]
        if system_prompt:
            # grok's append-to-system-prompt flag (alias: --append-system-prompt).
            argv += ["--rules", system_prompt]
        if run_options.allowed_tools:
            argv += ["--tools", ",".join(run_options.allowed_tools)]
        if run_options.disallowed_tools:
            argv += ["--disallowed-tools", ",".join(run_options.disallowed_tools)]
        # Prompt rides in a temp file; the loop appends --prompt-file.
        return AgentInvokeSpec(
            argv=argv,
            prompt=prompt,
            cwd=project_dir,
            prompt_via="file",
            prompt_file_flag="--prompt-file",
        )

    def handle_stream_event(
        self,
        event: dict,
        out: TextIO,
        project_dir: Path | None = None,
    ) -> CapturedFields | None:
        etype = event.get("type")

        if etype == "text":
            data = event.get("data") or ""
            if data:
                _safe_write(out, data)
            return {"text_delta": data} if data else None

        if etype == "thought":
            # Reasoning — cosmetic; not part of the protocol-bearing final text.
            return None

        if etype == "max_turns_reached":
            return {"stop_reason": "max_turns"}

        if etype == "error":
            return {
                "terminal": True,
                "error": event.get("message") or "unknown error",
            }

        if etype == "end":
            # Terminal event. stopReason/sessionId are camelCase; usage keys are
            # snake_case and reuse Claude's names for the shared token fields.
            _safe_write(out, "\n")
            return {
                "terminal": True,
                "stop_reason": event.get("stopReason"),
                "session_id": event.get("sessionId"),
            }

        # init/lifecycle — ignore.
        return None

    def normalise_result(
        self,
        terminal_event: dict | None,
        exit_code: int,
        wall_ms: int,
        stderr: str,
        *,
        accumulated_text: str = "",
    ) -> RunResult:
        """Classify a grok-build run from the `end`/`error` event + text deltas."""
        final_text = accumulated_text or ""
        stop_reason: str | None = None
        error: str | None = None
        session_id: str | None = None
        usage: dict | None = None
        cost_usd: float | None = None
        num_turns: int | None = None

        if terminal_event is not None:
            etype = terminal_event.get("type")
            if etype == "error":
                error = terminal_event.get("message") or "unknown error"
            else:
                # `end` (or any other terminal marker)
                stop_reason = terminal_event.get("stopReason")
                session_id = terminal_event.get("sessionId")
                usage = terminal_event.get("usage")
                cost_usd = terminal_event.get("total_cost_usd")
                num_turns = terminal_event.get("num_turns")

        succeeded = (
            exit_code == 0
            and error is None
            and terminal_event is not None
            and terminal_event.get("type") != "error"
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
            duration_ms=None,
            api_ms=None,
            num_turns=num_turns,
            usage=usage,
            cost_usd=cost_usd,
        )
