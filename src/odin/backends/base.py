"""The `AgentBackend` interface and the value types backends exchange.

A backend isolates everything platform-specific about driving a headless agent
CLI. The generic subprocess loop (exec, stdin write, concurrent stderr drain,
NDJSON line loop, wall-clock timing) stays in `runner.py`; a backend supplies
only the three platform-specific pieces plus a little metadata:

- `build_invoke(...)`        — argv + final prompt text (prepend vs flag injection)
- `handle_stream_event(...)` — live terminal rendering (tool lines differ per CLI)
- `normalise_result(...)`    — token/cost/stop_reason/`succeeded` from the terminal event
- `default_binary()`         — the CLI name when the user passes no override
- `instruction_files()`      — project instruction files, for startup warnings / lint

See `docs/multi-platform-agents-proposal.md` §2.
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
# nothing. It is primarily a live-display hook; the runner reads the terminal
# `result` event itself and hands it to `normalise_result`. Kept as a plain
# alias so backends can return ordinary dicts.
CapturedFields = dict


@dataclass(frozen=True)
class AgentInvokeSpec:
    """A fully-resolved invocation: what to exec, what to feed it on stdin, where.

    `argv` is the binary plus all flags (it does NOT include the prompt as an
    argument — the prompt is delivered on stdin via `prompt`). `cwd` is the
    target project dir so the project's instruction file loads.
    """

    argv: list[str]
    prompt: str
    cwd: Path


@dataclass(frozen=True)
class RunOptions:
    """Platform-agnostic knobs the loop hands to `build_invoke`.

    These mirror the keyword arguments `runner.run_claude` accepted before the
    backend split, plus
    `model` (the new platform-agnostic `--model`, proposal §3). A backend reads
    only the fields meaningful to it and ignores the rest; platform-specific
    autonomy flags (Cursor's `--force`/`--trust`/`--sandbox`) arrive in a later
    batch. Frozen — the loop builds one per task and never mutates it.
    """

    binary: str | None = None
    model: str | None = None
    permission_mode: str = "bypassPermissions"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None


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
        """Build the argv + final stdin prompt for one task invocation."""
        raise NotImplementedError

    @abstractmethod
    def handle_stream_event(
        self,
        event: dict,
        out: TextIO,
        project_dir: Path | None = None,
    ) -> CapturedFields | None:
        """Render one NDJSON stream event live; return captured fields or None.

        This is a live-display hook. The runner captures the terminal `result`
        event on its own and passes it to `normalise_result`, so the return value
        is advisory (handy for unit-testing what an event yields).
        """
        raise NotImplementedError

    @abstractmethod
    def normalise_result(
        self,
        terminal_event: dict | None,
        exit_code: int,
        wall_ms: int,
        stderr: str,
    ) -> "RunResult":
        """Turn the terminal event (or its absence) into a `RunResult`.

        This is where the success gate lives — each platform decides `succeeded`
        for itself (Claude keys off `stop_reason`; Cursor cannot). The runner
        trusts `RunResult.succeeded` and never re-derives it.
        """
        raise NotImplementedError
