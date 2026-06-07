"""Tests for the best-effort ANSI styling helpers."""

from __future__ import annotations

import io

import pytest

from odin import style


class _Sink(io.StringIO):
    """A StringIO that can pretend to be (or not be) a TTY."""

    def __init__(self, tty: bool = True):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D401 - simple override
        return self._tty


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with a clean color gate."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("ODIN_NO_COLOR", raising=False)
    style.set_no_color(False)
    yield
    style.set_no_color(False)


# --- enabled() gating ------------------------------------------------------

def test_enabled_true_on_tty():
    assert style.enabled(_Sink(tty=True)) is True


def test_disabled_when_not_tty():
    assert style.enabled(_Sink(tty=False)) is False


def test_disabled_by_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.enabled(_Sink(tty=True)) is False


def test_disabled_by_no_color_env_even_empty(monkeypatch):
    # NO_COLOR is honored by presence, not value.
    monkeypatch.setenv("NO_COLOR", "")
    assert style.enabled(_Sink(tty=True)) is False


def test_disabled_by_odin_no_color_env(monkeypatch):
    monkeypatch.setenv("ODIN_NO_COLOR", "1")
    assert style.enabled(_Sink(tty=True)) is False


def test_disabled_by_module_override():
    style.set_no_color(True)
    assert style.enabled(_Sink(tty=True)) is False


# --- plain output when disabled --------------------------------------------

def test_no_ansi_when_disabled():
    off = _Sink(tty=False)
    for fn in (style.header, style.ok, style.warn, style.err, style.tool,
               style.dim, style.bullet):
        out = fn("hello", out=off)
        assert out == "hello"
        assert "\033" not in out


def test_paint_returns_plain_when_disabled():
    off = _Sink(tty=False)
    assert style.paint("x", style.CYAN, out=off) == "x"


def test_glyphs_preserved_when_disabled():
    off = _Sink(tty=False)
    txt = style.bullet(style.GLYPH_BULLET, out=off)
    assert txt == style.GLYPH_BULLET
    assert "⏺" in txt


# --- expected SGR codes when forced on -------------------------------------

def test_paint_emits_sgr_when_enabled():
    on = _Sink(tty=True)
    assert style.paint("x", style.CYAN, out=on) == "\033[36mx\033[0m"


def test_semantic_helpers_codes():
    on = _Sink(tty=True)
    assert style.header("h", out=on) == "\033[1;36mh\033[0m"
    assert style.ok("o", out=on) == "\033[1;32mo\033[0m"
    assert style.warn("w", out=on) == "\033[33mw\033[0m"
    assert style.err("e", out=on) == "\033[1;31me\033[0m"
    assert style.tool("t", out=on) == "\033[36mt\033[0m"
    assert style.dim("d", out=on) == "\033[2md\033[0m"
    assert style.bullet("b", out=on) == "\033[36mb\033[0m"


def test_glyphs_preserved_when_enabled():
    on = _Sink(tty=True)
    txt = style.bullet(style.GLYPH_BULLET, out=on)
    assert "⏺" in txt
    assert "\033[" in txt
