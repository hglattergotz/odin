"""Backend selection + the grok backend, driven by a fake `grok` shell script.

The fake emits grok-shaped streaming-json (text deltas + a terminal `end`
event; errors via a `type:"error"` line) chosen by FAKE_GROK_SCENARIO, and
optionally records argv / copies the --prompt-file so tests can assert flag
mapping and file-based prompt delivery. The Claude backend is covered by
test_runner.py.
"""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from odin.runner import ClaudeBackend, GrokBackend, get_backend, run_agent


FAKE_GROK = r"""#!/bin/sh
# Record argv for flag-mapping assertions.
if [ -n "$ODIN_ARGS_FILE" ]; then printf '%s\n' "$@" > "$ODIN_ARGS_FILE"; fi
# grok reads the prompt from --prompt-file (never stdin); copy it if asked.
if [ -n "$ODIN_PROMPT_CAPTURE" ]; then
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--prompt-file" ]; then cp "$a" "$ODIN_PROMPT_CAPTURE"; fi
    prev="$a"
  done
fi
case "$FAKE_GROK_SCENARIO" in
  completed)
    printf '%s\n' '{"type":"text","data":"Working"}'
    printf '%s\n' '{"type":"text","data":" ...\n<<<NEXT_CONTEXT>>>\nDo task 2.\n<<<END>>>"}'
    printf '%s\n' '{"type":"end","stopReason":"EndTurn","sessionId":"g-1","usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":2,"total_tokens":17},"total_cost_usd":0.02,"num_turns":3}'
    exit 0
    ;;
  error_event)
    printf '%s\n' '{"type":"text","data":"partial"}'
    printf '%s\n' '{"type":"error","message":"model unavailable"}'
    exit 1
    ;;
  nonzero_exit)
    echo "boom" >&2
    exit 1
    ;;
  *)
    echo "unknown scenario: $FAKE_GROK_SCENARIO" >&2
    exit 99
    ;;
esac
"""


@pytest.fixture
def fake_grok(tmp_path: Path) -> Path:
    script = tmp_path / "fake-grok.sh"
    script.write_text(FAKE_GROK)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _run(scenario: str, fake: Path, project: Path, **kw):
    os.environ["FAKE_GROK_SCENARIO"] = scenario
    try:
        return run_agent(
            "do the thing",
            project,
            backend=GrokBackend(),
            bin=str(fake),
            out=io.StringIO(),
            **kw,
        )
    finally:
        os.environ.pop("FAKE_GROK_SCENARIO", None)


def test_get_backend_resolves_and_rejects():
    assert isinstance(get_backend("grok"), GrokBackend)
    assert isinstance(get_backend("claude"), ClaudeBackend)
    with pytest.raises(ValueError):
        get_backend("nope")


def test_grok_completed_accumulates_deltas_and_reads_end(fake_grok: Path, project_dir: Path):
    r = _run("completed", fake_grok, project_dir)
    assert r.succeeded is True
    assert r.platform == "grok"
    assert r.exit_code == 0
    assert r.session_id == "g-1"
    assert r.stop_reason == "EndTurn"
    # Text arrives as chunk deltas; they must concatenate into final_text so
    # protocol.parse can find the sentinel.
    assert "<<<NEXT_CONTEXT>>>" in r.final_text
    assert r.final_text.startswith("Working ...")
    assert r.cost_usd == 0.02
    assert r.usage["input_tokens"] == 10
    assert r.usage["cache_read_input_tokens"] == 2
    assert r.error is None


def test_grok_error_event_marks_failure(fake_grok: Path, project_dir: Path):
    r = _run("error_event", fake_grok, project_dir)
    assert r.succeeded is False
    assert r.error is not None
    assert "model unavailable" in r.error


def test_grok_nonzero_exit_marks_failure(fake_grok: Path, project_dir: Path):
    r = _run("nonzero_exit", fake_grok, project_dir)
    assert r.succeeded is False
    assert r.exit_code == 1
    assert r.error is not None


def test_grok_argv_maps_flags(fake_grok: Path, project_dir: Path, tmp_path: Path):
    args_file = tmp_path / "argv.txt"
    os.environ["ODIN_ARGS_FILE"] = str(args_file)
    try:
        _run(
            "completed", fake_grok, project_dir,
            system_prompt="CONTRACT",
            allowed_tools=["Read", "Edit"],
            disallowed_tools=["Bash(rm:*)"],
            max_turns=50,
        )
    finally:
        os.environ.pop("ODIN_ARGS_FILE", None)
    argv = args_file.read_text().splitlines()
    assert "--output-format" in argv and "streaming-json" in argv
    assert "--prompt-file" in argv
    # Protocol injected via grok's --rules (its --append-system-prompt alias).
    assert "--rules" in argv and "CONTRACT" in argv
    # Odin's --allowed-tools maps to grok's --tools allowlist.
    assert "--tools" in argv and "Read,Edit" in argv
    assert "--disallowed-tools" in argv and "Bash(rm:*)" in argv
    assert "--max-turns" in argv and "50" in argv
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "bypassPermissions"


def test_grok_prompt_delivered_via_file_and_cleaned_up(fake_grok: Path, project_dir: Path, tmp_path: Path):
    args_file = tmp_path / "argv.txt"
    capture = tmp_path / "prompt-copy.md"
    os.environ["ODIN_ARGS_FILE"] = str(args_file)
    os.environ["ODIN_PROMPT_CAPTURE"] = str(capture)
    try:
        _run("completed", fake_grok, project_dir)
    finally:
        os.environ.pop("ODIN_ARGS_FILE", None)
        os.environ.pop("ODIN_PROMPT_CAPTURE", None)
    # The prompt reached grok through --prompt-file, verbatim …
    assert capture.read_text() == "do the thing"
    # … and the temp prompt file was removed after the run.
    argv = args_file.read_text().splitlines()
    pf = argv[argv.index("--prompt-file") + 1]
    assert pf.endswith(".md")
    assert not Path(pf).exists()


def test_grok_streams_text_to_out(fake_grok: Path, project_dir: Path):
    sink = io.StringIO()
    os.environ["FAKE_GROK_SCENARIO"] = "completed"
    try:
        run_agent("do the thing", project_dir, backend=GrokBackend(),
                  bin=str(fake_grok), out=sink)
    finally:
        os.environ.pop("FAKE_GROK_SCENARIO", None)
    assert "Working" in sink.getvalue()
