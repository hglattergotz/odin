"""The `AgentBackend` interface and the value types backends exchange.

A backend isolates everything platform-specific about driving a headless agent
CLI. The generic subprocess loop (exec, prompt delivery, concurrent stderr
drain, NDJSON line loop, wall-clock timing) stays in `runner.py`; a backend
supplies only the platform-specific pieces plus a little metadata:

- `build_invoke(...)`        — argv + final prompt text (prepend vs flag injection)
- `handle_stream_event(...)` — live terminal rendering; may mark terminal /
  text deltas via returned `CapturedFields`
- `normalise_result(...)`    — token/cost/stop_reason/`succeeded` from the
  terminal event (+ optional accumulated stream text)
- `default_binary()`         — the CLI name when the user passes no override
- `instruction_files()`      — project instruction files, for startup warnings / lint

Every registered platform (Claude Code, Cursor CLI, Grok Build, …) is a peer that
implements this same interface — there is no first-class backend in the loop.

See `docs/multi-platform-agents-proposal.md` §2 and `docs/agent-backends.md`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:  # avoid an import cycle at runtime — runner imports nothing here
    from odin.runner import RunResult


# A backend's stream handler may return a dict of fields captured from an event
# (e.g. the session id from the init event), or None when an event contributes
# nothing. Recognised optional keys used by the generic loop:
#   - ``terminal`` (truthy) — this event is the run's terminal event
#   - ``text_delta`` (str)  — append to accumulated assistant text (CLIs that
#     stream chunk deltas instead of a whole-message terminal field)
#   - ``final_text`` / other keys — advisory for tests; ``normalise_result``
#     still owns the RunResult
CapturedFields = dict


@dataclass(frozen=True)
class AgentInvokeSpec:
    """A fully-resolved invocation: what to exec, what prompt to feed, where.

    `prompt_via` selects how the generic loop delivers `prompt`:

    - ``"stdin"`` (default) — write `prompt` on the child's stdin (Claude,
      Cursor, and most CLIs).
    - ``"file"`` — write `prompt` to a temp file and append
      ``[prompt_file_flag, <path>]`` to `argv` (Grok Build and similar).

    `argv` must NOT already include the prompt-file flag when `prompt_via` is
    ``"file"`` — the loop owns temp-file lifecycle and appends the flag.
    """

    argv: list[str]
    prompt: str
    cwd: Path
    prompt_via: str = "stdin"  # "stdin" | "file"
    prompt_file_flag: str = "--prompt-file"


@dataclass(frozen=True)
class RunOptions:
    """Platform-agnostic knobs the loop hands to `build_invoke`.

    A backend reads only the fields meaningful to it and ignores the rest.
    `sandbox` and `approve_mcps` are tri-state: None means "not set on the
    CLI", letting the backend fall back to its config section. Frozen — the
    loop builds one per run and never mutates it.
    """

    binary: str | None = None
    model: str | None = None
    permission_mode: str = "bypassPermissions"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    sandbox: str | None = None
    approve_mcps: bool | None = None


class AgentBackend(ABC):
    """Interface every agent-CLI backend implements.

    Concrete backends are resolved by name through `registry.get_backend`.
    Implementer's checklist lives in the proposal §11.
    """

    #: Stable platform identifier — recorded in metrics and used by the registry.
    name: str = ""

    @abstractmethod
    def default_binary(self) -> str:
        """The CLI binary name when the user passes no explicit override."""
        raise NotImplementedError

    @abstractmethod
    def instruction_files(self) -> list[Path]:
        """Project-relative instruction files this platform reads.

        Used for the startup "missing instructions" warning and the
        git-workflow conflict lint. Paths are relative to the project dir.
        """
        raise NotImplementedError

    @abstractmethod
    def build_invoke(
        self,
        prompt: str,
        project_dir: Path,
        system_prompt: str | None,
        run_options: RunOptions,
    ) -> AgentInvokeSpec:
        """Build the argv + final prompt for one task invocation.

        For ``prompt_via="file"`` backends, return argv *without* the
        prompt-file flag; the loop creates the temp file and appends it.
        """
        raise NotImplementedError

    @abstractmethod
    def handle_stream_event(
        self,
        event: dict,
        out: TextIO,
        project_dir: Path | None = None,
    ) -> CapturedFields | None:
        """Render one NDJSON stream event live; return captured fields or None.

        Return ``{"terminal": True, ...}`` when `event` is the run's terminal
        event so the loop can hand it to `normalise_result` (do not assume the
        event type is always ``"result"``). Return ``{"text_delta": "..."}``
        when the CLI streams assistant text as chunk deltas that must be
        concatenated into the final protocol-bearing text.
        """
        raise NotImplementedError

    @abstractmethod
    def normalise_result(
        self,
        terminal_event: dict | None,
        exit_code: int,
        wall_ms: int,
        stderr: str,
        *,
        accumulated_text: str = "",
    ) -> "RunResult":
        """Turn the terminal event (or its absence) into a `RunResult`.

        This is where the success gate lives — each platform decides `succeeded`
        for itself. `accumulated_text` is the concatenation of `text_delta`
        captures from the stream (empty for CLIs that put the full text on the
        terminal event). The runner trusts `RunResult.succeeded` and never
        re-derives it.
        """
        raise NotImplementedError
