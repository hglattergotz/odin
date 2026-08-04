"""`odin platforms` — what Odin can drive, and what it would use right now.

The discoverability surface for the two questions a returning user actually
asks: *which platforms are supported?* and *what will I get if I don't pass a
flag?* Both answers already existed in the code — `registry.available_platforms`,
`backend.model_help`, `config.resolve_*` — but only ever surfaced **after** a
mistake, in an error message. This renders them up front.

Everything here is derived, never re-declared: platform names come from the
registry, binaries/instruction files/config keys/model forms from the backend,
and the resolved values from the same `config.resolve_*` functions `odin run`
uses. Adding a backend therefore updates this output for free — the reason
`--platform`'s help string is now generated too (it used to be hand-typed prose
that could drift from the registry).

Same posture as `guide.py` / `metrics.py`: content and layout live here, the
CLI only prints what it returns. Stdlib only; a PATH probe is `shutil.which`.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from pathlib import Path

from odin import config, style
from odin.backends.base import AgentBackend
from odin.backends.registry import available_platforms, get_backend

#: Width of the label column in a platform block.
_LABEL = 13
#: Wider label column for the footer's flag table (`--permission-mode`).
_FLAG_LABEL = 18
#: Total line width before values wrap.
_WIDTH = 78
#: ESC byte — presence of it in a value means "already painted, don't measure".
_ESC = "\033"

#: Values for `--sandbox`, shared with the argparse `choices` on `odin run` so
#: the flag and this table cannot disagree.
SANDBOX_MODES = ("enabled", "disabled")


def _flag_values() -> list[tuple[str, str]]:
    """Enumerable flag values, for the footer.

    Deliberately short: only flags whose vocabulary **Odin** owns get a value
    list. Anything a provider owns (model ids, permission modes) is described
    as a form and passed through — a stale allowlist would reject values that
    actually work, which is the same reasoning behind `validate_model` being
    shape-based rather than a catalogue.
    """
    return [
        (
            "--platform",
            "{" + ", ".join(available_platforms()) + "} — validated by Odin, "
            "and the only flag here with a closed set of values",
        ),
        ("--sandbox", "{" + ", ".join(SANDBOX_MODES) + "} — cursor only"),
        (
            "--permission-mode",
            "passed through to the agent CLI, not validated by Odin "
            "(claude: default, acceptEdits, bypassPermissions, plan)",
        ),
        (
            "--model",
            "free text — Odin only shape-checks it (see 'model forms'); the "
            "provider owns the catalogue",
        ),
    ]


def _which(binary: str) -> str | None:
    """Absolute path of `binary` on PATH, or None. Never raises."""
    try:
        return shutil.which(binary)
    except Exception:  # pragma: no cover — defensive, same posture as style/term
        return None


def _model_and_source(platform: str, cfg: dict) -> tuple[str | None, str]:
    """The model that would be used for `platform`, and where it came from."""
    env = os.environ.get("ODIN_MODEL")
    if env and env.strip():
        return env.strip(), "$ODIN_MODEL"
    model = config.resolve_model(None, platform=platform, config=cfg)
    if model:
        return model, f"platforms.{platform}.model"
    return None, ""


def _platform_and_source(cfg: dict) -> tuple[str | None, str]:
    """The platform an `odin run` with no `--platform` would pick, and why."""
    env = os.environ.get("ODIN_PLATFORM")
    if env and env.strip():
        return env.strip().lower(), "$ODIN_PLATFORM"
    default = cfg.get("default_platform")
    if isinstance(default, str) and default.strip():
        return default.strip().lower(), "default_platform in config"
    return None, ""


def _field(label: str, value: str, out=None, width: int = _LABEL) -> str:
    """One `    label   value` line, wrapped and hanging-indented to the column.

    Padding is applied *before* painting — `style.dim` adds escape bytes that
    `ljust` would otherwise count as visible width. For the same reason a value
    that already carries escapes (a ✓/✗ marker) is left unwrapped rather than
    measured wrongly; those values are short by construction.
    """
    pad = " " * (width + 4)
    label_txt = label.ljust(width) if len(label) < width else label + " "
    if _ESC in value:
        body = value
    else:
        # Wrap with the pad as the initial indent too, then drop it, so the
        # first line is measured from the column it actually starts at.
        body = textwrap.fill(
            value, width=_WIDTH, initial_indent=pad, subsequent_indent=pad,
            # Never split a flag name or model id: `--approve-mcps` wrapping to
            # `--approve-` / `mcps` reads as two different flags.
            break_on_hyphens=False, break_long_words=False,
        )[len(pad):]
    return f"    {style.dim(label_txt, out)}{body}"


def _binary_line(backend: AgentBackend, cfg: dict, out=None) -> str:
    """Binary name + whether it is actually installed — the half of 'is this
    platform usable?' that a list of names can never answer."""
    configured = config.get_in(cfg, f"platforms.{backend.name}.binary")
    binary = (
        configured.strip()
        if isinstance(configured, str) and configured.strip()
        else backend.default_binary()
    )
    found = _which(binary)
    if found:
        return _field("binary", f"{binary}  {style.ok('✓ ' + found, out)}", out)
    return _field(
        "binary", f"{binary}  {style.warn('✗ not on PATH', out)}", out
    )


def _instructions_line(backend: AgentBackend, project: Path | None, out=None) -> str:
    """The instruction file(s) this platform reads, marked present when we can
    see the project — the 'why did my workflow rules not apply?' answer."""
    parts: list[str] = []
    for rel in backend.instruction_files():
        if project is None:
            parts.append(str(rel))
        elif (project / rel).exists():
            parts.append(style.ok(f"{rel} ✓", out))
        else:
            parts.append(style.dim(f"{rel} ✗", out))
    return _field("instructions", ", ".join(parts) or "—", out)


def _platform_block(
    name: str, cfg: dict, active: str | None, project: Path | None, out=None
) -> list[str]:
    backend = get_backend(name)
    title = f"{name} · {backend.product or name}"
    if name == active:
        title = f"{style.header(title, out)}  {style.ok('← active', out)}"
    else:
        title = style.header(title, out)
    lines = [f"  {title}", _binary_line(backend, cfg, out)]

    model, source = _model_and_source(name, cfg)
    if model:
        lines.append(_field("model", f"{model}  {style.dim('(' + source + ')', out)}", out))
    else:
        lines.append(
            _field("model", style.dim("unset — the agent CLI's own default", out), out)
        )
    help_line = backend.model_help()
    if help_line:
        lines.append(_field("model forms", help_line, out))
    suggestions = config.MODEL_SUGGESTIONS.get(name) or []
    if suggestions:
        lines.append(_field("suggestions", ", ".join(suggestions), out))
    lines.append(_instructions_line(backend, project, out))
    flags = backend.platform_flags()
    if flags:
        lines.append(_field("only-here", ", ".join(flags), out))
    lines.append(_field("config keys", ", ".join(backend.config_keys()), out))
    return lines


def render(
    *,
    cfg: dict | None = None,
    project: Path | None = None,
    out=None,
) -> str:
    """The full `odin platforms` report.

    `project` (when given) is only used to mark which instruction files exist.
    `out` is the stream color is gated on — never written to here.
    """
    if cfg is None:
        cfg = config.load_config()
    active, active_source = _platform_and_source(cfg)

    lines = [style.header("odin platforms — agent CLIs Odin can drive", out)]
    if project is not None:
        lines.append(
            "  " + style.dim(
                f"instruction files checked against {project} "
                "(✓ present · ✗ absent)", out
            )
        )
    lines.append("")
    for name in available_platforms():
        lines.extend(_platform_block(name, cfg, active, project, out))
        lines.append("")

    if active:
        known = active in available_platforms()
        note = f"{active}  ({active_source})"
        if not known:
            note += style.err("  — not a known platform", out)
        # Label padded to the same column as the `resolution:` lines below.
        lines.append(f"  {style.dim('active:'.ljust(_LABEL + 2), out)}{note}")
    else:
        lines.append(
            "  " + style.warn("no default platform set", out)
            + " — pass --platform, or set one with"
        )
        lines.append("  `odin config set default_platform <name>`.")
    lines.append(
        "  " + style.dim("resolution:".ljust(_LABEL + 2), out)
        + "--platform → $ODIN_PLATFORM → default_platform in config"
    )
    lines.append(
        "  " + style.dim("".ljust(_LABEL + 2), out)
        + "--model    → $ODIN_MODEL    → platforms.<platform>.model"
    )
    lines.append("")
    lines.append(style.header("  Values Odin validates", out))
    for flag, meaning in _flag_values():
        lines.append(_field(flag, meaning, out, width=_FLAG_LABEL))
    lines.append("")
    lines.append(
        "  " + style.dim(
            "config file: " + str(config.config_path())
            + ("" if config.config_path().exists() else "  (none yet)"), out
        )
    )
    return "\n".join(lines)
