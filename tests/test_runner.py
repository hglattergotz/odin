"""Runner tests using a fake `claude` shell script.

The fake script ignores all args and emits a fixed stream-json sequence
chosen by the FAKE_CLAUDE_SCENARIO env var. This keeps the tests fast
and decoupled from the real Claude binary.
"""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from odin.runner import _abbrev_path, _handle_event, _tool_detail, run_claude


FAKE_SCRIPT = r"""#!/bin/sh
# Drain stdin so the writer doesn't see EPIPE.
cat >/dev/null
case "$FAKE_CLAUDE_SCENARIO" in
  completed)
    printf '%s\n' '{"type":"system","subtype":"init","session_id":"sess-1"}'
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"thinking..."}]}}'
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"echo hi"}}]}}'
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","session_id":"sess-1","is_error":false,"result":"All done.\n<<<NEXT_CONTEXT>>>\nDo task 2.\n<<<END>>>"}'
    exit 0
    ;;
  held)
    printf '%s\n' '{"type":"system","subtype":"init","session_id":"sess-2"}'
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","session_id":"sess-2","is_error":false,"result":"<<<NEEDS_INPUT>>>\nWhich db?\n<<<END>>>"}'
    exit 0
    ;;
  max_turns)
    printf '%s\n' '{"type":"result","subtype":"error_max_turns","stop_reason":"max_turns","is_error":true,"result":"hit limit"}'
    exit 0
    ;;
  nonzero_exit)
    printf '%s\n' '{"type":"system","subtype":"init","session_id":"sess-3"}'
    echo "boom" >&2
    exit 2
    ;;
  garbage)
    echo "this is not json"
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"plain text, no markers"}'
    exit 0
    ;;
  echo_args)
    # Record argv so the test can assert which flags Odin passed.
    printf '%s\n' "$@" > "$ODIN_ARGS_FILE"
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\nok\n<<<END>>>"}'
    exit 0
    ;;
  stderr_flood)
    # Emit >64KB to stderr BEFORE any stdout. Without concurrent stderr
    # draining the child blocks on a full stderr pipe and the run deadlocks.
    i=0
    while [ $i -lt 2000 ]; do
      echo "stderr-noise-line-$i ........................................." >&2
      i=$((i + 1))
    done
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\nok\n<<<END>>>"}'
    exit 0
    ;;
  *)
    echo "unknown scenario: $FAKE_CLAUDE_SCENARIO" >&2
    exit 99
    ;;
esac
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake-claude.sh"
    script.write_text(FAKE_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _run(scenario: str, fake: Path, project: Path):
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    try:
        return run_claude(
            "do the thing",
            project,
            claude_bin=str(fake),
            out=io.StringIO(),
        )
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)


def test_completed_scenario(fake_claude: Path, project_dir: Path):
    r = _run("completed", fake_claude, project_dir)
    assert r.succeeded is True
    assert r.exit_code == 0
    assert r.stop_reason == "end_turn"
    assert r.session_id == "sess-1"
    assert "<<<NEXT_CONTEXT>>>" in r.final_text
    assert r.error is None


def test_held_scenario(fake_claude: Path, project_dir: Path):
    r = _run("held", fake_claude, project_dir)
    # `succeeded` reflects clean termination — the orchestrator decides
    # held vs completed by parsing final_text with protocol.parse().
    assert r.succeeded is True
    assert "<<<NEEDS_INPUT>>>" in r.final_text


def test_max_turns_scenario(fake_claude: Path, project_dir: Path):
    r = _run("max_turns", fake_claude, project_dir)
    assert r.succeeded is False
    assert r.stop_reason == "max_turns"
    assert r.error is not None


def test_nonzero_exit_scenario(fake_claude: Path, project_dir: Path):
    r = _run("nonzero_exit", fake_claude, project_dir)
    assert r.succeeded is False
    assert r.exit_code == 2
    assert r.error is not None
    assert "boom" in r.error


def test_garbage_then_success(fake_claude: Path, project_dir: Path):
    # Non-JSON lines must not break the run — they're just shown to the user.
    r = _run("garbage", fake_claude, project_dir)
    assert r.exit_code == 0
    assert r.final_text == "plain text, no markers"


def test_terminal_text_is_streamed_to_out(fake_claude: Path, project_dir: Path):
    os.environ["FAKE_CLAUDE_SCENARIO"] = "completed"
    sink = io.StringIO()
    try:
        run_claude(
            "do the thing",
            project_dir,
            claude_bin=str(fake_claude),
            out=sink,
        )
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
    output = sink.getvalue()
    assert "thinking..." in output
    assert "→ Bash" in output
    assert "echo hi" in output          # the command arg is summarised on the line
    assert "[session sess-1]" in output
    assert "⏺" in output                # assistant text block bullet


def test_system_prompt_passed_as_append_flag(fake_claude: Path, project_dir: Path, tmp_path: Path):
    args_file = tmp_path / "argv.txt"
    os.environ["FAKE_CLAUDE_SCENARIO"] = "echo_args"
    os.environ["ODIN_ARGS_FILE"] = str(args_file)
    try:
        run_claude(
            "do the thing",
            project_dir,
            claude_bin=str(fake_claude),
            system_prompt="CONTRACT-TEXT",
            out=io.StringIO(),
        )
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
        os.environ.pop("ODIN_ARGS_FILE", None)
    argv = args_file.read_text().splitlines()
    assert "--append-system-prompt" in argv
    assert "CONTRACT-TEXT" in argv


def test_disallowed_tools_and_default_permission_mode_passed(fake_claude: Path, project_dir: Path, tmp_path: Path):
    args_file = tmp_path / "argv.txt"
    os.environ["FAKE_CLAUDE_SCENARIO"] = "echo_args"
    os.environ["ODIN_ARGS_FILE"] = str(args_file)
    try:
        run_claude(
            "x", project_dir, claude_bin=str(fake_claude),
            disallowed_tools=["Bash(rm:*)", "WebFetch"], out=io.StringIO(),
        )
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
        os.environ.pop("ODIN_ARGS_FILE", None)
    argv = args_file.read_text().splitlines()
    assert "--disallowed-tools" in argv
    assert "Bash(rm:*),WebFetch" in argv
    # Full-autonomy default flows through unless overridden.
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "bypassPermissions"


def test_no_system_prompt_flag_when_absent(fake_claude: Path, project_dir: Path, tmp_path: Path):
    args_file = tmp_path / "argv.txt"
    os.environ["FAKE_CLAUDE_SCENARIO"] = "echo_args"
    os.environ["ODIN_ARGS_FILE"] = str(args_file)
    try:
        run_claude("x", project_dir, claude_bin=str(fake_claude), out=io.StringIO())
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
        os.environ.pop("ODIN_ARGS_FILE", None)
    assert "--append-system-prompt" not in args_file.read_text().splitlines()


def test_tool_detail_picks_the_right_arg_per_tool():
    assert _tool_detail("Read", {"file_path": "src/odin/cli.py"}) == "src/odin/cli.py"
    assert _tool_detail("Bash", {"command": "uv run pytest"}) == "uv run pytest"
    assert _tool_detail("Glob", {"pattern": "**/*.py"}) == "**/*.py"
    assert _tool_detail("WebSearch", {"query": "ansi colors"}) == "ansi colors"


def test_tool_detail_collapses_whitespace_and_handles_missing():
    assert _tool_detail("Bash", {"command": "echo a\n  echo b"}) == "echo a echo b"
    assert _tool_detail("Bash", {}) == ""          # no input key
    assert _tool_detail("Bash", None) == ""         # no input at all
    # Unknown tool falls back to first string arg.
    assert _tool_detail("Mystery", {"thing": "value"}) == "value"


def test_tool_detail_truncates_keeping_path_tail():
    long_path = "/very/long/" + "a/" * 60 + "target_file.py"
    out = _tool_detail("Read", {"file_path": long_path})
    assert len(out) <= 72
    assert out.startswith("…")
    assert out.endswith("target_file.py")   # filename stays visible
    long_cmd = "echo " + "x" * 200
    out2 = _tool_detail("Bash", {"command": long_cmd})
    assert len(out2) <= 72
    assert out2.startswith("echo ")
    assert out2.endswith("…")


def test_max_turns_flag_omitted_by_default_and_added_when_set(fake_claude: Path, project_dir: Path, tmp_path: Path):
    args_file = tmp_path / "argv.txt"
    os.environ["FAKE_CLAUDE_SCENARIO"] = "echo_args"
    os.environ["ODIN_ARGS_FILE"] = str(args_file)
    try:
        run_claude("x", project_dir, claude_bin=str(fake_claude), out=io.StringIO())
        assert "--max-turns" not in args_file.read_text().splitlines()
        run_claude("x", project_dir, claude_bin=str(fake_claude), max_turns=200, out=io.StringIO())
        argv = args_file.read_text().splitlines()
        assert "--max-turns" in argv and "200" in argv
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
        os.environ.pop("ODIN_ARGS_FILE", None)


def test_large_stderr_does_not_deadlock(fake_claude: Path, project_dir: Path):
    # Regression guard: a chatty stderr (>64KB) before stdout must not stall.
    r = _run("stderr_flood", fake_claude, project_dir)
    assert r.exit_code == 0
    assert "<<<NEXT_CONTEXT>>>" in r.final_text


class _Sink(io.StringIO):
    """A StringIO that can pretend to be (or not be) a TTY."""

    def __init__(self, tty: bool = False):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D401
        return self._tty


def test_handle_event_session_init_is_dim_indented_truncated():
    out = _Sink(tty=False)
    _handle_event(
        {"type": "system", "subtype": "init", "session_id": "957ba4b3-deadbeef"},
        out,
    )
    line = out.getvalue()
    assert line.startswith("  [session 957ba4b3…]")
    assert "\033" not in line   # no ANSI off-TTY


def test_handle_event_assistant_text_gets_blank_line_and_bullet():
    out = _Sink(tty=False)
    _handle_event(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "Hello there"}]}},
        out,
    )
    val = out.getvalue()
    # blank line, then ⏺ bullet + verbatim prose
    assert val.startswith("\n⏺ Hello there")
    assert "\033" not in val


def test_handle_event_tool_lines_indented_and_arrowed():
    out = _Sink(tty=False)
    _handle_event(
        {"type": "assistant",
         "message": {"content": [
             {"type": "tool_use", "name": "Read", "input": {"file_path": "src/x.py"}},
         ]}},
        out,
    )
    val = out.getvalue()
    assert "   → Read  src/x.py\n" in val
    assert "\033" not in val


def test_handle_event_abbreviates_path_relative_to_project(tmp_path: Path):
    out = _Sink(tty=False)
    proj = tmp_path / "proj"
    (proj / "src" / "odin").mkdir(parents=True)
    abs_path = str(proj / "src" / "odin" / "cli.py")
    _handle_event(
        {"type": "assistant",
         "message": {"content": [
             {"type": "tool_use", "name": "Read", "input": {"file_path": abs_path}},
         ]}},
        out,
        proj,
    )
    val = out.getvalue()
    assert "src/odin/cli.py" in val
    assert abs_path not in val


def test_abbrev_path_relative_and_home():
    proj = Path("/home/u/proj")
    assert _abbrev_path("/home/u/proj/src/a.py", proj) == "src/a.py"
    # relative paths pass through unchanged
    assert _abbrev_path("src/a.py", proj) == "src/a.py"
    # outside the project, but under home → ~ form
    assert _abbrev_path("/home/u/proj/src/a.py", None) == "/home/u/proj/src/a.py"


def test_handle_event_uses_ansi_on_tty():
    out = _Sink(tty=True)
    _handle_event(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "hi"}]}},
        out,
    )
    assert "\033[" in out.getvalue()   # color emitted on a TTY sink


def test_missing_project_dir_raises(fake_claude: Path, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        run_claude(
            "x",
            tmp_path / "nope",
            claude_bin=str(fake_claude),
            out=io.StringIO(),
        )
