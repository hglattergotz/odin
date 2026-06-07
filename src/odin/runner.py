"""Subprocess wrapper around `claude -p`.

We invoke Claude Code in headless streaming mode:

    claude -p --output-format stream-json --verbose --permission-mode <mode> \
        [--allowed-tools <csv>] --max-turns <n>

The prompt is passed on stdin so we never deal with shell quoting. Each
stream-json event is parsed; assistant text snippets are surfaced to the
user's terminal as they arrive, and the final `result` event is captured
for the orchestrator to classify.

No retry, no resume — fresh session per call by design.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from odin import style


@dataclass(frozen=True)
class RunResult:
    succeeded: bool          # exit==0, no error event, stop_reason in good set
    final_text: str          # text of the terminal assistant message — for protocol.parse
    stop_reason: str | None  # "end_turn", "max_turns", "error", ...
    error: str | None        # error text from the result event, if any
    exit_code: int
    session_id: str | None
    # Metrics captured from the terminal `result` event + our own timing.
    # All optional so a missing/older result event never breaks construction.
    wall_ms: int = 0                  # Odin-measured subprocess wall time
    duration_ms: int | None = None    # Claude-reported total duration
    api_ms: int | None = None         # Claude-reported API time
    num_turns: int | None = None      # turns the agent took
    usage: dict | None = None         # raw token usage block
    cost_usd: float | None = None     # total_cost_usd from the result event


_GOOD_STOPS = {"end_turn", "stop_sequence"}


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
    """Invoke `claude -p` with `prompt` in `project_dir`.

    `out` defaults to sys.stdout — pass a sink for tests. Streaming output
    is best-effort: we never let display failures break the run.
    """
    if out is None:
        out = sys.stdout

    if not project_dir.is_dir():
        raise FileNotFoundError(f"project dir does not exist: {project_dir}")

    cmd = [
        claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode,
    ]
    # No turn cap by default — an arbitrary limit can kill a healthy, in-progress
    # session (and isn't imposed on an interactive run). Only cap when explicitly
    # asked via max_turns.
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if allowed_tools:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]
    if disallowed_tools:
        cmd += ["--disallowed-tools", ",".join(disallowed_tools)]

    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None

    # Drain stderr on a background thread. If we only read stdout in the loop
    # below and the child writes more than the OS pipe buffer (~64 KB) to
    # stderr, the child blocks on that write and the whole session stalls —
    # which the agent perceives as "tool outputs are delayed" and reacts to by
    # spamming probe commands. Concurrent draining removes that back-pressure.
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is not None:
            for chunk in proc.stderr:
                stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        proc.stdin.write(prompt)
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

    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            _safe_write(out, "   " + style.dim(f"[non-JSON] {line}", out) + "\n")
            continue
        captured = _handle_event(event, out, project_dir)
        if captured is not None:
            final_text = captured.get("final_text", final_text) or final_text
            stop_reason = captured.get("stop_reason", stop_reason) or stop_reason
            error = captured.get("error", error) or error
            session_id = captured.get("session_id", session_id) or session_id
            # Metrics fields appear only on the terminal `result` event.
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
        _safe_write(out, "   " + style.dim("[claude stderr]", out) + "\n")
        for sline in stderr_text.rstrip("\n").split("\n"):
            _safe_write(out, "   " + style.dim(sline, out) + "\n")

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
        error=error or (stderr_text.strip() or None if exit_code != 0 else None),
        exit_code=exit_code,
        session_id=session_id,
        wall_ms=wall_ms,
        duration_ms=duration_ms,
        api_ms=api_ms,
        num_turns=num_turns,
        usage=usage,
        cost_usd=cost_usd,
    )


# ----------------------------------------------------------------------
# event handling
# ----------------------------------------------------------------------

def _handle_event(
    event: dict, out: TextIO, project_dir: Path | None = None
) -> dict | None:
    """Render the event to the user and, if it's a terminal event, return
    a dict of captured fields. Non-terminal events return None.

    `project_dir` is used to abbreviate path-type tool details relative to the
    project; it's optional so tests can call this without it.
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
        captured = {
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


# ----------------------------------------------------------------------
# display-only prettifying of the agent's message text
# ----------------------------------------------------------------------
# The protocol sentinel is parsed from the terminal `result` event (see
# protocol.parse); what we print here is purely cosmetic. So we render Markdown
# emphasis as ANSI (or strip the markers when color is off) and show the
# <<<...>>> handoff fences as a clean, labelled, indented block.

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
    """Inline Markdown for the terminal: `**x**` / `__x__` -> bold (the bare text
    when color is off); `# Heading` -> bold with the leading `#`s dropped."""
    m = _HEADING_RE.match(line)
    if m:
        return m.group(1) + style.paint(m.group(2), style.BOLD, out=out)
    return _BOLD_RE.sub(
        lambda mm: style.paint(mm.group(1) or mm.group(2), style.BOLD, out=out), line
    )


def _render_agent_text(text: str, out: TextIO) -> str:
    """Prettify the agent's streamed message for display (cosmetic only).

    Reformats the `<<<NEXT_CONTEXT>>> … <<<END>>>` handoff fences (and
    NEEDS_INPUT / FOLLOW_UP) into a dim, labelled, indented block — dropping the
    raw markers — and renders Markdown emphasis. Fenced code blocks pass through
    untouched. The protocol itself is parsed from the `result` event, not this.
    """
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
    """A short, single-line summary of a tool call's main argument.

    Path-type details are abbreviated relative to `project_dir` when given.
    """
    if not isinstance(inp, dict):
        return ""
    key = _TOOL_ARG.get(name)
    val = inp.get(key) if key else None
    if not isinstance(val, str) or not val.strip():
        # Fall back to the first non-empty string argument, if any.
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
    # For paths, keep the tail (filename); for everything else, keep the head.
    return ("…" + s[-(limit - 1):]) if is_path else (s[: limit - 1] + "…")


def _safe_write(out: TextIO, text: str) -> None:
    try:
        out.write(text)
        out.flush()
    except Exception:
        pass
