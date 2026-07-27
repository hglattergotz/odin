"""`ClaudeBackend` — drives **Claude Code** (Anthropic), binary `claude`.

Public product: [Claude Code](https://code.claude.com/docs).
`--platform claude` selects this backend.

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

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odin import style
from odin.backends.base import (
    AgentBackend,
    AgentInvokeSpec,
    CapturedFields,
    Failure,
    FailureKind,
    RunOptions,
    ended_on_agents_terms,
)
from odin.runner import (
    RunResult,
    _assistant_text,
    _render_agent_text,
    _safe_write,
    _short_session,
    _tool_calls,
    _write_tool_line,
)

#: Claude stop reasons that count as a clean, complete turn.
_GOOD_STOPS = {"end_turn", "stop_sequence"}


def _error_label(terminal_event: dict) -> str | None:
    """Human-meaningful error label for a terminal `result` event, or None.

    Claude Code can set ``is_error`` while leaving ``subtype`` at ``"success"``
    — the signature of a session cut short (usage limit, hard kill) rather than
    a clean failure. Naming the subtype in that case produced the nonsense
    ``error: success``, so say what actually happened instead. Everything else
    keeps the original semantics: a real subtype passes through, and an absent
    one is "unknown error".
    """
    subtype = terminal_event.get("subtype")
    if not (terminal_event.get("is_error") or subtype != "success"):
        return None
    if subtype and subtype != "success":
        return subtype
    if subtype == "success":
        # Only reachable with is_error truthy — the interrupted-session shape.
        return "agent reported is_error with subtype=success"
    return "unknown error"


# ----------------------------------------------------------------------
# model-name shape check (pre-flight; see AgentBackend.validate_model)
# ----------------------------------------------------------------------
#: Family aliases Claude Code accepts in place of a full model name.
_MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")

#: Deliberately shape-based, never a catalogue: Odin cannot know which models
#: exist today, and a list would rot into false rejections of models that work.
#: What it *can* say is that a name is not one of the two documented forms.
#: The version suffix must start with a digit, which is what separates
#: `opus-4.5` (a real alias+version) from `opus-claude-5` (a transposition).
_MODEL_SHAPE = re.compile(
    r"""^(?:
          claude[-.].+                       # full name: claude-opus-5, claude-3-5-…
        | .*anthropic\..+                    # bedrock / vertex ids
        | (?:opus|sonnet|haiku|fable)        # family alias…
          (?:plan)?                          #   …incl. opusplan
          (?:[-.]\d[\w.\-]*)?                #   …with an optional version
          (?:\[[\w.]+\])?                    #   …and an optional [1m]-style tag
        )$""",
    re.VERBOSE | re.IGNORECASE,
)


def _api_error_status(terminal_event: dict) -> int | None:
    """HTTP status when the provider rejected the request, else None.

    Claude Code puts `api_error_status` on the terminal event and sets
    `terminal_reason: "api_error"`. This is the difference between "your
    request was refused" (a 4xx: unknown model, bad key, no access) and "the
    agent was cut off" — a distinction the `is_error`/`subtype` pair cannot
    make, since a 404 arrives labelled `subtype: "success"`.
    """
    status = terminal_event.get("api_error_status")
    if isinstance(status, bool) or not isinstance(status, (int, float)):
        return None
    return int(status)


# ----------------------------------------------------------------------
# provider-limit recognition (enrichment only — never routing)
# ----------------------------------------------------------------------
# Odin decides *that* a run was interrupted structurally (see
# `ended_on_agents_terms`); these patterns only add *why* and *until when*.
# An unrecognised limit notice still classifies as interrupted — it just says
# reason="unknown" and offers no wait. Extend freely; nothing depends on a
# match succeeding.

_LIMIT_PATTERNS = (
    re.compile(r"usage limit reached", re.I),
    re.compile(r"session limit", re.I),
    re.compile(r"\brate[ _-]?limit", re.I),
    re.compile(r"quota (?:exceeded|exhausted)", re.I),
    re.compile(r"\b429\b"),
    re.compile(r"resets?\s+(?:at\s+)?\d", re.I),
)

#: `Claude AI usage limit reached|1753462800` — the most reliable form there is.
_EPOCH_RE = re.compile(r"limit reached\|(\d{10,13})\b", re.I)

#: `resets 3pm (America/New_York)`, `resets at 15:00`, `resets 3:30 pm`
_CLOCK_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
    r"(?:\s*\(([A-Za-z_]+/[A-Za-z_+\-]+)\))?",
    re.I,
)

#: The line worth showing the user, if we can find one.
_DETAIL_RE = re.compile(r"^.*(?:usage limit|session limit|rate.?limit).*$", re.I | re.M)


def parse_reset_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """Extract the moment a provider limit lifts, or None if not stated.

    Handles the epoch form and the human clock form (with or without an explicit
    IANA zone). A bare clock time is read in the stated zone, else local time,
    and rolled to tomorrow when it has already passed today. Returns an aware
    datetime. Never raises — an unparseable notice just yields None.
    """
    if not text:
        return None
    now = now or datetime.now().astimezone()

    m = _EPOCH_RE.search(text)
    if m:
        raw = int(m.group(1))
        secs = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None

    m = _CLOCK_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").replace(".", "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    tz = now.tzinfo
    if m.group(4):
        try:
            tz = ZoneInfo(m.group(4))
        except (ZoneInfoNotFoundError, ValueError):
            pass  # unknown zone name — fall back to local

    local_now = now.astimezone(tz)
    reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= local_now:
        reset += timedelta(days=1)  # stated time already passed → it means tomorrow
    return reset


def _limit_detail(text: str) -> str:
    """The most useful single line from a limit notice, trimmed."""
    m = _DETAIL_RE.search(text or "")
    return " ".join(m.group(0).split())[:200] if m else ""


class ClaudeBackend(AgentBackend):
    """Backend for Anthropic's Claude Code CLI (`claude`)."""

    name = "claude"

    def default_binary(self) -> str:
        return "claude"

    def instruction_files(self) -> list[Path]:
        return [Path("CLAUDE.md")]

    def validate_model(self, model: str) -> str | None:
        if _MODEL_SHAPE.match(model.strip()):
            return None
        return f"'{model}' does not look like a Claude Code model name"

    def model_help(self) -> str:
        return (
            "Accepted: an alias (" + ", ".join(_MODEL_ALIASES) + "), optionally "
            "with a version (opus-4.5), or a full name (claude-opus-5). "
            "Omit --model to use Claude Code's own default."
        )

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
                _write_tool_line(out, name, detail)
            # Hand the text to the loop's bounded tail buffer as well. Normally
            # the terminal `result` event carries the whole final message and
            # this is unused — but when the CLI is killed hard there IS no
            # terminal event, and this tail is then the only record of what the
            # agent was doing (see normalise_result's fallback).
            return {"text_delta": text} if text else None

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
            # Mark terminal so the generic loop does not hard-code type=="result".
            captured: CapturedFields = {
                "terminal": True,
                "final_text": event.get("result") or "",
                "stop_reason": event.get("stop_reason"),
                "session_id": event.get("session_id"),
            }
            error = _error_label(event)
            if error is not None:
                captured["error"] = error
            return captured

        return None

    def classify_failure(self, result: RunResult) -> Failure:
        """Interrupted vs. defect, in three tiers (proposal §4).

        1. A recognised provider-limit notice → INTERRUPTED, "confirmed", with
           the reset time when the notice carries one.
        2. The turn did not end on the agent's own terms → INTERRUPTED,
           "probable". This is the tier that makes the classification correct
           without any message matching; tier 1 only enriches it.
        3. Otherwise the agent finished and broke the protocol → DEFECT.
        """
        # `--max-turns` is the user's own circuit breaker, not something
        # external cutting the agent off. Recovering it would commit the partial
        # work and re-run straight back into the same cap — so it stays a defect
        # the user is told about, exactly as before.
        if result.error == "error_max_turns":
            return Failure(
                kind=FailureKind.DEFECT,
                confidence="confirmed",
                reason="max_turns",
                detail="hit the --max-turns cap mid-work",
            )

        # The provider refused the request. A 4xx that is not 429 says the run
        # never started — unknown model, bad key, no access — so there is no
        # partial work and nothing to recover. 429 is a rate/usage limit and
        # 5xx is the provider having a bad day: both are interruptions, so they
        # fall through to the tiers below.
        status = result.api_error_status
        if status is not None and 400 <= status < 500 and status != 429:
            return Failure(
                kind=FailureKind.CONFIG,
                confidence="confirmed",
                reason=f"api_{status}",
                detail=(result.final_text or "").strip()[:300],
            )

        haystack = f"{result.final_text}\n{result.stderr}"
        if any(p.search(haystack) for p in _LIMIT_PATTERNS):
            return Failure(
                kind=FailureKind.INTERRUPTED,
                confidence="confirmed",
                reason="usage_limit",
                detail=_limit_detail(haystack),
                resets_at=parse_reset_time(haystack),
            )
        if not ended_on_agents_terms(result):
            return Failure(
                kind=FailureKind.INTERRUPTED,
                confidence="probable",
                reason="unknown",
                detail=(result.error or "").strip()[:200],
            )
        return Failure(
            kind=FailureKind.DEFECT,
            confidence="probable",
            reason="no_sentinel",
            detail="the agent ended its turn without emitting a sentinel block",
        )

    def normalise_result(
        self,
        terminal_event: dict | None,
        exit_code: int,
        wall_ms: int,
        stderr: str,
        *,
        accumulated_text: str = "",
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
        api_status: int | None = None

        if terminal_event is not None:
            final_text = terminal_event.get("result") or ""
            stop_reason = terminal_event.get("stop_reason")
            session_id = terminal_event.get("session_id")
            usage = terminal_event.get("usage")
            cost_usd = terminal_event.get("total_cost_usd")
            duration_ms = terminal_event.get("duration_ms")
            api_ms = terminal_event.get("duration_api_ms")
            num_turns = terminal_event.get("num_turns")
            error = _error_label(terminal_event)
            api_status = _api_error_status(terminal_event)

        # A hard kill (SIGKILL, OOM, dropped connection) produces no terminal
        # event at all, leaving nothing to diagnose or to build a resumption
        # brief from. Fall back to the streamed tail so the agent's last words
        # survive. Never overrides a real terminal `result`.
        if not final_text:
            final_text = accumulated_text

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
            stderr=stderr,
            wall_ms=wall_ms,
            duration_ms=duration_ms,
            api_ms=api_ms,
            num_turns=num_turns,
            usage=usage,
            cost_usd=cost_usd,
            api_error_status=api_status,
        )
