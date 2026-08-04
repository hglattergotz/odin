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
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:  # avoid an import cycle at runtime — runner imports nothing here
    from odin.runner import RunResult


class FailureKind(str, Enum):
    """Why a run did not succeed — the routing decision.

    INTERRUPTED: something external stopped the agent mid-turn (usage limit,
    hard kill). The work is probably fine as far as it got, so the task is
    recoverable — see `docs/interruption-recovery-proposal.md`.

    DEFECT: the agent finished its turn on its own terms and produced something
    Odin could not use (no sentinel block, malformed output). A human should
    look at it.

    CONFIG: the provider rejected the request outright — an unknown model, a
    bad key, no access. No work was attempted, so there is nothing to recover
    and nothing wrong with the task. Treating this as an interruption is what
    made a typo'd `--model` commit the working tree and strand the task in
    `interrupted/`; the task must stay exactly where it was.
    """

    INTERRUPTED = "interrupted"
    DEFECT = "defect"
    CONFIG = "config"


@dataclass(frozen=True)
class Failure:
    """A classified non-success, with whatever detail the backend could recover.

    `confidence` is "confirmed" when a recognised provider message named the
    cause, "probable" when only the structural rule fired. `reason` is one of
    "usage_limit" / "process_died" / "unknown". `resets_at`, when present, is
    when the provider said the limit lifts — used to offer a wait.
    """

    kind: FailureKind
    confidence: str = "probable"
    reason: str = "unknown"
    detail: str = ""
    resets_at: datetime | None = None

    @property
    def interrupted(self) -> bool:
        return self.kind is FailureKind.INTERRUPTED

    @property
    def config_error(self) -> bool:
        return self.kind is FailureKind.CONFIG


def ended_on_agents_terms(result: "RunResult") -> bool:
    """Did the agent's turn end because the agent decided it was done?

    The platform-agnostic half of interruption detection, and the reason
    classification needs no message pattern-matching to be correct: a clean exit
    with a non-error terminal event means the agent stopped by choice, and
    anything else means something external stopped it. Backends layer
    provider-specific *enrichment* (which limit, when it resets) on top, but
    routing never depends on that enrichment succeeding.
    """
    return result.exit_code == 0 and result.error is None and bool(result.final_text)


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

    #: Public product name, for help output and docs ("Claude Code" for
    #: `claude`). Prefer this in anything a user reads; `name` is the key they
    #: type. Falls back to `name` when a backend leaves it unset.
    product: str = ""

    def config_keys(self) -> list[str]:
        """Dotted config keys this backend reads from `[platforms.<name>]`.

        Every backend honours `binary` and `model`; a backend with extra knobs
        (Cursor's sandbox / MCP approval) extends the list. Surfaced by
        `odin platforms` so the config vocabulary is discoverable without
        reading the source.
        """
        return [f"platforms.{self.name}.binary", f"platforms.{self.name}.model"]

    def platform_flags(self) -> list[str]:
        """`odin run` flags that apply *only* to this platform, for display.

        Purely informational — the flags themselves are declared on the parser
        and warned about in `cli._warn_ignored_platform_flags`.
        """
        return []

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

    def validate_model(self, model: str) -> str | None:
        """Reject a model name that cannot be right, before the run starts.

        Returns an error message, or None when the name is plausible. A shape
        check only — Odin cannot know a provider's live catalogue, and refusing
        a model that actually works would be worse than letting the provider
        say so. It exists to catch the typo (`opus-claude-5` for
        `claude-opus-5`) that otherwise costs a queue move and a WIP commit
        before anyone finds out.

        Backends that cannot say anything useful should not override it.
        """
        return None

    def model_help(self) -> str:
        """One line naming the accepted `--model` forms, for error messages."""
        return ""

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

    def classify_failure(self, result: "RunResult") -> Failure:
        """Why did this non-successful run fail? Called only when not succeeded.

        The default is DEFECT — today's behaviour, where every failure routes to
        `failed/` for a human. A backend opts into interruption recovery by
        overriding this; `ended_on_agents_terms` gives the platform-agnostic
        structural rule, so an implementation is usually a few lines plus
        whatever provider-message enrichment the CLI makes available.
        """
        return Failure(kind=FailureKind.DEFECT, confidence="probable", reason="unknown")
