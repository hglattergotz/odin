"""End-to-end CLI tests using the fake claude script."""

from __future__ import annotations

import io
import os
import stat
import subprocess
from pathlib import Path

import pytest

from odin.cli import _build_parser, main


FAKE_SCRIPT = r"""#!/bin/sh
cat >/dev/null
case "$FAKE_CLAUDE_SCENARIO" in
  completed)
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\ncarry from this task\n<<<END>>>"}'
    ;;
  held)
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEEDS_INPUT>>>\nWhich db?\n<<<END>>>"}'
    ;;
  held_json_then_done)
    if [ -f "$ODIN_STATE_FILE" ]; then
      printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\ndone now\n<<<END>>>"}'
    else
      : > "$ODIN_STATE_FILE"
      printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEEDS_INPUT>>>\n{\"questions\":[{\"problem\":\"storage undecided\",\"question\":\"Which db?\",\"options\":[{\"key\":\"a\",\"label\":\"Postgres\"},{\"key\":\"b\",\"label\":\"SQLite\"}],\"recommended\":\"a\",\"why\":\"reuse infra\"}]}\n<<<END>>>"}'
    fi
    ;;
  followup_backlog)
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\ndone\n<<<END>>>\n<<<FOLLOW_UP>>>\n[{\"title\":\"add logging\",\"urgent\":false,\"body\":\"add structured logging\"}]\n<<<END>>>"}'
    ;;
  followup_urgent_once)
    if [ -f "$ODIN_STATE_FILE" ]; then
      printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\ndone\n<<<END>>>"}'
    else
      : > "$ODIN_STATE_FILE"
      printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"<<<NEXT_CONTEXT>>>\ndone\n<<<END>>>\n<<<FOLLOW_UP>>>\n[{\"title\":\"fix the race\",\"urgent\":true,\"body\":\"must fix before next\"}]\n<<<END>>>"}'
    fi
    ;;
  unparseable)
    printf '%s\n' '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,"result":"I finished but forgot the protocol."}'
    ;;
  failed)
    printf '%s\n' '{"type":"result","subtype":"error_max_turns","stop_reason":"max_turns","is_error":true,"result":"hit limit"}'
    ;;
esac
"""


def _init_repo(path: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(path), check=True, capture_output=True, text=True)
    run("init", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (path / "seed.txt").write_text("seed\n")
    run("add", ".")
    run("commit", "-m", "initial")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def setup(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# pretend target CLAUDE.md\n")
    queue_dir = tmp_path / "queue"
    fake = tmp_path / "fake-claude.sh"
    fake.write_text(FAKE_SCRIPT)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return project, queue_dir, fake


def _seed_task(queue_dir: Path, name: str, body: str = "task body") -> None:
    pending = queue_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / name).write_text(body, encoding="utf-8")


def _run_cli(args: list[str], scenario: str | None = None) -> int:
    if scenario is not None:
        os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    try:
        return main(args)
    finally:
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)


def test_run_completed_task(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    assert (qdir / "carry" / "001-a.next-context.md").exists()
    assert "carry from this task" in (qdir / "carry" / "001-a.next-context.md").read_text()


def test_resolve_queue_arg_prefers_existing_then_nested(tmp_path, monkeypatch):
    from odin.cli import _resolve_queue_arg

    monkeypatch.chdir(tmp_path)
    (tmp_path / "queue" / "add-search" / "pending").mkdir(parents=True)

    # Bare name resolves under ./queue/.
    assert _resolve_queue_arg(Path("add-search")) == Path("queue/add-search")
    # Explicit queue/<name> path is used as given.
    assert _resolve_queue_arg(Path("queue/add-search")) == Path("queue/add-search")
    # Nonexistent name falls through unchanged (normal not-found handling).
    assert _resolve_queue_arg(Path("nope")) == Path("nope")
    # An existing local dir wins over the queue/ shortcut.
    (tmp_path / "add-search").mkdir()
    assert _resolve_queue_arg(Path("add-search")) == Path("add-search")


def test_run_bare_queue_name_resolves_under_queue(setup, monkeypatch, tmp_path):
    project, qdir, fake = setup
    sub = qdir / "add-search" / "pending"
    sub.mkdir(parents=True)
    (sub / "001-a.md").write_text("do a")
    monkeypatch.chdir(tmp_path)  # so ./queue/add-search resolves

    rc = _run_cli(
        ["run", "add-search", "--project", str(project), "--no-git",
         "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "add-search" / "done" / "001-a.md").exists()


def test_run_carries_context_to_next_task(setup):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md", "first task body")
    _seed_task(qdir, "002-b.md", "second task body")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    assert (qdir / "done" / "002-b.md").exists()
    # The carry from 001 should have been generated, and consumed by 002 as
    # prepended context (we can't directly inspect what the fake claude saw,
    # but presence of the carry file plus 002 succeeding confirms the flow).
    assert (qdir / "carry" / "001-a.next-context.md").exists()
    assert (qdir / "carry" / "002-b.next-context.md").exists()


def test_run_held_task_exits_10_and_prints_resume_instructions(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="held",
    )
    assert rc == 10
    out = capsys.readouterr().out
    assert "needs input" in out
    assert "odin resume 001-a" in out
    assert (qdir / "held" / "001-a.md").exists()
    assert (qdir / "held" / "001-a.questions.md").exists()


def test_run_unparseable_marks_failed(setup):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="unparseable",
    )
    assert rc == 1
    assert (qdir / "failed" / "001-a.md").exists()


def test_run_failed_max_turns(setup):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="failed",
    )
    assert rc == 1
    assert (qdir / "failed" / "001-a.md").exists()


def test_run_empty_queue(setup, capsys):
    project, qdir, fake = setup
    qdir.mkdir(exist_ok=True)  # exists but no pending tasks
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue is empty" in out


def test_run_max_tasks_limit(setup):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    _seed_task(qdir, "002-b.md")
    _seed_task(qdir, "003-c.md")
    rc = _run_cli(
        [
            "run", str(qdir),
            "--project", str(project),
            "--claude-bin", str(fake),
            "--max-tasks", "2",
        ],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    assert (qdir / "done" / "002-b.md").exists()
    assert (qdir / "pending" / "003-c.md").exists()


def test_default_permission_mode_is_full_autonomy():
    args = _build_parser().parse_args(["run"])
    assert args.permission_mode == "bypassPermissions"
    assert args.allowed_tools is None and args.disallowed_tools is None
    assert args.max_turns is None  # no turn cap by default


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_reports_package_location(flag, capsys):
    from odin import __version__

    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args([flag])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "from " in out
    assert "odin" in out


def test_no_subcommand_prints_help_not_error(capsys):
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    # The overview explains what the queue is and what the input looks like.
    assert "queue/<name>/pending/" in out
    assert "NNN-slug.md" in out
    assert "{run,status,resume,demo,guide,archive,metrics,config}" in out


def test_status_lists_each_section(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    _seed_task(qdir, "002-b.md")
    rc = _run_cli(["status", str(qdir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending" in out and "(2)" in out
    assert "001-a.md" in out
    assert "002-b.md" in out


def test_resume_round_trip(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md", "original body")
    # Trigger held
    _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="held",
    )
    # User fills in answers
    qpath = qdir / "held" / "001-a.questions.md"
    qpath.write_text(qpath.read_text() + "Use Postgres because we already have it.\n")
    # Resume
    rc = _run_cli(["resume", "001-a", str(qdir)])
    assert rc == 0
    pending_body = (qdir / "pending" / "001-a.md").read_text()
    assert "Prior questions and the user's answers" in pending_body
    assert "Use Postgres" in pending_body
    assert "original body" in pending_body
    # Now the next run picks it up and completes.
    rc2 = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc2 == 0
    assert (qdir / "done" / "001-a.md").exists()


def test_run_missing_claude_md_warns_but_proceeds(setup, capsys):
    project, qdir, fake = setup
    (project / "CLAUDE.md").unlink()
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    err = capsys.readouterr().err
    assert "CLAUDE.md not found" in err
    assert rc == 0


def test_run_cursor_agents_md_only_no_spurious_claude_warning(
    setup, tmp_path, monkeypatch, capsys
):
    """Acceptance: AGENTS.md-only project + --platform cursor → no CLAUDE.md warn."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    (project / "CLAUDE.md").unlink()
    (project / "AGENTS.md").write_text("# cursor instructions\n")
    rec = _cursor_recorder(
        tmp_path / "fake-agent.sh", tmp_path / "argv.log", tmp_path / "stdin.log"
    )
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec), "--no-git"],
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert "CLAUDE.md" not in err
    assert "not found" not in err
    assert (qdir / "done" / "001-a.md").exists()


def test_run_cursor_missing_instructions_warns(setup, tmp_path, monkeypatch, capsys):
    """Cursor with neither AGENTS.md nor .cursor/rules → missing-instruction warn."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    (project / "CLAUDE.md").unlink()
    rec = _cursor_recorder(
        tmp_path / "fake-agent.sh", tmp_path / "argv.log", tmp_path / "stdin.log"
    )
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec), "--no-git"],
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert "AGENTS.md" in err
    assert ".cursor/rules" in err
    assert "not found" in err or "none of" in err


def test_run_cursor_rules_dir_alone_suppresses_missing_warn(
    setup, tmp_path, monkeypatch, capsys
):
    """`.cursor/rules/` without AGENTS.md is enough for Cursor — no missing warn."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    (project / "CLAUDE.md").unlink()
    (project / ".cursor" / "rules").mkdir(parents=True)
    (project / ".cursor" / "rules" / "base.mdc").write_text("# rules\n")
    rec = _cursor_recorder(
        tmp_path / "fake-agent.sh", tmp_path / "argv.log", tmp_path / "stdin.log"
    )
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec), "--no-git"],
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert "not found" not in err
    assert "none of" not in err


def test_run_missing_project_dir_errors(setup):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", "/nonexistent/path", "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 2


def test_dry_run_prints_prompt_and_does_not_invoke(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md", "the task body")
    rc = _run_cli(
        [
            "run", str(qdir),
            "--project", str(project),
            "--claude-bin", str(fake),
            "--dry-run",
        ],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "the task body" in out
    # Nothing moved.
    assert (qdir / "pending" / "001-a.md").exists()
    assert not (qdir / "done" / "001-a.md").exists()


# ----------------------------------------------------------------------
# pre-run platform/model confirmation
# ----------------------------------------------------------------------

class _TTYConfirm(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_run_confirm_tty_yes_proceeds(setup, monkeypatch, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    monkeypatch.setattr("sys.stdin", _TTYConfirm("\n"))  # empty = proceed
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Odin will run this queue with:" in out
    assert "platform:  claude" in out
    assert "model:     (platform default)" in out
    assert (qdir / "done" / "001-a.md").exists()


def test_run_confirm_tty_no_aborts_untouched(setup, monkeypatch, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    monkeypatch.setattr("sys.stdin", _TTYConfirm("n\n"))
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "odin: aborted." in out
    assert (qdir / "pending" / "001-a.md").exists()
    assert not (qdir / "done" / "001-a.md").exists()


def test_run_confirm_non_tty_info_line_proceeds(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    # pytest stdin is not a TTY → info line, no prompt wait.
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "odin: platform=claude model=(platform default)" in out
    assert "Proceed?" not in out
    assert (qdir / "done" / "001-a.md").exists()


def test_run_confirm_yes_flag_skips_prompt_on_tty(setup, monkeypatch, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    monkeypatch.setattr("sys.stdin", _TTYConfirm("n\n"))  # would abort if prompted
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake),
         "--no-git", "--yes"],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Proceed?" not in out
    assert "odin: aborted." not in out
    assert (qdir / "done" / "001-a.md").exists()


def test_run_confirm_dry_run_skips_prompt(setup, monkeypatch, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md", "the task body")
    monkeypatch.setattr("sys.stdin", _TTYConfirm("n\n"))  # would abort if prompted
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake),
         "--dry-run"],
        scenario="completed",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "Proceed?" not in out
    assert "odin: aborted." not in out
    assert (qdir / "pending" / "001-a.md").exists()


# ----------------------------------------------------------------------
# platform / model selection (Batch A4)
# ----------------------------------------------------------------------

def test_run_platform_claude_behaves_identically(setup, monkeypatch):
    """`--platform claude` is the default path — same outcome as no flag."""
    monkeypatch.delenv("ODIN_PLATFORM", raising=False)
    monkeypatch.delenv("ODIN_MODEL", raising=False)
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "claude", "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()


def _arg_recorder(path: Path, log: Path) -> Path:
    """A fake claude that records its argv to `log`, then emits a completed result."""
    result = (
        '{"type":"result","subtype":"success","stop_reason":"end_turn",'
        '"is_error":false,"result":"<<<NEXT_CONTEXT>>>\\ndone\\n<<<END>>>"}'
    )
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        "cat >/dev/null\n"
        f"printf '%s\\n' '{result}'\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_run_model_flag_passed_to_claude(setup, tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_MODEL", raising=False)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _arg_recorder(tmp_path / "recorder.sh", log)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--claude-bin", str(rec), "--model", "claude-test-model"],
    )
    assert rc == 0
    logged = log.read_text().splitlines()
    assert "--model" in logged
    assert "claude-test-model" in logged


def test_run_no_model_emits_no_model_flag(setup, tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_MODEL", raising=False)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _arg_recorder(tmp_path / "recorder.sh", log)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(rec)],
    )
    assert rc == 0
    assert "--model" not in log.read_text().splitlines()


def test_run_unknown_platform_errors_clearly(setup, capsys, monkeypatch):
    """An unregistered `--platform` is a hard error before anything runs."""
    monkeypatch.delenv("ODIN_PLATFORM", raising=False)
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "kiro", "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown platform" in err
    assert "kiro" in err
    # Nothing ran — the task is untouched in pending/.
    assert (qdir / "pending" / "001-a.md").exists()


def test_run_model_from_env(setup, tmp_path, monkeypatch):
    """$ODIN_MODEL supplies the model when no --model flag is given."""
    monkeypatch.setenv("ODIN_MODEL", "env-model-id")
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _arg_recorder(tmp_path / "recorder.sh", log)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(rec)],
    )
    assert rc == 0
    logged = log.read_text().splitlines()
    assert "--model" in logged
    assert "env-model-id" in logged


# ----------------------------------------------------------------------
# --platform cursor wiring (Batch B1–B2)
# ----------------------------------------------------------------------

def _cursor_recorder(path: Path, log: Path, stdin_log: Path) -> Path:
    """A fake `agent` that records argv + stdin, then emits a Cursor-shaped
    terminal result (no stop_reason, camelCase usage) with a completed sentinel."""
    result = (
        '{"type":"result","subtype":"success","duration_ms":7,'
        '"duration_api_ms":6,"is_error":false,'
        '"result":"<<<NEXT_CONTEXT>>>\\ncursor carry\\n<<<END>>>",'
        '"session_id":"sess-cursor","usage":{"inputTokens":11,"outputTokens":2,'
        '"cacheReadTokens":3,"cacheWriteTokens":4}}'
    )
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        f'cat > "{stdin_log}"\n'
        f"printf '%s\\n' '{result}'\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_run_platform_cursor_end_to_end(setup, tmp_path, monkeypatch):
    """`--platform cursor` drives the agent CLI: cursor argv, protocol on
    stdin (not a flag), --claude-bin ignored, and the task lands in done/."""
    monkeypatch.delenv("ODIN_PLATFORM", raising=False)
    monkeypatch.delenv("ODIN_MODEL", raising=False)
    monkeypatch.delenv("ODIN_CONFIG", raising=False)
    project, qdir, fake = setup
    log = tmp_path / "argv.log"
    stdin_log = tmp_path / "stdin.log"
    rec = _cursor_recorder(tmp_path / "fake-agent.sh", log, stdin_log)
    # Binary supplied via config here — the --agent-bin flag path is covered
    # in the Batch B4–B5 section below.
    home = Path(os.environ["ODIN_HOME"])
    (home / "config.toml").write_text(
        f'[platforms.cursor]\nbinary = "{rec}"\n', encoding="utf-8"
    )
    _seed_task(qdir, "001-a.md", "the cursor task body")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--claude-bin", str(fake)],
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    assert "cursor carry" in (qdir / "carry" / "001-a.next-context.md").read_text()
    logged = log.read_text().splitlines()
    assert "-p" in logged
    assert "--force" in logged and "--trust" in logged
    assert logged[logged.index("--workspace") + 1] == str(project)
    # Protocol goes in via stdin prepend, never a flag; claude flags are absent.
    for flag in ("--append-system-prompt", "--permission-mode", "--verbose"):
        assert flag not in logged
    stdin = stdin_log.read_text()
    assert stdin.startswith("<!-- ODIN_PROTOCOL")
    assert "<!-- END ODIN_PROTOCOL -->" in stdin
    assert "the cursor task body" in stdin
    assert stdin.index("END ODIN_PROTOCOL") < stdin.index("the cursor task body")


# ----------------------------------------------------------------------
# Cursor CLI flags + dry-run from build_invoke (Batch B4–B5)
# ----------------------------------------------------------------------

def _clean_platform_env(monkeypatch):
    for var in ("ODIN_PLATFORM", "ODIN_MODEL", "ODIN_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def test_run_cursor_agent_bin_flag_end_to_end(setup, tmp_path, monkeypatch):
    """Acceptance: --platform cursor + --agent-bin (no config needed) drives the
    fake agent to a completed sentinel and the task lands in done/."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    stdin_log = tmp_path / "stdin.log"
    rec = _cursor_recorder(tmp_path / "fake-agent.sh", log, stdin_log)
    _seed_task(qdir, "001-a.md", "cursor flag task")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec)],
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    assert "cursor carry" in (qdir / "carry" / "001-a.next-context.md").read_text()
    logged = log.read_text().splitlines()
    # The recorder logs "$@" (no argv[0]) — it having run at all proves the
    # --agent-bin flag picked the binary (no config supplied one).
    assert "-p" in logged
    assert "--force" in logged and "--trust" in logged


def test_run_cursor_agent_bin_beats_config_binary(setup, tmp_path, monkeypatch):
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _cursor_recorder(tmp_path / "fake-agent.sh", log, tmp_path / "stdin.log")
    home = Path(os.environ["ODIN_HOME"])
    (home / "config.toml").write_text(
        '[platforms.cursor]\nbinary = "/nonexistent/agent"\n', encoding="utf-8"
    )
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec)],
    )
    assert rc == 0                 # the config binary would have crashed the run
    assert (qdir / "done" / "001-a.md").exists()


def test_run_cursor_sandbox_and_approve_mcps_flags_forwarded(setup, tmp_path, monkeypatch):
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _cursor_recorder(tmp_path / "fake-agent.sh", log, tmp_path / "stdin.log")
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec),
         "--force", "--trust", "--sandbox", "disabled", "--approve-mcps"],
    )
    assert rc == 0
    logged = log.read_text().splitlines()
    assert logged[logged.index("--sandbox") + 1] == "disabled"
    assert "--approve-mcps" in logged
    assert "--force" in logged and "--trust" in logged


def test_run_claude_warns_and_ignores_cursor_flags(setup, tmp_path, monkeypatch, capsys):
    """Cursor-only flags on the (default) claude platform warn once and are
    dropped — the run proceeds and the claude argv never sees them."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _arg_recorder(tmp_path / "recorder.sh", log)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--agent-bin", str(rec),
         "--force", "--trust", "--sandbox", "disabled", "--approve-mcps"],
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    err = capsys.readouterr().err
    for flag in ("--force", "--trust", "--sandbox", "--approve-mcps"):
        assert flag in err
    assert "ignoring" in err
    assert "--agent-bin" not in err  # universal binary flag — not platform-gated
    logged = log.read_text().splitlines()
    for flag in ("--force", "--trust", "--sandbox", "--approve-mcps"):
        assert flag not in logged


def test_run_claude_agent_bin_overrides_binary(setup, tmp_path, monkeypatch):
    """`--agent-bin` is the universal binary override, including for Claude."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    log = tmp_path / "argv.log"
    rec = _arg_recorder(tmp_path / "recorder.sh", log)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--agent-bin", str(rec)],
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()


def test_dry_run_cursor_prints_agent_argv_not_claude(setup, monkeypatch, capsys):
    """Dry-run sources the preview from backend.build_invoke (proposal C5)."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    _seed_task(qdir, "001-a.md", "the cursor dry body")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--platform", "cursor",
         "--agent-bin", "/opt/cursor/agent", "--sandbox", "disabled", "--dry-run"],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run] platform: cursor" in out
    assert "/opt/cursor/agent" in out
    assert "--workspace" in out and "--force" in out and "--trust" in out
    assert "--sandbox disabled" in out
    assert "claude" not in out.split("would invoke:")[1].splitlines()[0]
    assert "the cursor dry body" in out
    # Nothing moved, nothing invoked (the binary doesn't even exist).
    assert (qdir / "pending" / "001-a.md").exists()


def test_dry_run_claude_prints_backend_argv(setup, monkeypatch, capsys):
    _clean_platform_env(monkeypatch)
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--claude-bin", str(fake), "--dry-run"],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run] platform: claude" in out
    assert str(fake) in out                     # the resolved binary, not "claude"
    assert "--permission-mode bypassPermissions" in out


# ----------------------------------------------------------------------
# git startup
# ----------------------------------------------------------------------

def test_run_refuses_dirty_tree(setup, capsys):
    project, qdir, fake = setup
    _init_repo(project)
    (project / "seed.txt").write_text("uncommitted change\n")  # dirty
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not clean" in err
    assert "seed.txt" in err
    # Nothing ran.
    assert (qdir / "pending" / "001-a.md").exists()


def test_run_branch_flag_creates_and_uses_branch(setup):
    project, qdir, fake = setup
    _init_repo(project)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        [
            "run", str(qdir),
            "--project", str(project),
            "--claude-bin", str(fake),
            "--branch", "odin/batch", "--base", "main",
        ],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    assert _git(project, "rev-parse", "--abbrev-ref", "HEAD") == "odin/batch"


def test_run_no_git_skips_clean_check(setup):
    project, qdir, fake = setup
    _init_repo(project)
    (project / "seed.txt").write_text("dirty but ignored\n")
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="completed",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()


def test_run_warns_on_conflicting_claude_md_but_proceeds(setup, capsys):
    project, qdir, fake = setup
    # Conflicting git workflow, committed so the tree is clean.
    (project / "CLAUDE.md").write_text("# proj\nAlways open a pull request per task.\n")
    _init_repo(project)
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake),
         "--branch", "main"],
        scenario="completed",
    )
    assert rc == 0  # advisory only — never blocks
    err = capsys.readouterr().err
    assert "pull request" in err
    assert (qdir / "done" / "001-a.md").exists()


def test_run_warns_on_conflicting_agents_md_but_proceeds(
    setup, tmp_path, monkeypatch, capsys
):
    """Cursor platform scans AGENTS.md (not CLAUDE.md) for git-workflow conflicts."""
    _clean_platform_env(monkeypatch)
    project, qdir, _ = setup
    (project / "CLAUDE.md").unlink()
    (project / "AGENTS.md").write_text("# proj\nAlways open a pull request per task.\n")
    _init_repo(project)
    rec = _cursor_recorder(
        tmp_path / "fake-agent.sh", tmp_path / "argv.log", tmp_path / "stdin.log"
    )
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project),
         "--platform", "cursor", "--agent-bin", str(rec), "--branch", "main"],
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "pull request" in err
    assert "AGENTS.md" in err
    assert (qdir / "done" / "001-a.md").exists()


def test_run_non_git_project_warns_but_proceeds(setup, capsys):
    project, qdir, fake = setup  # project is not a git repo
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake)],
        scenario="completed",
    )
    assert rc == 0
    assert "not a git repo" in capsys.readouterr().err


# ----------------------------------------------------------------------
# interactive Q&A
# ----------------------------------------------------------------------

class _TTYStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_held_answers_and_continues(setup, monkeypatch, tmp_path):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md", "do the thing")
    state = tmp_path / "state"
    monkeypatch.setenv("ODIN_STATE_FILE", str(state))
    # Pretend we're on a TTY and answer the single question with option "a".
    # --yes skips the pre-run confirm so stdin is only for the held Q&A.
    monkeypatch.setattr("sys.stdin", _TTYStdin("a\n"))

    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake),
         "--no-git", "--yes"],
        scenario="held_json_then_done",
    )
    assert rc == 0
    # First run held → answered interactively → re-run completed.
    assert (qdir / "done" / "001-a.md").exists()
    # The audit questions file records the answer and the raw JSON block.
    qfile = (qdir / "held" / "001-a.questions.md").read_text()
    assert "Postgres" in qfile
    assert "raw agent block" in qfile
    # The resumed prompt carried the Q+A into the body that completed.
    # (body is consumed into done/, with the merged Q+A.)
    done_body = (qdir / "done" / "001-a.md").read_text()
    assert "Prior questions and the user's answers" in done_body
    assert "do the thing" in done_body


def test_non_tty_held_falls_back_to_file(setup, monkeypatch, tmp_path):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    state = tmp_path / "state"
    monkeypatch.setenv("ODIN_STATE_FILE", str(state))
    # Default pytest stdin is not a TTY → file fallback, exit 10.
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="held_json_then_done",
    )
    assert rc == 10
    assert (qdir / "held" / "001-a.questions.md").exists()


# ----------------------------------------------------------------------
# discovered follow-up work
# ----------------------------------------------------------------------

def test_followup_backlog_records_and_notifies(setup, capsys):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="followup_backlog",
    )
    assert rc == 0
    # Recorded to backlog, original completed, end-of-run notice printed.
    backlog = list((qdir / "backlog").glob("*.md"))
    assert len(backlog) == 1 and "add-logging" in backlog[0].name
    assert (qdir / "done" / "001-a.md").exists()
    out = capsys.readouterr().out
    assert "backlog" in out and "need attention" in out


def test_followup_urgent_no_tty_halts(setup, capsys, monkeypatch, tmp_path):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    monkeypatch.setenv("ODIN_STATE_FILE", str(tmp_path / "state"))
    # pytest stdin is not a TTY → urgent insert halts with exit 11.
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="followup_urgent_once",
    )
    assert rc == 11
    assert (qdir / "done" / "001-a.md").exists()
    inserted = list((qdir / "pending").glob("001-a-followup-*.md"))
    assert len(inserted) == 1  # urgent task parked in pending for next run
    assert "urgent follow-up" in capsys.readouterr().out


class _TTYStdinFollow(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_followup_urgent_tty_continue_runs_it_next(setup, monkeypatch, tmp_path):
    project, qdir, fake = setup
    _seed_task(qdir, "001-a.md")
    monkeypatch.setenv("ODIN_STATE_FILE", str(tmp_path / "state"))
    # --yes skips the pre-run confirm so stdin is only for ask_continue.
    monkeypatch.setattr("sys.stdin", _TTYStdinFollow("c\n"))  # answer "continue"
    rc = _run_cli(
        ["run", str(qdir), "--project", str(project), "--claude-bin", str(fake),
         "--no-git", "--yes"],
        scenario="followup_urgent_once",
    )
    assert rc == 0
    assert (qdir / "done" / "001-a.md").exists()
    # The inserted urgent task ran next and completed.
    done_followup = list((qdir / "done").glob("001-a-followup-*.md"))
    assert len(done_followup) == 1


# ----------------------------------------------------------------------
# archive + richer status
# ----------------------------------------------------------------------

def test_archive_command_moves_finished_subqueues_and_reports(setup, capsys):
    project, qdir, fake = setup
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    # A container with one finished sub-queue and one still active.
    done = Queue(qdir / "alpha")
    (done.root / "done" / "001-a.md").write_text("done")
    active = Queue(qdir / "beta")
    (active.root / "pending" / "001-x.md").write_text("todo")

    rc = _run_cli(["archive", str(qdir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "archived 1 sub-queue(s)" in out
    assert "alpha" in out
    # Whole finished sub-queue moved; active one untouched.
    assert not (qdir / "alpha").exists()
    assert (qdir / "archive" / "alpha" / "done" / "001-a.md").exists()
    assert (qdir / "beta" / "pending" / "001-x.md").exists()


def test_archive_command_nothing_finished(setup, capsys):
    project, qdir, fake = setup
    active = __import__("odin.queue", fromlist=["Queue"]).Queue(qdir / "beta")
    (active.root / "pending" / "001-x.md").write_text("todo")
    rc = _run_cli(["archive", str(qdir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to archive" in out
    assert "kept beta  (1 pending)" in out


def test_archive_command_no_subqueues(setup, capsys):
    project, qdir, fake = setup
    (qdir / "pending").mkdir(parents=True, exist_ok=True)
    rc = _run_cli(["archive", str(qdir)])
    assert rc == 0
    assert "no sub-queues to archive" in capsys.readouterr().err


def test_status_overview_orders_newest_first_with_footer(setup, capsys):
    import os

    project, qdir, fake = setup
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    old = Queue(qdir / "old")
    (old.root / "done" / "001-a.md").write_text("x")
    new = Queue(qdir / "new")
    p = new.root / "pending" / "001-x.md"
    p.write_text("y")
    os.utime(old.root / "done" / "001-a.md", (1_000_000, 1_000_000))
    os.utime(p, (2_000_000, 2_000_000))
    # An already-archived sub-queue should surface in the footer count.
    (qdir / "archive" / "gone" / "done").mkdir(parents=True)

    rc = _run_cli(["status", str(qdir)])
    assert rc == 0
    out = capsys.readouterr().out
    # Order by the indented sub-queue listing lines, not a raw substring search
    # of the whole output — the temp path itself can contain "old"
    # (e.g. /private/var/folders/...), which would false-match.
    listing = [ln for ln in out.splitlines() if ln.startswith("  ")]
    new_i = next(i for i, ln in enumerate(listing) if ln.strip().startswith("new"))
    old_i = next(i for i, ln in enumerate(listing) if ln.strip().startswith("old"))
    assert new_i < old_i  # newest-active first
    assert "Listed newest first" in out
    assert "1 archived" in out


def test_status_shows_hints_and_ages(setup, capsys):
    project, qdir, fake = setup
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    q = Queue(qdir)
    (q.root / "held" / "001-x.md").write_text("body")
    (q.root / "held" / "001-x.questions.md").write_text("# q\n## Answers\n")
    q.add_backlog("later", "body")

    rc = _run_cli(["status", str(qdir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "odin resume 001-x" in out          # next-action hint for held
    assert "promote: move to pending/" in out  # hint for backlog
    assert "ago)" in out                        # age annotation present


# ----------------------------------------------------------------------
# container detection (the queue/ vs queue/<name> footgun)
# ----------------------------------------------------------------------

def test_status_on_container_shows_overview_and_creates_nothing(tmp_path, capsys):
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    container = tmp_path / "queue"
    Queue(container / "waitlist")
    (container / "waitlist" / "done" / "001-x.md").write_text("done")
    (container / "waitlist" / "done" / "002-y.md").write_text("done")
    Queue(container / "auth")
    (container / "auth" / "pending" / "001-a.md").write_text("task")
    Queue(container / "fixme")
    (container / "fixme" / "held" / "001-h.md").write_text("held")
    (container / "fixme" / "failed" / "002-f.md").write_text("fail")

    rc = main(["status", str(container)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue overview" in out and "3 sub-queue(s)" in out
    # Progress as done/total so you can tell 3-of-3 from 3-of-10.
    assert "0/1 done, 1 pending" in out    # auth
    assert "2/2 done" in out               # waitlist
    assert "needs input" in out and "has failures" in out  # fixme flags
    # Read-only: must not fabricate standard subdirs in the container.
    assert not (container / "pending").exists()
    assert not (container / "done").exists()


def test_status_overview_shows_backlog_separately(tmp_path, capsys):
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    container = tmp_path / "queue"
    q = Queue(container / "feat")
    (q.root / "done" / "001-a.md").write_text("done")
    q.add_backlog("later thing", "body")
    rc = main(["status", str(container)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1/1 done, +1 backlog" in out   # backlog not counted in the total


def test_status_detail_flag_expands_every_subqueue(tmp_path, capsys):
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    container = tmp_path / "queue"
    qa = Queue(container / "alpha")
    (qa.root / "pending" / "001-aa.md").write_text("a")
    qb = Queue(container / "beta")
    (qb.root / "done" / "001-bb.md").write_text("b")

    rc = main(["status", str(container), "--detail"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue overview" in out                 # summary still shown first
    assert str(qa.root) in out and str(qb.root) in out  # each queue detailed
    assert "001-aa.md" in out and "001-bb.md" in out    # task-level lines


def test_status_detail_alias_a_parses():
    args = _build_parser().parse_args(["status", "q", "-a"])
    assert args.detail is True


def test_status_on_real_queue_lists(tmp_path, capsys):
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    q = Queue(tmp_path / "queue" / "waitlist")
    (q.root / "done" / "001-x.md").write_text("done")
    rc = main(["status", str(q.root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done     (1)" in out
    assert "001-x.md" in out


def test_run_on_container_errors_without_creating(setup, tmp_path, capsys):
    project, _, fake = setup
    Queue = __import__("odin.queue", fromlist=["Queue"]).Queue
    container = tmp_path / "queue"
    Queue(container / "waitlist")
    (container / "waitlist" / "pending" / "001-x.md").write_text("task")
    rc = _run_cli(
        ["run", str(container), "--project", str(project), "--claude-bin", str(fake), "--no-git"],
        scenario="completed",
    )
    assert rc == 2
    assert "holds sub-queues" in capsys.readouterr().err
    assert not (container / "pending").exists()


# ----- odin config ---------------------------------------------------

def test_config_set_get_round_trip(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("ODIN_CONFIG", str(cfg))
    assert main(["config", "set", "platforms.claude.model", "sonnet"]) == 0
    capsys.readouterr()
    assert main(["config", "get", "platforms.claude.model"]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"
    assert cfg.read_text() == (
        "[platforms.claude]\n"
        'model = "sonnet"\n'
    )


def test_config_get_missing_key_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ODIN_CONFIG", str(tmp_path / "config.toml"))
    assert main(["config", "get", "platforms.claude.model"]) == 1
    assert "key not set" in capsys.readouterr().err


def test_config_show_includes_effective(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("ODIN_CONFIG", str(cfg))
    monkeypatch.delenv("ODIN_PLATFORM", raising=False)
    monkeypatch.delenv("ODIN_MODEL", raising=False)
    main(["config", "set", "default_platform", "claude"])
    capsys.readouterr()
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "effective platform: claude" in out
    assert "effective model:    (platform default)" in out


def test_config_set_coerces_bool(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("ODIN_CONFIG", str(cfg))
    main(["config", "set", "platforms.claude.verbose", "true"])
    assert "verbose = true" in cfg.read_text()


def test_config_get_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ODIN_CONFIG", str(tmp_path / "config.toml"))
    assert main(["config", "get"]) == 2
    assert "usage" in capsys.readouterr().err
