"""Read-only configuration loading for platform and model selection.

Odin reads an optional user config file on every `odin run` to supply defaults
for the agent platform and model. The file lives at `$ODIN_HOME/config.toml`
(default `~/.odin/config.toml`); `$ODIN_CONFIG` overrides the full path. A
missing file is not an error — it yields empty defaults (platform falls back to
`"claude"`, model unset).

This module is **read only**. Writing (`odin config set …`) lands in a later
batch with a hand-rolled TOML writer; nothing here mutates the file.

Resolution order (proposal §3):

- platform: `--platform` flag → `$ODIN_PLATFORM` → `default_platform` in config
  → `"claude"`.
- model: `--model` flag → `$ODIN_MODEL` → `platforms.<platform>.model` in config
  → `None` (unset; no `--model` emitted to the CLI).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from odin.backends.registry import DEFAULT_PLATFORM


def odin_home() -> Path:
    """The `$ODIN_HOME` directory (default `~/.odin`). Not created here."""
    env = os.environ.get("ODIN_HOME")
    if env and env.strip():
        return Path(env.strip()).expanduser()
    return Path.home() / ".odin"


def config_path() -> Path:
    """Resolved config file path: `$ODIN_CONFIG` else `$ODIN_HOME/config.toml`."""
    override = os.environ.get("ODIN_CONFIG")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return odin_home() / "config.toml"


def load_config(path: Path | None = None) -> dict:
    """Parse the config TOML; missing or unreadable file → empty dict.

    Best-effort: a malformed file is swallowed and treated as empty so a typo
    in config can never sink a run (resolution then falls back to defaults).
    """
    target = path if path is not None else config_path()
    try:
        with open(target, "rb") as fh:
            return tomllib.load(fh)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return {}
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _platform_section(config: dict, platform: str) -> dict:
    """The `[platforms.<platform>]` table, or `{}` if absent/malformed."""
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        section = platforms.get(platform)
        if isinstance(section, dict):
            return section
    return {}


def resolve_platform(
    cli_flag: str | None = None,
    *,
    config: dict | None = None,
) -> str:
    """Resolve the active platform per the order above (always lowercased)."""
    if cli_flag and cli_flag.strip():
        return cli_flag.strip().lower()
    env = os.environ.get("ODIN_PLATFORM")
    if env and env.strip():
        return env.strip().lower()
    if config is None:
        config = load_config()
    default = config.get("default_platform")
    if isinstance(default, str) and default.strip():
        return default.strip().lower()
    return DEFAULT_PLATFORM


def resolve_model(
    cli_flag: str | None = None,
    *,
    platform: str = DEFAULT_PLATFORM,
    config: dict | None = None,
) -> str | None:
    """Resolve the model per the order above; `None` means "unset" (CLI default).

    Model IDs are case-sensitive, so the value is only stripped, never
    lowercased. `platform` selects which `[platforms.<platform>.model]` to read.
    """
    if cli_flag and cli_flag.strip():
        return cli_flag.strip()
    env = os.environ.get("ODIN_MODEL")
    if env and env.strip():
        return env.strip()
    if config is None:
        config = load_config()
    model = _platform_section(config, platform).get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None
