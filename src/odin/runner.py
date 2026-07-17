"""Subprocess wrappers around headless agent CLIs (Claude Code, grok-build).

Odin invokes a coding agent in headless streaming mode, one fresh session per
task. The subprocess machinery — spawn, stdin/prompt delivery, concurrent
stderr drain, NDJSON line loop, live display, wall-clock timing — is **generic**
(`run_agent`). Each agent CLI plugs in a small **backend** that owns only the
platform-specific pieces:

  * ``build_cmd``      — argv (flags + prompt delivery)
  * ``handle_event``   — interpret one NDJSON event (display + captured fields)
  * ``succeeded``      — classify the terminal outcome

Backends today:

  * :class:`ClaudeBackend` — ``claude -p --output-format stream-json …`` (prompt
    on **stdin**; terminal ``result`` event; snake_case usage; ``stop_reason``).
  * :class:`GrokBackend`   — ``grok --output-format streaming-json
    --prompt-file …`` (grok does **not** read stdin; terminal ``end`` event;
    text arrives as ``text`` chunk deltas; camelCase ``stopReason``/``sessionId``;
    no ``cache_creation`` token field).

Select a backend with ``get_backend(platform)``. ``run_claude`` remains as a
thin backward-compatible wrapper. No retry, no resume — fresh session per call
by design.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from odin import style


@dataclass(frozen=True)
class RunResult:
    succeeded: bool          # backend-classified terminal outcome
    final_text: str          # text of the terminal assistant message — for protocol.parse
    stop_reason: str | None  # "end_turn"/"EndTurn"/"max_turns"/... (platform-native)
    error: str | None        # error text from the terminal/error event, if any
    exit_code: int
    session_id: str | None
    # Metrics captured from the terminal event + our own timing. All optional so
    # a missing/older terminal event never breaks construction.
    platform: str = "claude"          # which agent backend produced this result
    wall_ms: int = 0                  # Odin-measured subprocess wall time
    duration_ms: int | None = None    # agent-reported total duration (if any)
    api_ms: int | None = None         # agent-reported API time (if any)
    num_turns: int | None = None      # turns the agent took
    usage: dict | None = None         # raw token usage block (platform-native keys)
    cost_usd: float | None = None     # total cost from the terminal event, if any


# ----------------------------------------------------------------------
# backends
# ----------------------------------------------------------------------

class _Backend:
    """Platform-specific pieces of a headless run. Stateless — one instance is
    reused across tasks; per-run accumulation lives in :func:`run_agent`."""

    name: str = "agent"
    default_bin: str = "agent"
    prompt_via: str = "stdin"   # "stdin" | "file"

    def build_cmd(
        self,
        *,
        bin: str,
        permission_mode: str,
        allowed_tools: list[str] | None,
        disallowed_tools: list[str] | None,
        max_turns: int | None,
        system_prompt: str | None,
        prompt_file: Path | None,
    ) -> list[str]:
        raise NotImplementedError

    def handle_event(
        self, event: dict, out: TextIO, project_dir: Path | None = None
    ) -> dict | None:
        raise NotImplementedError

    def succeeded(
        self,
        *,
        exit_code: int,
        error: str | None,
        stop_reason: str | None,
        final_text: str,
        terminal_seen: bool,
    ) -> bool:
        raise NotImplementedError


class ClaudeBackend(_Backend):
    """Claude Code: ``claude -p --output-format stream-json`` with the prompt on
    stdin and a terminal ``result`` event."""

    name = "claude"
    default_bin = "claude"
    prompt_via = "stdin"
    _GOOD_STOPS = {"end_turn", "stop_sequence"}

    def build_cmd(self, *, bin, permission_mode, allowed_tools, disallowed_tools,
                  max_turns, system_prompt, prompt_file):
        cmd = [
            bin,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", permission_mode,
        ]
        # No turn cap by default — an arbitrary limit can kill a healthy,
        # in-progress session. Only cap when explicitly asked.
        if max_turns is not None:
            cmd += ["--max-turns", str(max_turns)]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if allowed_tools:
            cmd += ["--allowed-tools", ",".join(allowed_tools)]
        if disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(disallowed_tools)]
        return cmd

    def handle_event(self, event, out, project_dir=None):
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
            #   is_error: bool; session_id, usage, total_cost_usd
            captured = {
                "terminal": True,
                "final_text": event.get("result") or "",
                "stop_reason": event.get("stop_reason"),
                "session_id": event.get("session_id"),
                "usage": event.get("usage"),
                "cost_usd": event.get("total_cost_usd"),
                "duration_ms": event.get("duration_ms"),
                "api_ms": event.get("duration_api_ms"),
                "num_turns": event.get("num_turns"),
            }
            if event.get("is_error") or event.get("subtype") != "success":
                captured["error"] = event.get("subtype") or "unknown error"
            return captured

        return None

    def succeeded(self, *, exit_code, error, stop_reason, final_text, terminal_seen):
        return (
            exit_code == 0
            and error is None
            and (stop_reason in self._GOOD_STOPS if stop_reason else False)
            and bool(final_text)
        )


class GrokBackend(_Backend):
    """grok-build: ``grok --output-format streaming-json --prompt-file <path>``.

    grok does **not** read the prompt from stdin, so we write it to a temp file
    and pass ``--prompt-file``. The assistant text arrives as ``text`` chunk
    deltas (there is no whole-message ``result`` field); the terminal event is
    ``end`` (camelCase ``stopReason``/``sessionId``, snake_case ``usage.*``).
    Errors surface as a ``{"type":"error"}`` line and/or a non-zero exit.

    ``--rules`` is grok's ``--append-system-prompt`` (append to system prompt);
    ``--tools`` is its allowlist; ``--disallowed-tools`` matches Claude's.
    """

    name = "grok"
    default_bin = "grok"
    prompt_via = "file"

    def build_cmd(self, *, bin, permission_mode, allowed_tools, disallowed_tools,
                  max_turns, system_prompt, prompt_file):
        assert prompt_file is not None, "grok backend requires a prompt file"
        cmd = [
            bin,
            "--output-format", "streaming-json",
            "--permission-mode", permission_mode,
            "--prompt-file", str(prompt_file),
        ]
        if max_turns is not None:
            cmd += ["--max-turns", str(max_turns)]
        if system_prompt:
            # grok's append-to-system-prompt flag (alias: --append-system-prompt).
            cmd += ["--rules", system_prompt]
        if allowed_tools:
            cmd += ["--tools", ",".join(allowed_tools)]
        if disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(disallowed_tools)]
        return cmd

    def handle_event(self, event, out, project_dir=None):
        etype = event.get("type")

        if etype == "text":
            data = event.get("data") or ""
            if data:
                _safe_write(out, data)
            return {"text_delta": data}

        if etype == "thought":
            # Reasoning — cosmetic; not part of the protocol-bearing final text.
            return None

        if etype == "max_turns_reached":
            return {"stop_reason": "max_turns"}

        if etype == "error":
            return {"terminal": True, "error": event.get("message") or "unknown error"}

        if etype == "end":
            # Terminal event. stopReason/sessionId are camelCase; usage keys are
            # snake_case and reuse Claude's names for the shared fields
            # (input_tokens/output_tokens/cache_read_input_tokens) — no
            # cache_creation field — so metrics._norm_usage maps them directly.
            _safe_write(out, "\n")
            return {
                "terminal": True,
                "stop_reason": event.get("stopReason"),
                "session_id": event.get("sessionId"),
                "usage": event.get("usage"),
                "cost_usd": event.get("total_cost_usd"),
                "num_turns": event.get("num_turns"),
            }

        # init/lifecycle (auto_compact_*, image_compressed, …) — ignore.
        return None

    def succeeded(self, *, exit_code, error, stop_reason, final_text, terminal_seen):
        # grok's stopReason values differ from Claude's and a success may omit a
        # "good" stop; classify on exit + a clean terminal `end` + real output
        # (mirrors the multi-platform proposal's non-Claude predicate).
        return (
            exit_code == 0
            and error is None
            and terminal_seen
            and bool(final_text)
        )


_BACKENDS: dict[str, _Backend] = {
    "claude": ClaudeBackend(),
    "grok": GrokBackend(),
}


def get_backend(platform: str) -> _Backend:
    """Resolve a backend by platform name (``claude`` | ``grok``)."""
    try:
        return _BACKENDS[platform]
    except KeyError:
        raise ValueError(
            f"unknown platform {platform!r}; expected one of {sorted(_BACKENDS)}"
        ) from None


def _handle_event(event: dict, out: TextIO, project_dir: Path | None = None) -> dict | None:
    """Backward-compatible module-level alias for the Claude event handler."""
    return _BACKENDS["claude"].handle_event(event, out, project_dir)


# ----------------------------------------------------------------------
# generic run loop
# ----------------------------------------------------------------------

def run_agent(
    prompt: str,
    project_dir: Path,
    *,
    backend: _Backend,
    bin: str | None = None,
    permission_mode: str = "bypassPermissions",
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    system_prompt: str | None = None,
    out: TextIO | None = None,
) -> RunResult:
    """Invoke ``backend``'s headless CLI with ``prompt`` in ``project_dir``.

    ``out`` defaults to sys.stdout — pass a sink for tests. Streaming display is
    best-effort: we never let display failures break the run.
    """
    if out is None:
        out = sys.stdout
    if not project_dir.is_dir():
        raise FileNotFoundError(f"project dir does not exist: {project_dir}")

    bin = bin or backend.default_bin

    # Prompt delivery: stdin (claude) or a temp --prompt-file (grok).
    prompt_file: Path | None = None
    stdin_text: str | None = None
    if backend.prompt_via == "file":
        tf = tempfile.NamedTemporaryFile(
            "w", suffix=".md", prefix="odin-prompt-", delete=False, encoding="utf-8"
        )
        tf.write(prompt)
        tf.close()
        prompt_file = Path(tf.name)
    else:
        stdin_text = prompt

    cmd = backend.build_cmd(
        bin=bin,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        max_turns=max_turns,
        system_prompt=system_prompt,
        prompt_file=prompt_file,
    )

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_dir),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        # Drain stderr on a background thread so a child that fills its ~64KB
        # stderr pipe never blocks (which stalls the whole session).
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr is not None:
                for chunk in proc.stderr:
                    stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                # The CLI exited before reading our prompt; we'll see it via wait().
                pass

        final_text = ""
        stop_reason: str | None = None
        error: str | None = None
        session_id: str | None = None
        usage: dict | None = None
        cost_usd: float | None = None
        duration_ms: int | None = None
        api_ms: int | None = None
        num_turns: int | None = None
        terminal_seen = False

        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                _safe_write(out, "   " + style.dim(f"[non-JSON] {line}", out) + "\n")
                continue
            captured = backend.handle_event(event, out, project_dir)
            if captured is None:
                continue
            if captured.get("terminal"):
                terminal_seen = True
            # Text: claude sets the whole thing on `result`; grok appends deltas.
            if "final_text" in captured:
                final_text = captured.get("final_text") or final_text
            if captured.get("text_delta"):
                final_text += captured["text_delta"]
            stop_reason = captured.get("stop_reason") or stop_reason
            error = captured.get("error") or error
            session_id = captured.get("session_id") or session_id
            if captured.get("usage") is not None:
                usage = captured["usage"]
            if captured.get("cost_usd") is not None:
                cost_usd = captured["cost_usd"]
            if captured.get("duration_ms") is not None:
                duration_ms = captured["duration_ms"]
            if captured.get("api_ms") is not None:
                api_ms = captured["api_ms"]
            if captured.get("num_turns") is not None:
                num_turns = captured["num_turns"]

        exit_code = proc.wait()
        wall_ms = int((time.monotonic() - start) * 1000)
        stderr_thread.join(timeout=5)
        stderr_text = "".join(stderr_chunks)
        if stderr_text.strip():
            _safe_write(out, "   " + style.dim(f"[{backend.name} stderr]", out) + "\n")
            for sline in stderr_text.rstrip("\n").split("\n"):
                _safe_write(out, "   " + style.dim(sline, out) + "\n")

        succeeded = backend.succeeded(
            exit_code=exit_code,
            error=error,
            stop_reason=stop_reason,
            final_text=final_text,
            terminal_seen=terminal_seen,
        )
        return RunResult(
            succeeded=succeeded,
            final_text=final_text,
            stop_reason=stop_reason,
            error=error or (stderr_text.strip() or None if exit_code != 0 else None),
            exit_code=exit_code,
            session_id=session_id,
            platform=backend.name,
            wall_ms=wall_ms,
            duration_ms=duration_ms,
            api_ms=api_ms,
            num_turns=num_turns,
            usage=usage,
            cost_usd=cost_usd,
        )
    finally:
        if prompt_file is not None:
            try:
                prompt_file.unlink()
            except OSError:
                pass


def run_claude(
    prompt: str,
    project_dir: Path,
    *,
    claude_bin: str = "claude",
    permission_mode: str = "bypassPermissions",
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    system_prompt: str | None = None,
    out: TextIO | None = None,
) -> RunResult:
    """Backward-compatible wrapper: run a task through Claude Code.

    Retained so existing callers/tests keep working; new code should call
    :func:`run_agent` with an explicit backend from :func:`get_backend`.
    """
    return run_agent(
        prompt,
        project_dir,
        backend=_BACKENDS["claude"],
        bin=claude_bin,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        max_turns=max_turns,
        system_prompt=system_prompt,
        out=out,
    )


# ----------------------------------------------------------------------
# shared display helpers (platform-neutral)
# ----------------------------------------------------------------------

def _assistant_text(event: dict) -> str:
    """Extract text content from an assistant event's message blocks."""
    msg = event.get("message") or {}
    content = msg.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                parts.append(text)
    return "".join(parts)


# The protocol sentinel is parsed from the terminal event (see protocol.parse);
# what we print here is purely cosmetic — Markdown emphasis rendered as ANSI and
# the <<<...>>> handoff fences shown as a clean, labelled, indented block.

_SENTINEL_LABEL = {
    "NEXT_CONTEXT": "carry-forward to the next task",
    "NEEDS_INPUT": "needs input",
    "FOLLOW_UP": "follow-up work discovered",
}
_SENTINEL_GLYPH = {"NEXT_CONTEXT": "↪", "NEEDS_INPUT": style.GLYPH_HELD, "FOLLOW_UP": "+"}
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_HEADING_RE = re.compile(r"^(\s*)#{1,6}\s+(.*)$")
_SENTINEL_OPEN_RE = re.compile(r"^<<<(NEXT_CONTEXT|NEEDS_INPUT|FOLLOW_UP)>>>$")


def _emphasize(line: str, out: TextIO) -> str:
    """Inline Markdown for the terminal: `**x**` / `__x__` -> bold; `# Heading`
    -> bold with the leading `#`s dropped."""
    m = _HEADING_RE.match(line)
    if m:
        return m.group(1) + style.paint(m.group(2), style.BOLD, out=out)
    return _BOLD_RE.sub(
        lambda mm: style.paint(mm.group(1) or mm.group(2), style.BOLD, out=out), line
    )


def _render_agent_text(text: str, out: TextIO) -> str:
    """Prettify the agent's streamed message for display (cosmetic only)."""
    rendered: list[str] = []
    in_code = in_sentinel = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            rendered.append(line)
            continue
        if in_code:
            rendered.append(line)
            continue
        m = _SENTINEL_OPEN_RE.match(line.strip())
        if m:
            kind = m.group(1)
            rendered.append(
                "  " + style.dim(f"{_SENTINEL_GLYPH.get(kind, '·')} "
                                 f"{_SENTINEL_LABEL.get(kind, kind.lower())}:", out)
            )
            in_sentinel = True
            continue
        if line.strip() == "<<<END>>>":
            in_sentinel = False
            continue
        if in_sentinel:
            rendered.append("    " + style.dim(line, out))
            continue
        rendered.append(_emphasize(line, out))
    return "\n".join(rendered)


# Per-tool, the input key worth showing on the activity line.
_TOOL_ARG = {
    "Read": "file_path", "Write": "file_path", "Edit": "file_path",
    "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
    "Bash": "command", "Glob": "pattern", "Grep": "pattern",
    "LS": "path", "WebFetch": "url", "WebSearch": "query",
    "Task": "description", "Agent": "description",
}
_PATH_KEYS = {"file_path", "notebook_path", "path"}


def _tool_calls(
    event: dict, project_dir: Path | None = None
) -> list[tuple[str, str]]:
    """(name, detail) for each tool_use block; detail is a one-line key arg."""
    msg = event.get("message") or {}
    content = msg.get("content") or []
    out: list[tuple[str, str]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name") or "?"
            out.append((name, _tool_detail(name, block.get("input"), project_dir)))
    return out


def _tool_detail(name: str, inp: object, project_dir: Path | None = None) -> str:
    """A short, single-line summary of a tool call's main argument."""
    if not isinstance(inp, dict):
        return ""
    key = _TOOL_ARG.get(name)
    val = inp.get(key) if key else None
    if not isinstance(val, str) or not val.strip():
        val = next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")
        key = None
    is_path = key in _PATH_KEYS
    if is_path:
        val = _abbrev_path(val, project_dir)
    collapsed = " ".join(val.split())
    return _truncate(collapsed, is_path=is_path)


def _abbrev_path(val: str, project_dir: Path | None) -> str:
    """Show a path relative to the project dir (or `~`) when possible."""
    try:
        p = Path(val)
        if not p.is_absolute():
            return val
        if project_dir is not None:
            try:
                return str(p.relative_to(project_dir))
            except ValueError:
                pass
        try:
            return "~/" + str(p.relative_to(Path.home()))
        except ValueError:
            return val
    except Exception:
        return val


def _short_session(sid: object, n: int = 8) -> str:
    """Truncated session id for the dim init line — `957ba4b3…`."""
    s = str(sid)
    return s[:n] + "…" if len(s) > n else s


def _truncate(s: str, limit: int = 72, *, is_path: bool = False) -> str:
    if len(s) <= limit:
        return s
    return ("…" + s[-(limit - 1):]) if is_path else (s[: limit - 1] + "…")


def _safe_write(out: TextIO, text: str) -> None:
    try:
        out.write(text)
        out.flush()
    except Exception:
        pass
