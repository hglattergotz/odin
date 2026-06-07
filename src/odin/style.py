"""Best-effort ANSI styling for Odin's streamed output.

A tiny, stdlib-only color layer. Like ``runner._safe_write`` and ``term``, it
is purely cosmetic and must never break a run:

- Color is emitted only when **all** hold: ``out.isatty()``, ``NO_COLOR``
  unset, ``ODIN_NO_COLOR`` unset, and the module-level ``--no-color`` override
  off. When color is off the helpers return the **plain text** — the caller
  keeps the glyphs, indentation, and blank lines so the layout still reads.
- Glyphs are Unicode and are part of the text the caller passes in; they
  survive whether or not color is on.

Stdlib only. No ``rich``/``colorama``.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

# --- SGR codes -------------------------------------------------------------
RESET = "0"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
CYAN = "36"

# --- glyphs ----------------------------------------------------------------
GLYPH_TASK = "⏵"      # task header
GLYPH_OK = "✓"        # done
GLYPH_FAIL = "✗"      # failed
GLYPH_HELD = "⏸"      # needs input / held
GLYPH_URGENT = "‼"    # urgent follow-up
GLYPH_BULLET = "⏺"    # assistant text block
GLYPH_ARROW = "→"     # tool call
GLYPH_RULE = "━"      # header/footer rule

_ESC = "\033"

# Module-level override set from the CLI ``--no-color`` flag. None = unset.
_NO_COLOR_OVERRIDE = False


def set_no_color(flag: bool) -> None:
    """Force color off (the ``--no-color`` flag / ``ODIN_NO_COLOR`` wiring)."""
    global _NO_COLOR_OVERRIDE
    _NO_COLOR_OVERRIDE = bool(flag)


def enabled(out: TextIO | None = None) -> bool:
    """True iff it is safe to emit ANSI to ``out``.

    Gated on: the ``--no-color`` override off, ``NO_COLOR`` and ``ODIN_NO_COLOR``
    unset, and ``out`` being a TTY.
    """
    if out is None:
        out = sys.stdout
    if _NO_COLOR_OVERRIDE:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("ODIN_NO_COLOR") is not None:
        return False
    try:
        return bool(out.isatty())
    except Exception:
        return False


def paint(text: str, *codes: str, out: TextIO | None = None) -> str:
    """Wrap ``text`` in the given SGR ``codes`` when color is enabled for ``out``.

    When color is off, returns ``text`` unchanged (glyphs preserved).
    """
    if not codes or not enabled(out):
        return text
    return f"{_ESC}[{';'.join(codes)}m{text}{_ESC}[{RESET}m"


# --- semantic helpers ------------------------------------------------------
# cyan = headers/bullets/tool names; green = ok; red = fail; yellow = warn/held;
# dim = paths, session, metadata.

def header(text: str, out: TextIO | None = None) -> str:
    return paint(text, BOLD, CYAN, out=out)


def ok(text: str, out: TextIO | None = None) -> str:
    return paint(text, BOLD, GREEN, out=out)


def warn(text: str, out: TextIO | None = None) -> str:
    return paint(text, YELLOW, out=out)


def err(text: str, out: TextIO | None = None) -> str:
    return paint(text, BOLD, RED, out=out)


def tool(text: str, out: TextIO | None = None) -> str:
    return paint(text, CYAN, out=out)


def bullet(text: str, out: TextIO | None = None) -> str:
    return paint(text, CYAN, out=out)


def dim(text: str, out: TextIO | None = None) -> str:
    return paint(text, DIM, out=out)
