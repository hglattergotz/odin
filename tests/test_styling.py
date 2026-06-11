"""Tests for the styled task banners in `cli.py` (Component 3).

We drive `_run_loop` with scripted `RunResult`s and capture the banner text
(which prints to stdout via capsys). The OSC tab signaling goes to a separate
sink and is covered by `test_signals.py`. Here we assert the visible banner
*text* — glyphs, the run summary, and the color gate (no ANSI off-TTY or under
`--no-color`, ANSI present when stdout is a forced TTY).
"""

from __future__ import annotations

import io
import sys

import pytest

from odin import metrics, style
from odin.cli import Signals, _build_parser, _run_loop
from odin.queue import Queue
from odin.runner import RunResult


class _TTYStdout(io.StringIO):
    """A StringIO that claims to be a TTY (so style.enabled() emits ANSI)."""
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean_color(monkeypatch):
    """Each test starts (and ends) with a clean color gate."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("ODIN_NO_COLOR", raising=False)
    style.set_no_color(False)
    yield
    style.set_no_color(False)


def _completed(**kw) -> RunResult:
    return RunResult(
        True, "<<<NEXT_CONTEXT>>>\ncarry\n<<<END>>>", "end_turn", None, 0, None, **kw
    )


def _failed() -> RunResult:
    return RunResult(False, "boom", "max_turns", "error_max_turns", 1, None)


def _sig() -> Signals:
    return Signals(title_on=False, notify_on=False, prefix="odin", base_color=None)


def _drive(tmp_path, monkeypatch, *, tasks, results, branch=None):
    """Seed `tasks`, script run_claude with `results`, run the loop; banners go
    to stdout (capsys), OSC to a throwaway sink."""
    monkeypatch.delenv("TMUX", raising=False)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    qdir = tmp_path / "queue" / "myq"
    q = Queue(qdir)
    for name in tasks:
        (q.root / "pending" / name).write_text("body", encoding="utf-8")

    seq = list(results)
    monkeypatch.setattr("odin.cli.run_agent", lambda *a, **k: seq.pop(0))
    args = _build_parser().parse_args(["run", str(qdir)])
    acc = metrics.RunAccumulator(
        run_id="t", project=project, queue=q.root, branch=branch, enabled=False
    )
    return _run_loop(
        args, project, q, None, None, "sp", acc, _sig(),
        branch=branch, out=io.StringIO(),
    )


# --- banner text ----------------------------------------------------------

def test_start_banner_has_rule_header_and_metadata(tmp_path, monkeypatch, capsys):
    rc = _drive(tmp_path, monkeypatch, tasks=["001-a.md"], results=[_completed()],
                branch="feature-x")
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{style.GLYPH_RULE * 2} {style.GLYPH_TASK} task 1/1 · 001-a" in out
    assert "branch feature-x" in out
    assert "project" in out


def test_done_footer_shows_run_summary(tmp_path, monkeypatch, capsys):
    rc = _drive(
        tmp_path, monkeypatch, tasks=["001-a.md"],
        results=[_completed(num_turns=12, wall_ms=102_000, cost_usd=0.21)],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{style.GLYPH_OK} 001-a · done" in out
    assert "12 turns" in out
    assert "1m42s" in out
    assert "$0.21" in out


def test_done_footer_omits_absent_fields(tmp_path, monkeypatch, capsys):
    rc = _drive(tmp_path, monkeypatch, tasks=["001-a.md"], results=[_completed()])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{style.GLYPH_OK} 001-a · done" in out
    assert "turns" not in out  # no num_turns / wall / cost → bare footer


def test_failed_banner_has_red_glyph_header(tmp_path, monkeypatch, capsys):
    rc = _drive(tmp_path, monkeypatch, tasks=["001-a.md"], results=[_failed()])
    assert rc == 1
    out = capsys.readouterr().out
    assert f"{style.GLYPH_FAIL} 001-a · FAILED · exit 1 · stop_reason=max_turns" in out


# --- color gate ------------------------------------------------------------

def test_no_ansi_off_tty(tmp_path, monkeypatch, capsys):
    """capsys stdout is not a TTY → banners are plain (glyphs kept, no ANSI)."""
    _drive(tmp_path, monkeypatch, tasks=["001-a.md"],
           results=[_completed(num_turns=3)])
    out = capsys.readouterr().out
    assert "\033[" not in out
    assert style.GLYPH_OK in out  # glyphs survive without color


def test_ansi_present_on_forced_tty(tmp_path, monkeypatch):
    """With a TTY stdout and color enabled, banners carry SGR codes."""
    tty = _TTYStdout()
    monkeypatch.setattr(sys, "stdout", tty)
    _drive(tmp_path, monkeypatch, tasks=["001-a.md"], results=[_completed()])
    assert "\033[" in tty.getvalue()
    assert style.GLYPH_OK in tty.getvalue()


def test_no_color_flag_disables_ansi_even_on_tty(tmp_path, monkeypatch):
    """`--no-color` (module override) keeps glyphs but emits no ANSI on a TTY."""
    style.set_no_color(True)
    tty = _TTYStdout()
    monkeypatch.setattr(sys, "stdout", tty)
    _drive(tmp_path, monkeypatch, tasks=["001-a.md"], results=[_completed()])
    out = tty.getvalue()
    assert "\033[" not in out
    assert style.GLYPH_OK in out


def test_no_color_arg_wires_into_style(tmp_path, monkeypatch):
    """The run subparser exposes --no-color; _cmd_run folds it into the gate."""
    args = _build_parser().parse_args(["run", "queue", "--no-color"])
    assert args.no_color is True
