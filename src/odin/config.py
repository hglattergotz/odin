"""Read-only configuration loading for platform and model selection.

Odin reads an optional user config file on every `odin run` to supply defaults
for the agent platform and model. The file lives at `$ODIN_HOME/config.toml`
(default `~/.odin/config.toml`); `$ODIN_CONFIG` overrides the full path. A
missing file is not an error. There is **no built-in default platform**: you
must set `--platform`, `$ODIN_PLATFORM`, or `default_platform` in config.

Reading uses stdlib `tomllib` (Python 3.11+). Writing (`odin config set …`)
uses a **hand-rolled minimal TOML writer** below — `tomllib` is read-only and
`tomli-w` is forbidden by the supply-chain rules (no new dependencies). The
writer covers the flat/small schema Odin emits (top-level scalars + nested
`[platforms.<p>]` tables of scalars); it is not a general TOML serialiser.

Resolution order:

- platform: `--platform` flag → `$ODIN_PLATFORM` → `default_platform` in config
  → error if unset (no silent product default).
- model: `--model` flag → `$ODIN_MODEL` → `platforms.<platform>.model` in config
  → `None` (unset; no `--model` emitted to the CLI).
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path

#: Curated model suggestions per platform for the interactive picker. These
#: drift over time, so the picker always also offers free-text entry and an
#: "unset" choice — this list is convenience, never a validation allowlist.
MODEL_SUGGESTIONS: dict[str, list[str]] = {
    "claude": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "cursor": ["composer-2.5-fast", "composer-2.5", "cursor-grok-4.5-high"],
    "grok": [],  # free-text / unset — Grok Build model ids drift with the CLI
}


class PlatformRequiredError(ValueError):
    """Raised when no platform was set via flag, env, or config."""


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
    in config can never sink a run.
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
    """Resolve the active platform (always lowercased).

    Raises `PlatformRequiredError` when flag, env, and config are all unset —
    Odin never assumes Claude Code (or any other product) by default.
    """
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
    raise PlatformRequiredError(
        "platform is required: pass --platform {claude,cursor,grok}, "
        "set $ODIN_PLATFORM, or `odin config set default_platform …`"
    )


def try_resolve_platform(
    cli_flag: str | None = None,
    *,
    config: dict | None = None,
) -> str | None:
    """Like `resolve_platform`, but returns None instead of raising."""
    try:
        return resolve_platform(cli_flag, config=config)
    except PlatformRequiredError:
        return None


def resolve_model(
    cli_flag: str | None = None,
    *,
    platform: str,
    config: dict | None = None,
) -> str | None:
    """Resolve the model for `platform`; `None` means "unset" (CLI default).

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


# ---------------------------------------------------------------------------
# dotted-key access (get/set on the nested config dict)
# ---------------------------------------------------------------------------

def get_in(config: dict, dotted_key: str):
    """Read a dotted key (e.g. ``platforms.claude.model``); ``None`` if absent.

    Traversal stops at the first missing segment or non-dict node, so a typo'd
    or partial key simply yields ``None`` rather than raising.
    """
    cur = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_in(config: dict, dotted_key: str, value) -> dict:
    """Set a dotted key, creating intermediate tables, and return ``config``.

    Merges in place: sibling keys are preserved. A path segment whose existing
    value is not a table is replaced by a fresh table (the new key wins).
    """
    parts = dotted_key.split(".")
    cur = config
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return config


def parse_value(raw: str):
    """Coerce a CLI string into a TOML scalar: bool, int, float, else string.

    ``true``/``false`` (any case) → bool; an integer literal → int; a decimal
    literal → float; anything else stays a string. Model IDs are free text and
    almost never pure numbers, so this rarely surprises; document the edge
    (``odin config set k 5`` stores the integer 5, not "5").
    """
    s = raw.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# ---------------------------------------------------------------------------
# minimal TOML writer (hand-rolled — tomllib is read-only, tomli-w forbidden)
# ---------------------------------------------------------------------------

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _format_key(key: str) -> str:
    """A bare key when it is a simple identifier, else a quoted basic string."""
    return key if _BARE_KEY.match(key) else _quote(key)


def _quote(s: str) -> str:
    """A TOML basic string (double-quoted, minimal escaping)."""
    out = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def _format_value(value) -> str:
    # bool before int — bool is a subclass of int in Python.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _emit_table(table: dict, path: list[str], lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path and (scalars or not subtables):
        # Emit a header for any table with direct scalars, or for an empty leaf;
        # a table that only holds sub-tables (e.g. `platforms`) needs no header.
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"[{'.'.join(_format_key(p) for p in path)}]")
    for key, val in scalars.items():
        lines.append(f"{_format_key(key)} = {_format_value(val)}")
    for key, val in subtables.items():
        _emit_table(val, path + [key], lines)


def dump_toml(config: dict) -> str:
    """Serialise the nested config dict to TOML text (trailing newline).

    Handles top-level scalars followed by nested tables; values may be
    str/bool/int/float. Round-tripping through this writer drops any comments
    the file previously held (documented in the proposal §4).
    """
    lines: list[str] = []
    _emit_table(config, [], lines)
    text = "\n".join(lines).strip("\n")
    return text + "\n" if text else ""


def save_config(config: dict, path: Path | None = None) -> Path:
    """Atomically write `config` as TOML to `path` (default `config_path()`).

    Creates the parent dir (`~/.odin`) on first write. Writes a temp file in
    the target dir then `os.replace`s it into place, so a crash mid-write can
    never leave a half-written config.
    """
    target = path if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = dump_toml(config)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=".config.", suffix=".toml.tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target
