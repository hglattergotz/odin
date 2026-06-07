"""Best-effort terminal signaling via OSC escape sequences.

Odin streams to the user's TTY; this module piggybacks OSC escapes on top of
that stream so the housekeeping tab paints its own live status (title, color,
attention, notification, progress bar).

Every function here is *best-effort*, modeled on ``runner._safe_write``:

- It no-ops immediately when ``out`` is not a TTY (no escape junk in pipes,
  logs, or CI capture).
- It swallows every error — signaling must never sink a run.
- It is pure: no persistent state, just byte writes.

When ``$TMUX`` is set, the iTerm2 OSC 1337 / OSC 9 sequences are wrapped in
tmux DCS passthrough so they survive the multiplexer. OSC 0 titles pass
through tmux untouched and need no wrapping.

Stdlib only.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, TextIO

ESC = "\033"
BEL = "\a"  # \007

# Longest title we will ever emit; a weird queue name can't overflow the tab.
_TITLE_MAX = 80


def _is_tty(out: TextIO) -> bool:
    try:
        return bool(out.isatty())
    except Exception:
        return False


def _emit(out: TextIO, text: str) -> None:
    """Write + flush, swallowing all errors (see ``runner._safe_write``)."""
    try:
        out.write(text)
        out.flush()
    except Exception:
        pass


def _tmux_wrap(seq: str) -> str:
    """Wrap an escape sequence in tmux DCS passthrough when inside tmux.

    tmux passes a wrapped sequence through to the outer terminal as:
    ``ESC P tmux; <seq with every ESC doubled> ESC \\``. Outside tmux the
    sequence is returned unchanged.
    """
    if not os.environ.get("TMUX"):
        return seq
    return f"{ESC}Ptmux;{seq.replace(ESC, ESC + ESC)}{ESC}\\"


def _sanitize_title(text: str) -> str:
    """Strip control chars (incl. ESC/BEL) and clamp length."""
    cleaned = "".join(ch for ch in str(text) if ch.isprintable())
    return cleaned[:_TITLE_MAX]


def set_title(text: str, out: TextIO = sys.stdout) -> None:
    """Set the terminal/tab title (OSC 0). Universal — Terminal.app + iTerm2.

    OSC 0 passes through tmux fine, so it is not DCS-wrapped.
    """
    if not _is_tty(out):
        return
    _emit(out, f"{ESC}]0;{_sanitize_title(text)}{BEL}")


def set_tab_color(hex_or_none: Optional[str], out: TextIO = sys.stdout) -> None:
    """Set the iTerm2 tab color (OSC 1337). ``None`` resets to default."""
    if not _is_tty(out):
        return
    if hex_or_none is None:
        value = "default"
    else:
        value = str(hex_or_none).lstrip("#").strip()
    _emit(out, _tmux_wrap(f"{ESC}]1337;SetColors=tab={value}{BEL}"))


def request_attention(out: TextIO = sys.stdout) -> None:
    """Bounce the iTerm2 dock icon once (OSC 1337 RequestAttention)."""
    if not _is_tty(out):
        return
    _emit(out, _tmux_wrap(f"{ESC}]1337;RequestAttention=once{BEL}"))


def notify(message: str, out: TextIO = sys.stdout) -> None:
    """Post a terminal notification (OSC 9). iTerm2 and others."""
    if not _is_tty(out):
        return
    _emit(out, _tmux_wrap(f"{ESC}]9;{message}{BEL}"))


def set_progress(state: int, pct: int = 0, out: TextIO = sys.stdout) -> None:
    """Set the in-tab progress indicator (OSC 9;4).

    ``state``: 0=hide, 1=normal value, 2=error, 3=indeterminate (spinner;
    ignores ``pct``), 4=warning. Supported by iTerm2 >= 3.6.6, Ghostty,
    Kitty, WezTerm, Windows Terminal; harmlessly ignored elsewhere.
    """
    if not _is_tty(out):
        return
    _emit(out, _tmux_wrap(f"{ESC}]9;4;{int(state)};{int(pct)}{BEL}"))
