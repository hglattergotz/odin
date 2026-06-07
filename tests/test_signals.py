"""Tests for terminal signaling wired into `_run_loop` (Components 2 & 3).

We drive `_run_loop` directly with a scripted fake `run_claude` and a
TTY-reporting `StringIO` sink, then assert the exact title strings, OSC 9;4
progress sequences, and (only with --notify) the iTerm2 color/attention/notify
escapes. Plain `print()` output goes to the real stdout (capsys), so the sink
contains *only* the out-of-band escape bytes.
"""

from __future__ import annotations

import io

import pytest

from odin import metrics
from odin.cli import (
    Signals, _build_parser, _reset_terminal, _resolve_signals, _run_loop,
)
from odin.queue import Queue
from odin.runner import RunResult


# ----------------------------------------------------------------------
# sinks + scripted results
# ----------------------------------------------------------------------

class _TTYSink(io.StringIO):
    """A StringIO that claims to be a TTY so term.py actually writes."""
    def isatty(self) -> bool:
        return True


class _BoomSink(io.StringIO):
    """A TTY sink whose write() always raises — to prove best-effort."""
    def isatty(self) -> bool:
        return True

    def write(self, s):  # type: ignore[override]
        raise OSError("forced write failure")


def _completed() -> RunResult:
    return RunResult(True, "<<<NEXT_CONTEXT>>>\ncarry\n<<<END>>>", "end_turn", None, 0, None)


def _held() -> RunResult:
    j = ('{"questions":[{"problem":"p","question":"Which db?",'
         '"options":[{"key":"a","label":"PG"}],"recommended":"a","why":"y"}]}')
    return RunResult(True, f"<<<NEEDS_INPUT>>>\n{j}\n<<<END>>>", "end_turn", None, 0, None)


def _failed() -> RunResult:
    return RunResult(False, "boom", "max_turns", "error_max_turns", 1, None)


# ----------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------

def _drive(tmp_path, monkeypatch, *, tasks, results, signals, sink):
    """Seed `tasks` (list of filenames) into a fresh queue, script `run_claude`
    to return `results` in order, run the loop with `signals`+`sink`, return
    (exit_code, sink_contents)."""
    monkeypatch.delenv("TMUX", raising=False)  # no DCS wrapping in assertions
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    qdir = tmp_path / "queue" / "myq"
    q = Queue(qdir)
    for name in tasks:
        (q.root / "pending" / name).write_text("body", encoding="utf-8")

    seq = list(results)
    monkeypatch.setattr("odin.cli.run_claude", lambda *a, **k: seq.pop(0))

    args = _build_parser().parse_args(["run", str(qdir)])
    acc = metrics.RunAccumulator(
        run_id="t", project=project, queue=q.root, branch=None, enabled=False
    )
    rc = _run_loop(args, project, q, None, None, "sys-prompt", acc, signals, out=sink)
    return rc, sink.getvalue()


def _sig(*, title_on=True, notify_on=False, prefix="odin", base_color=None) -> Signals:
    return Signals(title_on=title_on, notify_on=notify_on, prefix=prefix, base_color=base_color)


# ----------------------------------------------------------------------
# titles + progress (on by default)
# ----------------------------------------------------------------------

def test_completed_emits_titles_and_progress(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_completed()],
        signals=_sig(), sink=_TTYSink(),
    )
    assert rc == 0
    assert "odin ⏵ 1/1 myq" in out      # task start ⏵
    assert "odin ✓ 1/1 myq" in out      # task done ✓
    assert "odin ✓ done myq" in out     # queue drained
    assert "\033]9;4;1;0\a" in out           # progress: 0% at start
    assert "\033]9;4;1;100\a" in out         # progress: 100% at done


def test_progress_fills_across_multiple_tasks(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md", "002-b.md"], results=[_completed(), _completed()],
        signals=_sig(), sink=_TTYSink(),
    )
    assert rc == 0
    assert "odin ⏵ 1/2 myq" in out
    assert "odin ⏵ 2/2 myq" in out
    assert "\033]9;4;1;50\a" in out   # 1 of 2 done = 50%


def test_held_emits_amber_title(tmp_path, monkeypatch):
    # stdin is not a TTY under pytest → non-interactive held path, exit 10.
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_held()],
        signals=_sig(), sink=_TTYSink(),
    )
    assert rc == 10
    assert "odin ⏸ needs input myq" in out
    # No iTerm2 color/attention without --notify.
    assert "1337" not in out


def test_failed_emits_error_progress(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_failed()],
        signals=_sig(), sink=_TTYSink(),
    )
    assert rc == 1
    assert "odin ✗ failed myq" in out
    assert "\033]9;4;2;0\a" in out   # error state on the progress bar


def test_max_tasks_limit_drains_with_title(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    q = Queue(tmp_path / "queue" / "myq")
    for n in ("001-a.md", "002-b.md"):
        (q.root / "pending" / n).write_text("body")
    monkeypatch.setattr("odin.cli.run_claude", lambda *a, **k: _completed())
    args = _build_parser().parse_args(["run", str(q.root), "--max-tasks", "1"])
    acc = metrics.RunAccumulator(run_id="t", project=project, queue=q.root,
                                 branch=None, enabled=False)
    sink = _TTYSink()
    rc = _run_loop(args, project, q, None, None, "sp", acc, _sig(), out=sink)
    out = sink.getvalue()
    assert rc == 0
    assert "odin ✓ done myq" in out   # drained title on the --max-tasks stop


# ----------------------------------------------------------------------
# --notify gates the iTerm2-specific escapes
# ----------------------------------------------------------------------

def test_notify_emits_color_attention_and_notification_on_held(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_held()],
        signals=_sig(notify_on=True), sink=_TTYSink(),
    )
    assert rc == 10
    assert "\033]1337;SetColors=tab=e0a020\a" in out      # amber flag
    assert "\033]1337;RequestAttention=once\a" in out     # dock bounce
    assert "\033]9;odin: myq needs input\a" in out        # OSC 9 notification


def test_notify_asserts_base_color_at_start_and_reverts_on_drain(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_completed()],
        signals=_sig(notify_on=True, base_color="123456"), sink=_TTYSink(),
    )
    assert rc == 0
    # Base hue asserted at task start AND reverted on drain (never iTerm2 default).
    assert out.count("\033]1337;SetColors=tab=123456\a") >= 2
    assert "tab=default" not in out


def test_failed_leaves_red_flag_color_with_notify(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_failed()],
        signals=_sig(notify_on=True, base_color="123456"), sink=_TTYSink(),
    )
    assert rc == 1
    assert "\033]1337;SetColors=tab=cc3333\a" in out   # red flag left in place


def test_no_color_escapes_without_notify(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_completed()],
        signals=_sig(notify_on=False, base_color="123456"), sink=_TTYSink(),
    )
    assert rc == 0
    assert "1337" not in out   # no tab-color/attention without --notify
    assert "odin ✓ 1/1 myq" in out  # titles still emitted


# ----------------------------------------------------------------------
# suppression: --no-title and non-TTY
# ----------------------------------------------------------------------

def test_no_title_suppresses_all_escapes(tmp_path, monkeypatch):
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_completed()],
        signals=_sig(title_on=False, notify_on=False), sink=_TTYSink(),
    )
    assert rc == 0
    assert out == ""   # titles off, notify off → nothing emitted


def test_non_tty_sink_emits_nothing(tmp_path, monkeypatch):
    # Titles on, but a plain (non-TTY) StringIO → term.py no-ops.
    rc, out = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_completed()],
        signals=_sig(notify_on=True, base_color="123456"), sink=io.StringIO(),
    )
    assert rc == 0
    assert out == ""


# ----------------------------------------------------------------------
# best-effort: a write failure never changes the exit code
# ----------------------------------------------------------------------

def test_signaling_write_failure_does_not_break_run(tmp_path, monkeypatch):
    rc, _ = _drive(
        tmp_path, monkeypatch,
        tasks=["001-a.md"], results=[_completed()],
        signals=_sig(notify_on=True), sink=_BoomSink(),
    )
    assert rc == 0   # every term.py write raised, run still drained cleanly


# ----------------------------------------------------------------------
# Signals resolution + terminal reset helpers
# ----------------------------------------------------------------------

def test_resolve_signals_defaults_titles_on_notify_off(monkeypatch):
    monkeypatch.delenv("ODIN_NO_TITLE", raising=False)
    monkeypatch.delenv("ODIN_NOTIFY", raising=False)
    monkeypatch.delenv("PROJECT_TAB_COLOR", raising=False)
    args = _build_parser().parse_args(["run"])
    s = _resolve_signals(args)
    assert s.title_on is True and s.notify_on is False
    assert s.prefix == "odin" and s.base_color is None


def test_resolve_signals_env_overrides(monkeypatch):
    monkeypatch.setenv("ODIN_NO_TITLE", "1")
    monkeypatch.setenv("ODIN_NOTIFY", "1")
    monkeypatch.setenv("PROJECT_TAB_COLOR", "abcdef")
    args = _build_parser().parse_args(["run"])
    s = _resolve_signals(args)
    assert s.title_on is False and s.notify_on is True
    assert s.base_color == "abcdef"


def test_resolve_signals_flags_beat_env_for_base_color(monkeypatch):
    monkeypatch.setenv("PROJECT_TAB_COLOR", "abcdef")
    args = _build_parser().parse_args(["run", "--tab-color", "001122", "--tab-title", "x"])
    s = _resolve_signals(args)
    assert s.base_color == "001122"   # --tab-color wins over $PROJECT_TAB_COLOR
    assert s.prefix == "x"


def test_reset_terminal_clears_progress_and_neutral_title(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    q = Queue(tmp_path / "queue" / "myq")
    sink = _TTYSink()
    _reset_terminal(_sig(), q, sink)
    out = sink.getvalue()
    assert "\033]9;4;0;0\a" in out          # progress cleared
    assert "\033]0;odin myq\a" in out        # neutral title
    assert "1337" not in out                 # tab color left untouched


def test_reset_terminal_noop_when_titles_off(tmp_path):
    q = Queue(tmp_path / "queue" / "myq")
    sink = _TTYSink()
    _reset_terminal(_sig(title_on=False), q, sink)
    assert sink.getvalue() == ""


def test_signaler_indeterminate_progress_when_total_zero(monkeypatch):
    # Defensive branch: an unknown/zero total spins (state 3) instead of dividing.
    from odin.cli import _Signaler
    monkeypatch.delenv("TMUX", raising=False)
    sink = _TTYSink()
    _Signaler(_sig(), sink, "myq", total=0).task_start(0)
    out = sink.getvalue()
    assert "\033]9;4;3;0\a" in out   # indeterminate spinner
    assert "odin ⏵ 1/0 myq" in out
