"""Backend lookup by platform name.

`get_backend(name)` is the single resolution point the CLI and runner use to
turn a `--platform` value into an `AgentBackend`. New platforms register by
adding an entry to `_BACKENDS`. Resolution is case-insensitive; an unknown name
is a hard error (no silent fallback). Every registered backend is a peer.

Platform selection itself lives in `config.resolve_platform` (flag → env →
config). The registry never invents a default product.
"""

from __future__ import annotations

from odin.backends.base import AgentBackend
from odin.backends.claude import ClaudeBackend
from odin.backends.cursor import CursorBackend
from odin.backends.grok import GrokBackend

# name -> zero-arg factory. Factories (not instances) so each call yields a
# fresh backend and registration stays cheap to import.
_BACKENDS: dict[str, type[AgentBackend]] = {
    "claude": ClaudeBackend,
    "cursor": CursorBackend,
    "grok": GrokBackend,
}


def available_platforms() -> list[str]:
    """Sorted list of registered platform names."""
    return sorted(_BACKENDS)


def get_backend(name: str) -> AgentBackend:
    """Return a fresh backend for `name`.

    Raises `ValueError` for an unknown or empty platform.
    """
    if not name or not str(name).strip():
        raise ValueError(
            "platform name is required; available platforms: "
            + ", ".join(available_platforms())
        )
    key = str(name).strip().lower()
    try:
        factory = _BACKENDS[key]
    except KeyError:
        known = ", ".join(available_platforms())
        raise ValueError(
            f"unknown platform {name!r}; available platforms: {known}"
        ) from None
    return factory()
