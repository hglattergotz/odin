"""Backend lookup by platform name.

`get_backend(name)` is the single resolution point the CLI and runner use to
turn a `--platform` value into an `AgentBackend`. New platforms register by
adding an entry to `_BACKENDS`. Resolution is case-insensitive; an unknown name
is a hard error (no silent fallback — a typo'd `--platform` should fail loudly,
not quietly run Claude).
"""

from __future__ import annotations

from odin.backends.base import AgentBackend
from odin.backends.claude import ClaudeBackend

#: The default platform when none is specified — preserves today's behaviour.
DEFAULT_PLATFORM = "claude"

# name -> zero-arg factory. Factories (not instances) so each call yields a
# fresh backend and registration stays cheap to import.
_BACKENDS: dict[str, type[AgentBackend]] = {
    "claude": ClaudeBackend,
}


def available_platforms() -> list[str]:
    """Sorted list of registered platform names."""
    return sorted(_BACKENDS)


def get_backend(name: str | None = None) -> AgentBackend:
    """Return a fresh backend for `name` (defaults to `"claude"`).

    Raises `ValueError` for an unknown platform.
    """
    key = (name or DEFAULT_PLATFORM).strip().lower()
    try:
        factory = _BACKENDS[key]
    except KeyError:
        known = ", ".join(available_platforms())
        raise ValueError(
            f"unknown platform {name!r}; available platforms: {known}"
        ) from None
    return factory()
