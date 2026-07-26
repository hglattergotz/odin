"""Interruption recovery: classification, WIP checkpoint, brief, wait, queue state.

See `docs/interruption-recovery-proposal.md`. The load-bearing guarantees these
tests pin down:

  - an interruption is told apart from a defect *structurally*, so recognising
    the provider's wording is never required for correct routing;
  - the WIP checkpoint leaves a clean tree, never sweeps the queue dir or a
    credential in, and loses nothing when it fails;
  - a recovered task carries exactly one resumption brief, however many times
    it has been recovered.
"""

from __future__ import annotations

import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from odin import git, recovery, wait
from odin.backends.base import FailureKind
from odin.backends.claude import ClaudeBackend, parse_reset_time
from odin.queue import Queue, Task
from odin.runner import RunResult


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _result(**kw) -> RunResult:
    base = dict(
        succeeded=False, final_text="partial work", stop_reason=None, error=None,
        exit_code=0, session_id="s1", platform="claude", stderr="",
    )
    base.update(kw)
    return RunResult(**base)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(path), check=True, capture_output=True)

    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (path / "seed.txt").write_text("seed\n")
    run("add", ".")
    run("commit", "-m", "Milestone 070: the seed")
    return path


def _log(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


# ----------------------------------------------------------------------
# classification — the six real historical failure signatures
# ----------------------------------------------------------------------
# Taken verbatim from ~/.odin/metrics/events.jsonl: 305 task executions, six
# failures. Five were external terminations, one was a genuine protocol defect.
# `error: "success"` is what Claude Code actually emitted (is_error true with
# subtype still "success") before that label was fixed.

_IS_ERROR_SUCCESS = "agent reported is_error with subtype=success"

HISTORICAL = [
    # (name, exit_code, stop_reason, error, expected kind)
    ("019-canvas-center-mark", 0, "end_turn", None, FailureKind.DEFECT),
    ("002-p13-02-cdk-scaffold", 1, "end_turn", _IS_ERROR_SUCCESS, FailureKind.INTERRUPTED),
    ("001-author-openapi-spec", 1, "stop_sequence", _IS_ERROR_SUCCESS, FailureKind.INTERRUPTED),
    ("002-author-llms-txt", 1, "stop_sequence", _IS_ERROR_SUCCESS, FailureKind.INTERRUPTED),
    ("030-current-prices", 1, "stop_sequence", _IS_ERROR_SUCCESS, FailureKind.INTERRUPTED),
    ("080-ui-port", 1, "stop_sequence", _IS_ERROR_SUCCESS, FailureKind.INTERRUPTED),
]


@pytest.mark.parametrize("name,code,stop,error,expected", HISTORICAL)
def test_classifies_historical_failures(name, code, stop, error, expected):
    failure = ClaudeBackend().classify_failure(
        _result(exit_code=code, stop_reason=stop, error=error)
    )
    assert failure.kind is expected, name


def test_historical_split_is_five_to_one():
    """The whole design rests on interruptions dominating real defects."""
    kinds = [
        ClaudeBackend().classify_failure(
            _result(exit_code=c, stop_reason=s, error=e)
        ).kind
        for _, c, s, e, _ in HISTORICAL
    ]
    assert kinds.count(FailureKind.INTERRUPTED) == 5
    assert kinds.count(FailureKind.DEFECT) == 1


def test_unrecognised_message_still_classifies_as_interrupted():
    """The fragility guarantee: routing must not depend on matching wording.

    If a provider reworks its limit notice tomorrow, tier 2 still catches it —
    the only thing lost is the reason label and the reset time.
    """
    failure = ClaudeBackend().classify_failure(
        _result(exit_code=1, final_text="Totally novel wording nobody has seen")
    )
    assert failure.kind is FailureKind.INTERRUPTED
    assert failure.confidence == "probable"
    assert failure.reason == "unknown"
    assert failure.resets_at is None


def test_recognised_limit_is_confirmed_and_enriched():
    failure = ClaudeBackend().classify_failure(
        _result(exit_code=1,
                final_text="You've hit your session limit · resets 3pm (America/New_York)")
    )
    assert failure.kind is FailureKind.INTERRUPTED
    assert failure.confidence == "confirmed"
    assert failure.reason == "usage_limit"
    assert failure.resets_at is not None


def test_limit_notice_on_stderr_is_found_too():
    failure = ClaudeBackend().classify_failure(
        _result(exit_code=1, final_text="", stderr="Error: rate_limit_error (429)")
    )
    assert failure.reason == "usage_limit"


def test_no_sentinel_after_clean_turn_is_a_defect():
    """The agent ended on its own terms and broke the protocol — a human's job."""
    failure = ClaudeBackend().classify_failure(
        _result(exit_code=0, stop_reason="end_turn", final_text="I forgot the protocol.")
    )
    assert failure.kind is FailureKind.DEFECT


def test_max_turns_stays_a_defect():
    """`--max-turns` is the user's own circuit breaker, not an interruption.

    Recovering it would commit the partial work and re-run straight back into
    the same cap, which is precisely what the cap exists to prevent.
    """
    failure = ClaudeBackend().classify_failure(
        _result(exit_code=1, stop_reason="max_turns", error="error_max_turns")
    )
    assert failure.kind is FailureKind.DEFECT
    assert failure.reason == "max_turns"


def test_other_backends_default_to_defect():
    """Cursor/Grok keep today's behaviour until they opt in."""
    from odin.backends.cursor import CursorBackend

    failure = CursorBackend().classify_failure(_result(exit_code=1))
    assert failure.kind is FailureKind.DEFECT


# ----------------------------------------------------------------------
# reset-time parsing
# ----------------------------------------------------------------------

def test_parse_reset_epoch():
    now = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    got = parse_reset_time("Claude AI usage limit reached|1753462800", now=now)
    assert got == datetime.fromtimestamp(1753462800, tz=timezone.utc).astimezone()


def test_parse_reset_epoch_milliseconds():
    now = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    got = parse_reset_time("usage limit reached|1753462800000", now=now)
    assert got == datetime.fromtimestamp(1753462800, tz=timezone.utc).astimezone()


def test_parse_reset_clock_with_zone():
    now = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    got = parse_reset_time("resets 3pm (America/New_York)", now=now)
    assert got is not None
    assert (got.hour, got.minute) == (15, 0)
    assert got.tzinfo.key == "America/New_York"


def test_parse_reset_clock_without_zone_uses_local():
    now = datetime(2026, 7, 25, 10, 0).astimezone()
    got = parse_reset_time("limit reached, resets at 15:30", now=now)
    assert got is not None
    assert (got.hour, got.minute) == (15, 30)


def test_parse_reset_rolls_to_tomorrow_when_already_past():
    now = datetime(2026, 7, 25, 20, 0).astimezone()
    got = parse_reset_time("resets at 15:00", now=now)
    assert got is not None
    assert got.day == 26


def test_parse_reset_absent_or_nonsense():
    assert parse_reset_time("") is None
    assert parse_reset_time("no time here at all") is None
    assert parse_reset_time("resets at 99:99") is None


# ----------------------------------------------------------------------
# git: the WIP checkpoint
# ----------------------------------------------------------------------

def test_commit_wip_leaves_a_clean_tree(tmp_path):
    """The invariant the whole design leans on — no commit, no clean start."""
    repo = _repo(tmp_path / "proj")
    (repo / "new.go").write_text("package main\n")
    (repo / "seed.txt").write_text("changed\n")

    sha, snap = git.commit_wip(repo, stem="080-ui-port", run_id="r1", reason="usage_limit")
    assert sha
    assert snap.files == 2
    assert git.is_clean(repo) == (True, "")


def test_commit_wip_excludes_the_queue_dir(tmp_path):
    repo = _repo(tmp_path / "proj")
    (repo / "work.go").write_text("package main\n")
    qdir = repo / "queue" / "go-rewrite" / "pending"
    qdir.mkdir(parents=True)
    (qdir / "001-a.md").write_text("task")

    git.commit_wip(repo, stem="001-a", ignore_within=repo / "queue")

    committed = _log(repo, "show", "--stat", "--format=", "HEAD")
    assert "work.go" in committed
    assert "queue/" not in committed
    # The queue is still untracked — Odin's bookkeeping, not the project's.
    # (git collapses a wholly untracked directory to a single `?? queue/`.)
    assert "?? queue/" in _log(repo, "status", "--porcelain")
    assert (qdir / "001-a.md").exists()


def test_commit_wip_on_clean_tree_makes_no_empty_commit(tmp_path):
    repo = _repo(tmp_path / "proj")
    before = _log(repo, "rev-parse", "HEAD")
    assert git.commit_wip(repo, stem="001-noop") is None
    assert _log(repo, "rev-parse", "HEAD") == before


def test_commit_wip_writes_findable_trailers(tmp_path):
    repo = _repo(tmp_path / "proj")
    (repo / "a.go").write_text("x\n")
    git.commit_wip(repo, stem="080-ui-port", run_id="a9696b33", reason="usage_limit")

    body = _log(repo, "log", "-1", "--format=%B")
    assert "wip(odin): interrupted attempt at 080-ui-port" in body
    assert "Odin-WIP: 080-ui-port" in body
    assert "Odin-Run: a9696b33" in body
    assert "may not build or pass tests" in body
    assert _log(repo, "log", "--grep=Odin-WIP", "--format=%s")


def test_commit_wip_refuses_secrets_and_stages_nothing(tmp_path):
    """"Never commit secrets" survives contact with an automated `git add -A`."""
    repo = _repo(tmp_path / "proj")
    (repo / "feature.go").write_text("work\n")
    (repo / ".env").write_text("TOKEN=hunter2\n")
    before = _log(repo, "rev-parse", "HEAD")

    with pytest.raises(git.SecretGuardError) as exc:
        git.commit_wip(repo, stem="080")

    assert ".env" in str(exc.value)
    assert _log(repo, "rev-parse", "HEAD") == before
    # Refused *before* staging: both files are still untracked, nothing indexed.
    assert _log(repo, "diff", "--cached", "--name-only") == ""
    assert (repo / ".env").exists()


def test_commit_wip_bypasses_hooks(tmp_path):
    """A pre-commit hook must not be able to block the unblocking mechanism."""
    repo = _repo(tmp_path / "proj")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    (repo / "a.go").write_text("x\n")

    assert git.commit_wip(repo, stem="080") is not None
    assert git.is_clean(repo) == (True, "")


def test_commit_wip_handles_renames_and_deletes(tmp_path):
    repo = _repo(tmp_path / "proj")
    (repo / "keep.txt").write_text("k\n")
    (repo / "gone.txt").write_text("g\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "more"], cwd=repo, check=True, capture_output=True)

    (repo / "gone.txt").unlink()
    subprocess.run(["git", "mv", "keep.txt", "moved.txt"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "brand new.txt").write_text("spaces in the name\n")

    assert git.commit_wip(repo, stem="080") is not None
    assert git.is_clean(repo) == (True, "")


def test_snapshot_counts_files_inside_an_untracked_dir(tmp_path):
    """Git collapses `?? pkg/` into one entry; the count must not follow suit."""
    repo = _repo(tmp_path / "proj")
    (repo / "pkg").mkdir()
    for i in range(3):
        (repo / "pkg" / f"f{i}.go").write_text("package pkg\n")

    snap = git.snapshot(repo)
    assert snap.files == 3
    assert snap.insertions == 3


def test_snapshot_of_clean_tree_is_not_dirty(tmp_path):
    snap = git.snapshot(_repo(tmp_path / "proj"))
    assert not snap.dirty
    assert snap.diffstat() == ""
    assert snap.head_subject == "Milestone 070: the seed"


# ----------------------------------------------------------------------
# queue state and the brief merge
# ----------------------------------------------------------------------

def _queue_with_interrupted(tmp_path: Path) -> tuple[Queue, str]:
    q = Queue(tmp_path / "queue")
    body = q.root / "pending" / "080-ui-port.md"
    body.write_text("# Port the UI\n\nPort the table view.\n")
    running = q.claim_running(Task.from_path(body))
    q.mark_interrupted(running, "# Interrupted — 080-ui-port\n")
    return q, "080-ui-port"


def test_interrupted_round_trip(tmp_path):
    q, stem = _queue_with_interrupted(tmp_path)
    assert [t.stem for t in q.interrupted()] == [stem]
    assert q.recovery_path(stem).exists()
    assert q.counts()["interrupted"] == 1

    task = q.recover_interrupted(stem, "You are resuming.")

    assert task.path == q.root / "pending" / f"{stem}.md"
    assert not (q.root / "interrupted" / f"{stem}.md").exists()
    # The sidecar stays behind as the audit record.
    assert q.recovery_path(stem).exists()
    assert "You are resuming." in task.read()
    assert "Port the table view." in task.read()


def test_recovering_twice_keeps_exactly_one_brief(tmp_path):
    """Three interruptions must not mean three stacked, contradictory briefs."""
    q, stem = _queue_with_interrupted(tmp_path)
    first = q.recover_interrupted(stem, "Brief one: 8 files.")

    running = q.claim_running(first)
    q.mark_interrupted(running, "# again\n")
    second = q.recover_interrupted(stem, "Brief two: 12 files.")

    body = second.read()
    assert body.count("odin:resumption-brief") == 2  # one open + one close
    assert "Brief two: 12 files." in body
    assert "Brief one" not in body
    assert "Port the table view." in body


def test_interrupted_work_blocks_archiving(tmp_path):
    q, _ = _queue_with_interrupted(tmp_path)
    (q.root / "done" / "001-earlier.md").write_text("done")
    ok, reason = q.archive_state()
    assert not ok
    assert "interrupted" in reason


def test_stranded_running_is_recoverable(tmp_path):
    q = Queue(tmp_path / "queue")
    body = q.root / "pending" / "081-stranded.md"
    body.write_text("# Stranded\n")
    q.claim_running(Task.from_path(body))

    assert [t.stem for t in q.stranded_running()] == ["081-stranded"]
    assert [t.stem for t in q.recoverable()] == ["081-stranded"]


# ----------------------------------------------------------------------
# the record and the brief
# ----------------------------------------------------------------------

def _attempt(**kw) -> recovery.Attempt:
    base = dict(reason="usage_limit", confidence="confirmed", turns=85,
                wall_ms=635316, cost_usd=8.07, files=8, insertions=1187,
                deletions=43, new=["internal/ui/table.go"], modified=["main.go"],
                head_before="a3f91c2", head_subject="Milestone 070: alerts",
                last_words="still need the column-width pass")
    base.update(kw)
    return recovery.Attempt(**base)


def test_sidecar_round_trips(tmp_path):
    record = recovery.Record(stem="080-ui-port")
    record.add(_attempt(wip_commit="7c21f0a"))
    text = recovery.render_sidecar(record)

    back = recovery.load(text, stem="080-ui-port")
    assert back.stem == "080-ui-port"
    assert len(back.attempts) == 1
    assert back.latest.wip_commit == "7c21f0a"
    assert back.latest.turns == 85


def test_corrupt_sidecar_never_blocks_recovery():
    """A hand-edited sidecar must not be what stops someone recovering work."""
    assert recovery.load("total garbage, no json here", stem="x").attempts == []
    assert recovery.load("```json\n{not json\n```", stem="x").attempts == []


def test_brief_names_the_commit_and_the_milestone():
    record = recovery.Record(stem="080-ui-port")
    record.add(_attempt(wip_commit="7c21f0a"))
    brief = recovery.build_brief(record)

    assert "You are resuming this task" in brief
    assert "not starting fresh" in brief
    assert "7c21f0a" in brief                     # where its work went
    assert "a3f91c2" in brief                     # what came before it
    assert "Milestone 070: alerts" in brief
    assert "8 files changed, +1187/-43" in brief
    assert "still need the column-width pass" in brief   # its last words
    assert "Prove each acceptance criterion" in brief


def test_brief_is_honest_when_the_process_just_died():
    record = recovery.Record(stem="081")
    record.add(_attempt(reason="process_died", turns=None, wall_ms=None,
                        last_words="", wip_commit="abc1234"))
    brief = recovery.build_brief(record)
    assert "Odin's own process stopped" in brief
    assert "How far it got is not recorded" in brief


def test_brief_when_nothing_was_left_behind():
    """A limit hit before any work — the cheapest possible recovery."""
    record = recovery.Record(stem="001")
    record.add(_attempt(turns=1, files=0, insertions=0, deletions=0,
                        new=[], modified=[], wip_commit=None))
    brief = recovery.build_brief(record)
    assert "left nothing behind" in brief


def test_brief_includes_verify_output_when_configured():
    record = recovery.Record(stem="080")
    record.add(_attempt())
    brief = recovery.build_brief(record, verify_output="$ go build ./...\ntable.go:9: undefined: x")
    assert "undefined: x" in brief


def test_attempt_numbering_and_progress():
    record = recovery.Record(stem="080")
    record.add(_attempt())
    record.add(_attempt(files=3))
    assert [a.n for a in record.attempts] == [1, 2]
    assert not record.blocked


def test_two_dead_attempts_block_recovery():
    """Progress, not attempt count, is the circuit breaker."""
    record = recovery.Record(stem="080")
    record.add(_attempt(turns=0, files=0))
    assert not record.blocked          # once is a limit hit before work started
    record.add(_attempt(turns=0, files=0))
    assert record.blocked              # twice means something is actually wrong


def test_progress_resets_the_breaker():
    record = recovery.Record(stem="080")
    record.add(_attempt(turns=0, files=0))
    record.add(_attempt(turns=61, files=4))
    record.add(_attempt(turns=0, files=0))
    assert not record.blocked


def test_long_task_across_several_windows_is_not_blocked():
    record = recovery.Record(stem="080")
    for turns, files in ((85, 8), (61, 4), (44, 2)):
        record.add(_attempt(turns=turns, files=files))
    assert not record.blocked


# ----------------------------------------------------------------------
# waiting
# ----------------------------------------------------------------------

def test_seconds_until_includes_the_buffer():
    now = datetime(2026, 7, 25, 12, 0).astimezone()
    target = now + timedelta(hours=2)
    assert wait.seconds_until(target, now=now) == 7200 + wait.BUFFER_SECONDS


def test_seconds_until_never_negative():
    now = datetime(2026, 7, 25, 12, 0).astimezone()
    assert wait.seconds_until(now - timedelta(hours=5), now=now) == 0.0


def test_exceeds_cap():
    now = datetime(2026, 7, 25, 12, 0).astimezone()
    assert wait.exceeds_cap(now + timedelta(hours=8), 360, now=now)
    assert not wait.exceeds_cap(now + timedelta(hours=2), 360, now=now)


def test_human_delta():
    assert wait.human_delta(8000) == "2h13m"
    assert wait.human_delta(780) == "13m"
    assert wait.human_delta(45) == "45s"


def test_sleep_until_waits_then_returns(tmp_path):
    """The clock is injected, so the suite never actually sleeps."""
    import io

    clock = {"now": datetime(2026, 7, 25, 12, 0).astimezone()}
    target = clock["now"] + timedelta(minutes=5)
    slept: list[float] = []

    def sleep_fn(secs: float) -> None:
        slept.append(secs)
        clock["now"] += timedelta(seconds=secs)

    out = io.StringIO()
    assert wait.sleep_until(target, out=out, now_fn=lambda: clock["now"],
                            sleep_fn=sleep_fn)
    assert sum(slept) >= 300
    assert "continuing" in out.getvalue()


def test_sleep_until_refuses_beyond_the_cap():
    import io

    now = datetime(2026, 7, 25, 12, 0).astimezone()
    out = io.StringIO()
    assert not wait.sleep_until(now + timedelta(hours=10), out=out, max_minutes=360,
                                now_fn=lambda: now, sleep_fn=lambda s: None)
    assert "not waiting" in out.getvalue()


def test_ctrl_c_during_wait_stops_cleanly():
    import io

    now = datetime(2026, 7, 25, 12, 0).astimezone()

    def sleep_fn(secs: float) -> None:
        raise KeyboardInterrupt

    out = io.StringIO()
    assert not wait.sleep_until(now + timedelta(minutes=5), out=out,
                                now_fn=lambda: now, sleep_fn=sleep_fn)
    assert "cancelled" in out.getvalue()


# ----------------------------------------------------------------------
# [recovery] config defaults
# ----------------------------------------------------------------------
# The flags are the documented surface, but a user who always wants this
# behaviour should not have to retype it on every run. Config is the standing
# default; an explicit flag always wins over it.

class _FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _run_args(*extra: str):
    """Real argparse defaults, so these tests can't drift from the parser."""
    from odin.cli import _build_parser
    return _build_parser().parse_args(["run", "queue/x", *extra])


def _with_config(tmp_path: Path, monkeypatch, body: str) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setenv("ODIN_CONFIG", str(cfg))


def test_config_wait_for_reset_waits_without_the_flag(tmp_path, monkeypatch):
    import io

    from odin.cli import _should_wait
    monkeypatch.setattr("odin.cli.sys.stdin", _FakeStdin(False))
    resets = datetime.now().astimezone() + timedelta(minutes=10)

    assert not _should_wait(_run_args(), resets, out=io.StringIO())
    _with_config(tmp_path, monkeypatch, "[recovery]\nwait_for_reset = true\n")
    assert _should_wait(_run_args(), resets, out=io.StringIO())


def test_config_max_wait_minutes_is_the_default_cap(tmp_path, monkeypatch):
    from odin.cli import _max_wait

    assert _max_wait(_run_args()) == wait.DEFAULT_MAX_WAIT_MINUTES
    _with_config(tmp_path, monkeypatch, "[recovery]\nmax_wait_minutes = 45\n")
    assert _max_wait(_run_args()) == 45
    # An explicit flag beats the standing default.
    assert _max_wait(_run_args("--max-wait", "5")) == 5


def test_config_cap_can_refuse_a_wait_the_flag_asked_for(tmp_path, monkeypatch):
    """`wait_for_reset` says yes, `max_wait_minutes` still gets the last word."""
    import io

    from odin.cli import _should_wait
    monkeypatch.setattr("odin.cli.sys.stdin", _FakeStdin(False))
    _with_config(
        tmp_path, monkeypatch,
        "[recovery]\nwait_for_reset = true\nmax_wait_minutes = 30\n",
    )
    out = io.StringIO()
    resets = datetime.now().astimezone() + timedelta(hours=4)
    assert not _should_wait(_run_args(), resets, out=out)
    assert "beyond the 30-minute cap" in out.getvalue()


def test_config_auto_recover_false_suppresses_the_offer(tmp_path, monkeypatch):
    from odin.cli import _may_recover

    monkeypatch.setattr("odin.cli.sys.stdin", _FakeStdin(True))
    monkeypatch.setattr("odin.cli.ask_continue", lambda *a, **k: True)
    assert _may_recover(_run_args())

    _with_config(tmp_path, monkeypatch, "[recovery]\nauto_recover = false\n")
    assert not _may_recover(_run_args())
    # ...but typing --recover still means it.
    assert _may_recover(_run_args("--recover"))


# ----------------------------------------------------------------------
# CLI wiring
# ----------------------------------------------------------------------

def _cli_setup(tmp_path: Path, *, body: str = "# Port the UI\n\nDo it.\n"):
    """A real git project + a queue holding one pending task."""
    project = _repo(tmp_path / "proj")
    (project / "CLAUDE.md").write_text("# target\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "docs"], cwd=project, check=True,
                   capture_output=True)
    q = Queue(tmp_path / "queue" / "go-rewrite")
    (q.root / "pending" / "080-ui-port.md").write_text(body)
    return project, q


def _interrupting_agent(project: Path, text: str):
    """A run_agent stand-in that leaves real work behind, then gets cut off."""
    def fake(*a, **k):
        (project / "internal").mkdir(exist_ok=True)
        (project / "internal" / "table.go").write_text("package internal\n")
        (project / "main.go").write_text("package main\n")
        return _result(exit_code=1, stop_reason="stop_sequence",
                       error=_IS_ERROR_SUCCESS, final_text=text,
                       num_turns=85, wall_ms=635316, cost_usd=8.07)
    return fake


_LIMIT_TEXT = ("Wired the renderer.\n\nYou've hit your session limit · "
               "resets 3pm (America/New_York)")


def _run(argv: list[str]) -> int:
    from odin.cli import _build_parser, _cmd_run
    return _cmd_run(_build_parser().parse_args(argv))


def test_run_routes_interruption_to_interrupted_not_failed(tmp_path, monkeypatch):
    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("odin.cli.run_agent", _interrupting_agent(project, _LIMIT_TEXT))
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())

    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y"])

    assert rc == 12                                   # not 1: distinct from a defect
    assert (q.root / "interrupted" / "080-ui-port.md").exists()
    assert not (q.root / "failed" / "080-ui-port.md").exists()
    assert q.recovery_path("080-ui-port").exists()


def test_unattended_run_never_commits_on_your_behalf(tmp_path, monkeypatch):
    """Decision 9: a non-TTY run halts. `-y` is not consent to write history."""
    project, q = _cli_setup(tmp_path)
    before = _log(project, "rev-parse", "HEAD")
    monkeypatch.setattr("odin.cli.run_agent", _interrupting_agent(project, _LIMIT_TEXT))
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())

    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y"])

    assert rc == 12
    assert _log(project, "rev-parse", "HEAD") == before      # nothing committed
    assert "main.go" in _log(project, "status", "--porcelain")  # work still there


def test_run_with_recover_flag_commits_and_requeues(tmp_path, monkeypatch):
    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("odin.cli.run_agent", _interrupting_agent(project, _LIMIT_TEXT))
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())

    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y", "--recover"])

    # Halts rather than looping straight back into a limit that is still in force.
    assert rc == 12
    assert (q.root / "pending" / "080-ui-port.md").exists()
    assert git.is_clean(project, ignore_within=q.root) == (True, "")
    assert "Odin-WIP: 080-ui-port" in _log(project, "log", "-1", "--format=%B")

    body = (q.root / "pending" / "080-ui-port.md").read_text()
    assert "You are resuming this task" in body
    assert "Do it." in body


def test_recovered_task_reruns_and_completes(tmp_path, monkeypatch):
    """The whole point: the next agent gets the brief and finishes the job."""
    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    monkeypatch.setattr("odin.cli.run_agent", _interrupting_agent(project, _LIMIT_TEXT))
    _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
          "--no-metrics", "-y", "--recover"])

    seen = {}

    def second(prompt, *a, **k):
        seen["prompt"] = prompt
        return _result(succeeded=True, exit_code=0, stop_reason="end_turn",
                       final_text="<<<NEXT_CONTEXT>>>\nUI ported.\n<<<END>>>")

    monkeypatch.setattr("odin.cli.run_agent", second)
    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y"])

    assert rc == 0
    assert (q.root / "done" / "080-ui-port.md").exists()
    assert "You are resuming this task" in seen["prompt"]
    assert "not starting fresh" in seen["prompt"]


def test_defect_still_goes_to_failed(tmp_path, monkeypatch):
    """The other half of the split must keep working exactly as before."""
    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    monkeypatch.setattr(
        "odin.cli.run_agent",
        lambda *a, **k: _result(exit_code=0, stop_reason="end_turn",
                                final_text="I finished but forgot the protocol."),
    )
    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y"])

    assert rc == 1
    assert (q.root / "failed" / "080-ui-port.md").exists()
    assert not (q.root / "interrupted" / "080-ui-port.md").exists()


def test_cmd_recover_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from odin.cli import _build_parser, _cmd_recover

    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    monkeypatch.setattr("odin.cli.run_agent", _interrupting_agent(project, _LIMIT_TEXT))
    _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
          "--no-metrics", "-y"])
    before = _log(project, "rev-parse", "HEAD")

    args = _build_parser().parse_args(
        ["recover", "080-ui-port", str(q.root), "--project", str(project), "--dry-run"]
    )
    assert _cmd_recover(args) == 0

    out = capsys.readouterr().out
    assert "You are resuming this task" in out       # the brief, verbatim
    assert "nothing was written" in out
    assert _log(project, "rev-parse", "HEAD") == before
    assert (q.root / "interrupted" / "080-ui-port.md").exists()
    assert not (q.root / "pending" / "080-ui-port.md").exists()


def test_cmd_recover_accepts_a_queue_as_its_only_argument(tmp_path, monkeypatch):
    """`odin recover <queue>` must not be read as a task stem."""
    from odin.cli import _build_parser, _disambiguate_recover_args

    project, q = _cli_setup(tmp_path)
    args = _build_parser().parse_args(["recover", str(q.root), "--project", str(project)])
    _disambiguate_recover_args(args)
    assert args.stem is None
    assert args.queue == q.root


def test_cmd_recover_blocked_after_two_dead_attempts(tmp_path, monkeypatch, capsys):
    from odin.cli import _build_parser, _cmd_recover

    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    body = q.root / "pending" / "080-ui-port.md"
    running = q.claim_running(Task.from_path(body))
    record = recovery.Record(stem="080-ui-port")
    record.add(_attempt(turns=0, files=0))
    record.add(_attempt(turns=0, files=0))
    q.mark_interrupted(running, recovery.render_sidecar(record))

    args = _build_parser().parse_args(
        ["recover", "080-ui-port", str(q.root), "--project", str(project)]
    )
    assert _cmd_recover(args) == 12
    assert "no progress" in capsys.readouterr().out
    assert (q.root / "interrupted" / "080-ui-port.md").exists()

    args = _build_parser().parse_args(
        ["recover", "080-ui-port", str(q.root), "--project", str(project),
         "--force", "--yes"]
    )
    assert _cmd_recover(args) == 0
    assert (q.root / "pending" / "080-ui-port.md").exists()


def test_startup_adopts_a_stranded_running_file(tmp_path, monkeypatch):
    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    q.claim_running(Task.from_path(q.root / "pending" / "080-ui-port.md"))
    (project / "orphan.go").write_text("left behind\n")

    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y"])

    assert rc == 12
    assert (q.root / "interrupted" / "080-ui-port.md").exists()
    assert not q.stranded_running()
    sidecar = q.recovery_path("080-ui-port").read_text()
    assert "process_died" in sidecar


def test_secret_in_the_tree_refuses_recovery_without_losing_work(tmp_path, monkeypatch, capsys):
    project, q = _cli_setup(tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())

    def leaky(*a, **k):
        (project / "main.go").write_text("package main\n")
        (project / ".env").write_text("TOKEN=hunter2\n")
        return _result(exit_code=1, stop_reason="stop_sequence",
                       error=_IS_ERROR_SUCCESS, final_text=_LIMIT_TEXT)

    monkeypatch.setattr("odin.cli.run_agent", leaky)
    rc = _run(["run", str(q.root), "--project", str(project), "--platform", "claude",
               "--no-metrics", "-y", "--recover"])

    assert rc == 12
    assert "look like secrets" in capsys.readouterr().out
    assert (project / ".env").exists()                    # nothing destroyed
    assert (q.root / "interrupted" / "080-ui-port.md").exists()   # still recoverable
