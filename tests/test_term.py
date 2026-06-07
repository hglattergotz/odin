"""Tests for the best-effort terminal signaling helpers."""

from __future__ import annotations

import io

import pytest

from odin import term


class _Sink(io.StringIO):
    """A StringIO that can pretend to be (or not be) a TTY."""

    def __init__(self, tty: bool = True):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D401 - simple override
        return self._tty


# --- exact byte sequences (TTY sink, no tmux) ------------------------------


def test_set_title_exact(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_title("hello", out=out)
    assert out.getvalue() == "\033]0;hello\007"


def test_set_tab_color_hex(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_tab_color("e0a020", out=out)
    assert out.getvalue() == "\033]1337;SetColors=tab=e0a020\007"


def test_set_tab_color_strips_leading_hash(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_tab_color("#cc3333", out=out)
    assert out.getvalue() == "\033]1337;SetColors=tab=cc3333\007"


def test_set_tab_color_none_is_default(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_tab_color(None, out=out)
    assert out.getvalue() == "\033]1337;SetColors=tab=default\007"


def test_request_attention_exact(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.request_attention(out=out)
    assert out.getvalue() == "\033]1337;RequestAttention=once\007"


def test_notify_exact(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.notify("odin: q needs input", out=out)
    assert out.getvalue() == "\033]9;odin: q needs input\007"


def test_set_progress_normal(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_progress(1, 42, out=out)
    assert out.getvalue() == "\033]9;4;1;42\007"


def test_set_progress_indeterminate(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_progress(3, out=out)
    assert out.getvalue() == "\033]9;4;3;0\007"


def test_set_progress_error_and_hide(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_progress(2, 90, out=out)
    term.set_progress(0, out=out)
    assert out.getvalue() == "\033]9;4;2;90\007\033]9;4;0;0\007"


# --- non-TTY: nothing is written -------------------------------------------


def test_nothing_written_when_not_tty(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink(tty=False)
    term.set_title("hello", out=out)
    term.set_tab_color("e0a020", out=out)
    term.request_attention(out=out)
    term.notify("hi", out=out)
    term.set_progress(1, 50, out=out)
    assert out.getvalue() == ""


# --- tmux DCS passthrough toggles on $TMUX ---------------------------------


def test_tmux_wraps_iterm_sequences(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    out = _Sink()
    term.set_tab_color("e0a020", out=out)
    # Inner ESC is doubled; whole thing framed by ESC P tmux; ... ESC \
    inner = "\033\033]1337;SetColors=tab=e0a020\007"
    assert out.getvalue() == f"\033Ptmux;{inner}\033\\"


def test_tmux_does_not_wrap_title(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    out = _Sink()
    term.set_title("hello", out=out)
    # OSC 0 titles pass through tmux untouched.
    assert out.getvalue() == "\033]0;hello\007"


def test_no_tmux_no_wrap(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.notify("hi", out=out)
    assert out.getvalue() == "\033]9;hi\007"


# --- title sanitation -------------------------------------------------------


def test_title_strips_control_chars(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_title("a\033]666;evil\007b\nc\t", out=out)
    # ESC, BEL, newline, tab all stripped; only printable kept.
    assert out.getvalue() == "\033]0;a]666;evilbc\007"


def test_title_clamped_to_80(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    out = _Sink()
    term.set_title("x" * 200, out=out)
    assert out.getvalue() == "\033]0;" + "x" * 80 + "\007"


# --- best-effort: a write failure never propagates -------------------------


def test_write_failure_is_swallowed(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)

    class _Boom(io.StringIO):
        def isatty(self):
            return True

        def write(self, _):
            raise OSError("boom")

    # Must not raise.
    term.set_title("hello", out=_Boom())
    term.set_progress(1, 10, out=_Boom())
