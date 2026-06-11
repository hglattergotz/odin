"""Generic headless-agent subprocess loop.

Odin drives every agent CLI the same way: spawn it in headless streaming mode,
feed the prompt on stdin (so we never deal with shell quoting), parse the NDJSON
event stream line by line, surface assistant text and tool activity to the
user's terminal as it arrives, and capture the terminal `result` event for the
orchestrator to classify.

Everything *platform-specific* — building the argv, rendering each stream event,
and normalising the terminal event into a `RunResult` (including the success
gate) — lives in an `AgentBackend` (see `odin.backends`). `run_agent` owns only
the platform-agnostic machinery: process exec, the concurrent stderr drain,
the NDJSON line loop, and wall-clock timing.

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
from typing import TYPE_CHECKING, TextIO

from odin import style

if TYPE_CHECKING:  # import only for typing — avoids loading the backends package
    from odin.backends.base import AgentBackend, RunOptions


@dataclass(frozen=True)
class RunResult:
    succeeded: bool          # exit==0, no error event, stop_reason in good set
    final_text: str          # text of the terminal assistant message — for protocol.parse
    stop_reason: str | None  # "end_turn", "max_turns", "error", ...
    error: str | None        # error text from the result event, if any
    exit_code: int
    session_id: str | None
    platform: str = "claude"  # which backend produced this result
    # Metrics captured from the terminal `result` event + our own timing.
    # All optional so a missing/older result event never breaks construction.
    wall_ms: int = 0                  # Odin-measured subprocess wall time
    duration_ms: int | None = None    # Claude-reported total duration
    api_ms: int | None = None         # Claude-reported API time
    num_turns: int | None = None      # turns the agent took
    usage: dict | None = None         # raw token usage block
    cost_usd: float | None = None     # total_cost_usd from the result event


def run_agent(
    prompt: str,
    project_dir: Path,
    backend: "AgentBackend",
    *,
    system_prompt: str | None = None,
    run_options: "RunOptions | None" = None,
    out: TextIO | None = None,
) -> RunResult:
    """Run one task through `backend` in `project_dir`, returning its `RunResult`.

    The platform-agnostic loop: ask the backend to build the invocation, exec it,
    write the prompt on stdin, drain stderr concurrently, dispatch each NDJSON
    event to the backend for live display, and hand the terminal `result` event
    (or its absence) to the backend's `normalise_result` — which owns the success
    gate. `out` defaults to sys.stdout; pass a sink for tests. Streaming display
    is best-effort: we never let display failures break the run.
    """
    if out is None:
        out = sys.stdout
    if run_options is None:
        # Lazy import keeps `odin.backends` off runner's module-load path, so the
        # backends package (which imports runner) can't form an import cycle.
        from odin.backends.base import RunOptions
        run_options = RunOptions()

    if not project_dir.is_dir():
        raise FileNotFoundError(f"project dir does not exist: {project_dir}")

    spec = backend.build_invoke(prompt, project_dir, system_prompt, run_options)

    start = time.monotonic()
    proc = subprocess.Popen(
        spec.argv,
        cwd=str(spec.cwd),
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
        proc.stdin.write(spec.prompt)
        proc.stdin.close()
    except BrokenPipeError:
        # The CLI exited before reading our prompt; we'll see it via wait().
        pass

    # The terminal event (NDJSON `result`) carries the final text, stop info, and
    # metrics. We dispatch every event to the backend for live display and keep
    # the last `result`-typed event to hand to `normalise_result`. The terminal
    # `result` event is the cross-platform NDJSON convention (proposal §6); a
    # backend that needs different framing can ignore what we capture here.
    terminal_event: dict | None = None

    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            _safe_write(out, "   " + style.dim(f"[non-JSON] {line}", out) + "\n")
            continue
        backend.handle_stream_event(event, out, project_dir)
        if event.get("type") == "result":
            terminal_event = event

    exit_code = proc.wait()
    wall_ms = int((time.monotonic() - start) * 1000)
    stderr_thread.join(timeout=5)
    stderr_text = "".join(stderr_chunks)
    if stderr_text.strip():
        _safe_write(out, "   " + style.dim("[stderr]", out) + "\n")
        for sline in stderr_text.rstrip("\n").split("\n"):
            _safe_write(out, "   " + style.dim(sline, out) + "\n")

    return backend.normalise_result(terminal_event, exit_code, wall_ms, stderr_text)


# ----------------------------------------------------------------------
# stream rendering helpers (shared across backends)
# ----------------------------------------------------------------------
# These primitives render the parts of the NDJSON stream that look the same
# across agent CLIs — assistant text blocks, the session-init line, tool-call
# activity lines. A backend's `handle_stream_event` decides which events route
# to which primitive (Cursor frames tool activity differently, for example).

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
