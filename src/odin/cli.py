"""odin CLI — argparse entry point.

Subcommands:
  odin run    [QUEUE]  --project P  --max-tasks N  --allowed-tools CSV ...
  odin status [QUEUE]
  odin resume STEM     [QUEUE]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import __version__, completed, git, metrics, style, term
from .contract import build_system_prompt
from .demo import DemoError, DemoExists, create_demo
from .guide import TOPICS, render as render_guide
from .lint import scan_claude_md
from .prompts import (
    BranchPlan, ask_branch_choice, ask_continue, ask_questions, render_questions,
)
from .protocol import (
    FollowUp, Outcome, Question, parse, parse_follow_ups, parse_questions, unwrap_fence,
)
from .queue import Queue, Task, archive_finished_subqueues, archived_subqueues
from .runner import RunResult, get_backend, run_agent


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # No subcommand given — show the overview help instead of erroring.
        parser.print_help()
        return 0
    return args.func(args)


def _entry() -> None:
    """Console-script entry point — translates main()'s int into a real exit code."""
    sys.exit(main())


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------

_OVERVIEW = """\
Odin runs a queue of tasks through Claude Code (`claude -p`) one at a time,
each in a fresh session, carrying context forward between tasks and pausing
when the agent needs your input.

The queue
  Organize tasks into NAMED queues under ./queue — one sub-queue per batch of
  related work: queue/<name>/pending/. Odin manages the sibling state dirs
  (running/ done/ failed/ held/ carry/ backlog/). You only create pending/.

The input
  ONE Markdown file per task, dropped into queue/<name>/pending/ and named
  NNN-slug.md (e.g. 001-add-readme.md). The file body IS the prompt —
  plain Markdown, no frontmatter, no special format. The NNN prefix sets
  run order; each file is one task, run in sequence. Many files = many tasks.

How a task ends
  The agent finishes with a hidden marker Odin reads: either it completes
  (and Odin moves on, carrying context to the next task) or it asks a
  question — which Odin shows you in the terminal to answer on the spot.
"""

_QUICKSTART = """\
quickstart:
  cd ~/code/myproject                          # any project that has a CLAUDE.md
  mkdir -p queue/add-license/pending           # a named queue for this batch
  echo 'Add an MIT LICENSE file.' > queue/add-license/pending/001-license.md
  echo 'Add a CHANGELOG.'         > queue/add-license/pending/002-changelog.md
  odin run queue/add-license --branch add-license --base main

  # Always use a named queue (queue/<name>/pending/), one per batch of work.
  # `odin status queue` shows every named queue at a glance; add a name to drill in.

Run `odin run -h` for the full set of run options.
Run `odin guide` for a complete task-authoring manual (an agent can read it
to learn the format with no other context). `odin demo DIR` scaffolds a
working example project.
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odin",
        description=_OVERVIEW,
        epilog=_QUICKSTART,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _pkg_dir = Path(__file__).resolve().parent
    p.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"odin {__version__} (from {_pkg_dir})",
    )
    sub = p.add_subparsers(
        dest="cmd", metavar="{run,status,resume,demo,guide,archive,metrics}"
    )

    run = sub.add_parser(
        "run",
        help="run pending tasks through claude -p",
        description=(
            "Run every NNN-slug.md file in <queue>/pending/ through claude -p, "
            "in lexicographic (numeric-prefix) order, one fresh session each. "
            "Each file's body is the task prompt. Stops when the queue drains, "
            "a task fails, or (unattended) a task needs input."
        ),
    )
    run.add_argument(
        "queue", nargs="?", default="./queue", type=Path,
        help="queue directory (default: ./queue). A bare name resolves under "
             "./queue/, so `odin run add-search` == `odin run queue/add-search`.",
    )
    run.add_argument(
        "--project", default=None, type=Path,
        help="target project directory (default: current working directory)",
    )
    run.add_argument(
        "--max-tasks", type=int, default=0,
        help="stop after N successfully completed tasks (0 = no limit)",
    )
    run.add_argument(
        "--allowed-tools", default=None,
        help="comma-separated allowlist passed to claude --allowed-tools "
             "(restricts the agent to only these tools)",
    )
    run.add_argument(
        "--disallowed-tools", default=None,
        help="comma-separated denylist passed to claude --disallowed-tools, "
             "e.g. 'Bash(rm:*),WebFetch' (carve-outs from the default autonomy)",
    )
    run.add_argument(
        "--permission-mode", default="bypassPermissions",
        help="claude --permission-mode (default: bypassPermissions — the agent "
             "runs all tools ungated; pass acceptEdits/default to restrict)",
    )
    run.add_argument(
        "--max-turns", type=int, default=None,
        help="cap the agent's turns per task (default: no limit — set this only "
             "as a circuit-breaker against runaway sessions)",
    )
    run.add_argument(
        "--claude-bin", default="claude",
        help="path to the claude CLI (default: claude on PATH)",
    )
    run.add_argument(
        "--platform", choices=["claude", "grok"], default=None,
        help="headless agent CLI to drive the queue (default: claude; also "
             "$ODIN_PLATFORM). 'grok' uses grok-build's headless mode.",
    )
    run.add_argument(
        "--grok-bin", default="grok",
        help="path to the grok CLI (default: grok on PATH; used with --platform grok)",
    )
    run.add_argument(
        "--branch", default=None,
        help="branch to run the whole queue on; created from --base if absent. "
             "Skips the interactive branch prompt.",
    )
    run.add_argument(
        "--base", default=None,
        help="branch point for a new --branch (default: current branch)",
    )
    run.add_argument(
        "--no-git", action="store_true",
        help="skip all git startup (clean-tree check + branch selection)",
    )
    run.add_argument(
        "--no-metrics", action="store_true",
        help="don't record run/task metrics for this run (also: ODIN_NO_METRICS=1)",
    )
    run.add_argument(
        "--no-title", action="store_true",
        help="suppress terminal/tab title + progress updates (also: ODIN_NO_TITLE=1). "
             "Titles are on by default — OSC 0/9;4 are universally safe.",
    )
    run.add_argument(
        "--notify", action="store_true",
        help="enable iTerm2 attention bounce + notification + tab color on "
             "held/failed/done (off by default; also: ODIN_NOTIFY=1)",
    )
    run.add_argument(
        "--tab-title", default="odin", metavar="PREFIX",
        help="leading token in the tab title (default: odin) — lets two "
             "projects' Odin tabs differ at a glance",
    )
    run.add_argument(
        "--tab-color", default=None, metavar="HEX",
        help="base tab color for this run (iTerm2, only with --notify); "
             "defaults to $PROJECT_TAB_COLOR. State colors revert to this base.",
    )
    run.add_argument(
        "--no-color", action="store_true",
        help="disable ANSI color/bold in the styled banners (also: ODIN_NO_COLOR=1 "
             "or the standard NO_COLOR). Glyphs and layout are kept; colors only.",
    )
    run.add_argument(
        "--completed-file", action="store_true",
        help="write a metadata-only COMPLETED.md mailbox into the queue dir on "
             "exit, for the paired Claude session to read (also: ODIN_COMPLETED=1; "
             "off by default)",
    )
    run.add_argument(
        "--dry-run", action="store_true",
        help="show what would run without invoking claude",
    )
    run.set_defaults(func=_cmd_run)

    st = sub.add_parser("status", help="show queue state")
    st.add_argument("queue", nargs="?", default="./queue", type=Path)
    st.add_argument(
        "--detail", "--all", "-a", action="store_true", dest="detail",
        help="on a container, also print the full task-level detail of every "
             "sub-queue (not just the summary)",
    )
    st.set_defaults(func=_cmd_status)

    rs = sub.add_parser("resume", help="resume a held task after answering questions")
    rs.add_argument("stem", help="task stem, e.g. 001-add-readme")
    rs.add_argument("queue", nargs="?", default="./queue", type=Path)
    rs.set_defaults(func=_cmd_resume)

    dm = sub.add_parser(
        "demo",
        help="scaffold the otest demo target project (a repeatable test fixture)",
        description=(
            "Write the 'otest' demo target project into DIR: a throwaway greeter "
            "CLI build whose 7-task queue exercises Odin end-to-end (carry-context, "
            "a held->resume cycle on task 005, completion). Run it with `odin run` "
            "from DIR, then re-scaffold with --force to start over."
        ),
    )
    dm.add_argument("dir", type=Path, help="directory to create the demo in")
    dm.add_argument(
        "--force", action="store_true",
        help="if DIR exists and is non-empty, wipe and recreate it (reset)",
    )
    dm.set_defaults(func=_cmd_demo)

    gd = sub.add_parser(
        "guide",
        help="print a self-contained guide to authoring Odin tasks/queues",
        description=(
            "Print a complete authoring manual to stdout: how to lay out the "
            "queue, write task files, structure a CLAUDE.md, and the protocol "
            "Odin injects. Designed so an agent in another project can run it "
            "and learn the format with no other context."
        ),
    )
    gd.add_argument(
        "topic", nargs="?", default="all",
        choices=["all", *TOPICS.keys()],
        help="all (default), or a focused topic: " + ", ".join(TOPICS),
    )
    gd.set_defaults(func=_cmd_guide)

    ar = sub.add_parser(
        "archive",
        help="move whole finished sub-queues out of the status overview",
        description=(
            "Declutter a container's `odin status` overview: move every fully "
            "finished sub-queue (no pending/running/held/failed/backlog, at least "
            "one done) as-is into <CONTAINER>/archive/<name>/. Sub-queues with "
            "work left are kept and reported. Nothing is deleted — restore one by "
            "moving it back out of archive/."
        ),
    )
    ar.add_argument("queue", nargs="?", default="./queue", type=Path)
    ar.set_defaults(func=_cmd_archive)

    mt = sub.add_parser(
        "metrics",
        help="summarise the central run/task metrics (text or --html report)",
        description=(
            "Read the central metrics log (default ~/.odin/metrics/events.jsonl, "
            "written by every `odin run` across all projects) and print an "
            "aggregate summary: run/task counts, outcomes, token usage, cost, "
            "average run/task times, peak concurrent runs, and a per-project "
            "breakdown. Pass --html to render a self-contained HTML report."
        ),
    )
    mt.add_argument(
        "--html", nargs="?", const="odin-metrics.html", default=None, metavar="PATH",
        help="render an HTML report to PATH (default: odin-metrics.html) instead "
             "of printing text",
    )
    mt.add_argument(
        "--project", default=None,
        help="only include runs/tasks whose project path contains this substring",
    )
    mt.add_argument(
        "--file", default=None, type=Path,
        help="metrics events file to read (default: ~/.odin/metrics/events.jsonl)",
    )
    mt.set_defaults(func=_cmd_metrics)

    return p


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    args.queue = _resolve_queue_arg(args.queue)
    project = (args.project or Path.cwd()).resolve()
    if not project.is_dir():
        print(f"odin: --project does not exist: {project}", file=sys.stderr)
        return 2
    if not (project / "CLAUDE.md").exists():
        print(
            f"odin: warning — {project}/CLAUDE.md not found. "
            "The agent will not know the sentinel protocol; tasks will likely fail.",
            file=sys.stderr,
        )

    # Visible-output styling: gate ANSI off when --no-color (or ODIN_NO_COLOR).
    # The NO_COLOR / ODIN_NO_COLOR env vars are honored by style.enabled() too;
    # this just folds the flag into the same module-level override.
    style.set_no_color(bool(args.no_color or os.environ.get("ODIN_NO_COLOR")))

    q = Queue(args.queue, create=False)
    if _is_container(q):
        subs = q.subqueues()
        print(
            f"odin: {q.root} holds sub-queues, not tasks directly. Point run at "
            f"one, e.g. `odin run {Path(args.queue) / subs[0]}`.",
            file=sys.stderr,
        )
        return 2
    q.ensure_dirs()  # safe to materialise now we know it's a queue, not a container
    allowed = (
        [t.strip() for t in args.allowed_tools.split(",") if t.strip()]
        if args.allowed_tools else None
    )
    disallowed = (
        [t.strip() for t in args.disallowed_tools.split(",") if t.strip()]
        if args.disallowed_tools else None
    )

    # Startup git setup: clean-tree check + select/create the one branch the
    # whole queue lands on. Skipped on --dry-run and for non-git projects.
    if args.dry_run:
        branch = args.branch
    else:
        branch, err = _setup_branch(args, project, q.root)
        if err is not None:
            return err
    # Always inject the protocol; add the branch directive when we have one.
    system_prompt = build_system_prompt(branch)

    # Central metrics: one accumulator per run, fed one record per task, with the
    # run summary written in a finally so it lands on every exit path. Off for
    # --dry-run and when metrics are disabled (env or --no-metrics).
    metrics_on = metrics.enabled() and not args.no_metrics and not args.dry_run
    acc = metrics.RunAccumulator(
        run_id=metrics.new_run_id(),
        project=project,
        queue=q.root,
        branch=branch,
        enabled=metrics_on,
    )
    signals = _resolve_signals(args)
    # The COMPLETED.md mailbox (opt-in): a metadata-only handoff written on every
    # exit path so the paired Claude session can read it. Off by default; never
    # on --dry-run. `started` is stamped here so the record spans the whole run.
    completed_on = (
        bool(args.completed_file or os.environ.get("ODIN_COMPLETED"))
        and not args.dry_run
    )
    started = completed.now()
    exit_code = 1
    try:
        exit_code = _run_loop(
            args, project, q, allowed, disallowed, system_prompt, acc, signals,
            branch=branch,
        )
    finally:
        acc.finish(exit_code)
        if completed_on:
            completed.write_record(
                q, run_id=acc.run_id, branch=branch, exit_code=exit_code,
                started=started, acc=acc,
            )
        # Restore the terminal (neutral title + clear progress bar) so a
        # finished/crashed run leaves no stale chrome. Best-effort; the tab
        # color is intentionally left as-is (held/failed flags persist).
        if not args.dry_run:
            _reset_terminal(signals, q, sys.stdout)
    return exit_code


# ----------------------------------------------------------------------
# terminal signaling (Signals config + lifecycle emissions via term.py)
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Signals:
    """Resolved terminal-signaling config for one run (see term.py).

    `title_on` gates the universally-safe OSC 0 title + OSC 9;4 progress bar;
    `notify_on` gates the iTerm2-specific attention/notification/tab-color.
    `base_color` is the project hue the user's shell hook publishes via
    $PROJECT_TAB_COLOR — state colors revert to it, never to iTerm2 `default`.
    """
    title_on: bool
    notify_on: bool
    prefix: str
    base_color: str | None


def _resolve_signals(args: argparse.Namespace) -> Signals:
    """Resolve flags + env into a Signals config (mirrors metrics.enabled())."""
    title_on = not (args.no_title or os.environ.get("ODIN_NO_TITLE"))
    notify_on = bool(args.notify or os.environ.get("ODIN_NOTIFY"))
    base_color = args.tab_color or os.environ.get("PROJECT_TAB_COLOR") or None
    return Signals(
        title_on=title_on,
        notify_on=notify_on,
        prefix=args.tab_title,
        base_color=base_color,
    )


# Title glyphs per lifecycle state.
_GLYPH_START = "⏵"
_GLYPH_DONE = "✓"
_GLYPH_HELD = "⏸"
_GLYPH_URGENT = "‼"
_GLYPH_FAILED = "✗"

# Transient iTerm2 state colors (only emitted with --notify; revert to base).
_COLOR_HELD = "e0a020"    # amber — needs input
_COLOR_FAILED = "cc3333"  # red   — failed


class _Signaler:
    """Binds the resolved Signals config + output sink + queue context and
    paints the tab through the task lifecycle via term.py.

    Every method is best-effort (term.py swallows errors and no-ops off a TTY)
    and additionally a no-op when titles are off. iTerm2 extras (tab color,
    attention, notification) fire only with --notify. The OSC 9;4 progress bar
    rides with titles (universally safe), so it answers "is the runner alive?".
    """

    def __init__(self, signals: Signals, out: TextIO, queue_name: str, total: int):
        self.s = signals
        self.out = out
        self.q = queue_name
        self.total = total

    # -- low-level guarded emitters --
    def _title(self, text: str) -> None:
        if self.s.title_on:
            term.set_title(text, out=self.out)

    def _progress(self, state: int, pct: int = 0) -> None:
        if self.s.title_on:
            term.set_progress(state, pct, out=self.out)

    def _progress_value(self, completed: int) -> None:
        """Fill the bar to completed/total, or spin (indeterminate) if unknown."""
        if self.total <= 0:
            self._progress(3)
        else:
            self._progress(1, round(100 * completed / self.total))

    def _color(self, hex_or_none: str | None) -> None:
        if self.s.notify_on:
            term.set_tab_color(hex_or_none, out=self.out)

    # -- lifecycle transitions --
    def task_start(self, completed: int) -> None:
        """`completed` = tasks done before this one; this is task #completed+1."""
        self._title(f"{self.s.prefix} {_GLYPH_START} {completed + 1}/{self.total} {self.q}")
        self._progress_value(completed)
        self._color(self.s.base_color)  # (re)assert the project hue

    def task_done(self, completed: int) -> None:
        """`completed` = tasks done including this one."""
        self._title(f"{self.s.prefix} {_GLYPH_DONE} {completed}/{self.total} {self.q}")
        self._progress_value(completed)

    def held(self) -> None:
        self._title(f"{self.s.prefix} {_GLYPH_HELD} needs input {self.q}")
        if self.s.notify_on:
            term.request_attention(out=self.out)
            term.set_tab_color(_COLOR_HELD, out=self.out)
            term.notify(f"odin: {self.q} needs input", out=self.out)

    def urgent(self) -> None:
        self._title(f"{self.s.prefix} {_GLYPH_URGENT} urgent {self.q}")
        if self.s.notify_on:
            term.request_attention(out=self.out)
            term.notify(f"odin: {self.q} urgent follow-up", out=self.out)

    def failed(self, completed: int) -> None:
        self._title(f"{self.s.prefix} {_GLYPH_FAILED} failed {self.q}")
        pct = round(100 * completed / self.total) if self.total > 0 else 0
        self._progress(2, pct)  # error state on the bar
        if self.s.notify_on:
            term.request_attention(out=self.out)
            term.set_tab_color(_COLOR_FAILED, out=self.out)
            term.notify(f"odin: {self.q} failed", out=self.out)

    def drained(self) -> None:
        self._title(f"{self.s.prefix} {_GLYPH_DONE} done {self.q}")
        if self.s.notify_on:
            term.set_tab_color(self.s.base_color, out=self.out)  # revert to base
            term.notify(f"odin: {self.q} done", out=self.out)


def _reset_terminal(signals: Signals, q: Queue, out: TextIO) -> None:
    """On process exit: clear the progress bar and emit a neutral title.

    Tab color is deliberately NOT reset here — a held/failed flag color must
    persist past exit (the shell hook does not reassert it), and a drained run
    already reverted to the base hue. The title is transient (the shell's
    precmd hook reasserts the folder name on the next prompt anyway)."""
    if not signals.title_on:
        return
    term.set_progress(0, out=out)
    term.set_title(f"{signals.prefix} {q.root.name}", out=out)


# ----------------------------------------------------------------------
# visible task banners (the styled stdout text; independent of the OSC tab
# signaling above — see style.py). All print to stdout so the user sees them;
# color is gated by style.enabled() (TTY + NO_COLOR/ODIN_NO_COLOR/--no-color).
# ----------------------------------------------------------------------

_RULE_WIDTH = 60  # target visible width of the task-start rule


def _abbrev_home(path: Path) -> str:
    """Show a path under ``~`` when it's inside the home dir, else verbatim."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _fmt_duration(ms: int) -> str:
    """Compact human duration: ``1m42s`` / ``42s`` / ``800ms``."""
    if ms >= 60_000:
        m, s = divmod(ms // 1000, 60)
        return f"{m}m{s:02d}s"
    if ms >= 1000:
        return f"{ms // 1000}s"
    return f"{ms}ms"


def _banner_start(
    completed: int, total: int, stem: str, project: Path, branch: str | None
) -> None:
    """Blank line + cyan rule header + dim metadata for task #completed+1."""
    label = f"{style.GLYPH_RULE * 2} {style.GLYPH_TASK} task {completed + 1}/{total} · {stem} "
    rule = label + style.GLYPH_RULE * max(3, _RULE_WIDTH - len(label))
    print()
    print(style.header(rule))
    meta = f"   project  {_abbrev_home(project)}"
    if branch:
        meta += f" · branch {branch}"
    print(style.dim(meta))


def _banner_done(stem: str, result: RunResult) -> None:
    """Green done footer with the run summary (omit absent fields) + blank line."""
    bits: list[str] = []
    if result.num_turns is not None:
        bits.append(f"{result.num_turns} turns")
    dur = result.wall_ms or result.duration_ms or 0
    if dur:
        bits.append(_fmt_duration(dur))
    if result.cost_usd is not None:
        bits.append(f"${result.cost_usd:.2f}")
    summary = ("   " + " · ".join(bits)) if bits else ""
    print(style.ok(f"{style.GLYPH_OK} {stem} · done{summary}"))
    print()


def _run_loop(
    args: argparse.Namespace,
    project: Path,
    q: Queue,
    allowed: list[str] | None,
    disallowed: list[str] | None,
    system_prompt: str,
    acc: metrics.RunAccumulator,
    signals: Signals,
    branch: str | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """The task-processing loop. Records each task into `acc` and returns the
    process exit code; the caller writes the run summary."""
    completed = 0
    # Planned total, computed once: tasks left + tasks already done this run.
    sig = _Signaler(signals, out, q.root.name, total=completed + len(q.pending()))

    # Which headless agent CLI drives the queue: --platform > $ODIN_PLATFORM > claude.
    platform = args.platform or os.environ.get("ODIN_PLATFORM") or "claude"
    try:
        backend = get_backend(platform)
    except ValueError as exc:
        print(f"odin: {exc}")
        return 2
    agent_bin = args.grok_bin if platform == "grok" else args.claude_bin

    while True:
        if args.max_tasks and completed >= args.max_tasks:
            print(f"odin: reached --max-tasks={args.max_tasks}, stopping.")
            sig.drained()
            _print_backlog_notice(q)
            return 0

        task = q.next_pending()
        if task is None:
            print("odin: no pending tasks. done." if completed else "odin: queue is empty.")
            sig.drained()
            _print_backlog_notice(q)
            return 0

        prompt = _build_prompt(q, task)
        _banner_start(completed, sig.total, task.stem, project, branch)

        if args.dry_run:
            print(f"[dry-run] platform={platform} bin={agent_bin} (cwd={project}, "
                  f"perm={args.permission_mode}, allowed={allowed}, "
                  f"disallowed={disallowed})")
            print(f"[dry-run] prompt ({len(prompt)} chars):")
            print(_indent(prompt[:2000]))
            return 0

        sig.task_start(completed)
        running = q.claim_running(task)
        result = run_agent(
            prompt,
            project,
            backend=backend,
            bin=agent_bin,
            permission_mode=args.permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            max_turns=args.max_turns,
            system_prompt=system_prompt,
        )
        outcome, questions, follow_ups = _route(q, running, result)
        acc.record_task(task_stem=running.stem, outcome=outcome, result=result)

        if outcome == "completed":
            completed += 1
            _banner_done(task.stem, result)
            sig.task_done(completed)
            if follow_ups:
                if any(f.urgent for f in follow_ups):
                    sig.urgent()
                halt = _handle_follow_ups(q, running.stem, follow_ups)
                if halt is not None:
                    return halt
            continue
        if outcome == "held":
            sig.held()
            # Interactive when attached to a terminal; otherwise the file +
            # `odin resume` fallback so unattended/CI runs still work.
            if sys.stdin.isatty():
                _answer_held_interactively(q, running.stem, questions)
                continue  # re-picked next loop in a fresh session
            return _print_held_instructions(q, running.stem)
        # failed
        sig.failed(completed)
        return _print_failed(q, running.stem, result)


def _build_prompt(q: Queue, task: Task) -> str:
    body = task.read()
    carry = q.latest_carry_body(task.stem)
    if not carry:
        return body
    return (
        "## Context from previous task\n\n"
        f"{carry.strip()}\n\n"
        "---\n\n"
        f"{body}"
    )


def _route(
    q: Queue, running: Task, result: RunResult
) -> tuple[str, list[Question] | None, list[FollowUp] | None]:
    """Classify a finished run, perform the queue move, and return parsed
    questions (held) or discovered follow-ups (completed) for the caller."""
    if not result.succeeded:
        q.mark_failed(running)
        return "failed", None, None

    parsed = parse(result.final_text)
    if parsed.outcome is Outcome.COMPLETED:
        q.write_carry(running.stem, unwrap_fence(parsed.body))
        q.mark_done(running)
        follow_ups = parse_follow_ups(parsed.follow_up) if parsed.follow_up else None
        return "completed", None, follow_ups
    if parsed.outcome is Outcome.HELD:
        questions = parse_questions(parsed.body)
        rendered = render_questions(questions) if questions else parsed.body
        q.mark_held(running, rendered, raw=parsed.body)
        return "held", questions, None

    # Unparseable — agent finished cleanly but did not emit the protocol.
    q.mark_failed(running)
    return "failed", None, None


# ----------------------------------------------------------------------
# git startup
# ----------------------------------------------------------------------

def _setup_branch(
    args: argparse.Namespace, project: Path, queue_root: Path
) -> tuple[str | None, int | None]:
    """Resolve the branch the queue will run on.

    Returns (branch, error_exit_code). On success error is None; on a hard stop
    (dirty tree, unresolvable branch) branch is None and error is an exit code.
    A None branch with None error means "no git management" (--no-git or the
    project isn't a git repo) — the queue still runs, just without a branch
    directive.
    """
    if args.no_git:
        return None, None
    if not git.is_repo(project):
        print(
            f"odin: warning — {project} is not a git repo; skipping branch "
            "management. Pass --no-git to silence this.",
            file=sys.stderr,
        )
        return None, None

    clean, dirty = git.is_clean(project, ignore_within=queue_root)
    if not clean:
        print("odin: refusing to start — the working tree is not clean:", file=sys.stderr)
        print(_indent(dirty), file=sys.stderr)
        print("Commit or stash your changes first.", file=sys.stderr)
        return None, 2

    try:
        branch = _resolve_branch(args, project)
    except git.GitError as e:
        print(f"odin: {e}", file=sys.stderr)
        return None, 2
    _warn_claude_md_conflicts(project)
    return branch, None


def _warn_claude_md_conflicts(project: Path) -> None:
    """Soft-warn when the target CLAUDE.md mandates a git workflow that fights
    Odin's one-branch/no-PR model. Advisory only — never blocks the run."""
    claude_md = project / "CLAUDE.md"
    if not claude_md.exists():
        return
    reasons = scan_claude_md(claude_md.read_text(encoding="utf-8", errors="replace"))
    if not reasons:
        return
    print(
        f"odin: note — {claude_md} {', '.join(reasons)}. Odin runs the whole "
        "queue on one branch with no pull requests, and the injected protocol "
        "overrides contrary git instructions. Review if that's unexpected.",
        file=sys.stderr,
    )


def _resolve_branch(args: argparse.Namespace, project: Path) -> str:
    current = git.current_branch(project)
    if args.branch:
        name = args.branch
        if git.branch_exists(project, name):
            if name != current:
                git.checkout(project, name)
            print(f"odin: working on branch '{name}'.")
        else:
            git.create_and_checkout(project, name, args.base or current or None)
            print(f"odin: created and checked out branch '{name}'.")
        return name

    if sys.stdin.isatty():
        return _apply_branch_plan(project, ask_branch_choice(current), current)

    # Non-interactive with no --branch: run on the current branch (dev mode).
    print(f"odin: working on current branch '{current or '(detached HEAD)'}'.")
    return current


def _apply_branch_plan(project: Path, plan: BranchPlan, current: str) -> str:
    if plan.create:
        git.create_and_checkout(project, plan.name, plan.base or current or None)
        print(f"odin: created and checked out branch '{plan.name}'.")
        return plan.name
    if plan.name and plan.name != current:
        git.checkout(project, plan.name)
        print(f"odin: switched to branch '{plan.name}'.")
    else:
        print(f"odin: working on current branch '{plan.name or current}'.")
    return plan.name or current


# ----------------------------------------------------------------------
# interactive held handling
# ----------------------------------------------------------------------

def _answer_held_interactively(q: Queue, stem: str, questions: list[Question] | None) -> None:
    print()
    print(style.warn(f"{style.GLYPH_HELD} {stem} · needs input"))
    if questions:
        answers = ask_questions(questions)
    else:
        answers = _ask_freeform(q, stem)
    q.record_answers(stem, answers)
    q.resume_held(stem)
    print(style.dim(f"   {stem} answered, re-queued; continuing"))


def _ask_freeform(q: Queue, stem: str) -> str:
    """Fallback when the agent emitted plain-text (non-JSON) questions."""
    print(q.held_questions_path(stem).read_text(encoding="utf-8"))
    print("Type your answer; finish with an empty line:")
    lines: list[str] = []
    for line in sys.stdin:
        if line.strip() == "":
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines) or "(no answer provided)"


# ----------------------------------------------------------------------
# discovered follow-up work
# ----------------------------------------------------------------------

def _handle_follow_ups(q: Queue, done_stem: str, follow_ups: list[FollowUp]) -> int | None:
    """Record follow-ups discovered by a completed task.

    Non-urgent → backlog (summarised at end of run). Urgent → inserted to run
    next, with the user asked to continue or stop. Returns an exit code to halt
    the run, or None to continue.
    """
    backlog = [f for f in follow_ups if not f.urgent]
    urgent = [f for f in follow_ups if f.urgent]

    for f in backlog:
        t = q.add_backlog(f.title, f.body)
        print(f"odin: recorded backlog task -> backlog/{t.name}")

    if not urgent:
        return None

    inserted = [q.insert_pending_after(done_stem, f.title, f.body) for f in urgent]
    print()
    print(style.paint(
        f"{style.GLYPH_URGENT} {done_stem} · urgent follow-up — inserted to run NEXT",
        style.BOLD, style.YELLOW,
    ))
    for t in inserted:
        print(style.dim(f"   ! pending/{t.name}"))

    if sys.stdin.isatty():
        if ask_continue():
            print("odin: continuing.")
            return None
        print(
            "odin: stopping at your request. The inserted task(s) remain in "
            "pending/ — run `odin run` to continue."
        )
        return 11

    # Unattended: there's nobody to ask — halt for review.
    print(
        "odin: not a TTY — halting for review (exit 11). The inserted task(s) "
        "remain in pending/; run `odin run` to continue.",
        file=sys.stderr,
    )
    return 11


def _print_backlog_notice(q: Queue) -> None:
    items = q.backlog()
    if not items:
        return
    print()
    print(f"odin: NOTE — {len(items)} item(s) in {q.root / 'backlog'} need attention:")
    for t in items:
        print(f"  - backlog/{t.name}")
    print("Promote one by moving it into pending/, then run `odin run`.")


def _print_held_instructions(q: Queue, stem: str) -> int:
    qpath = q.held_questions_path(stem)
    print()
    print(style.warn(f"{style.GLYPH_HELD} {stem} · needs input (agent requested input)"))
    print(f"Questions written to: {qpath}")
    print("Next steps:")
    print(f"  1. Open {qpath} and fill in the '## Answers' section.")
    print(f"  2. Run: odin resume {stem}")
    print("     (then `odin run` to continue the queue)")
    return 10  # distinct non-zero exit so CI/loops can distinguish


def _print_failed(q: Queue, stem: str, result: RunResult) -> int:
    print()
    print(style.err(
        f"{style.GLYPH_FAIL} {stem} · FAILED · exit {result.exit_code} "
        f"· stop_reason={result.stop_reason}"
    ))
    print(f"  exit_code:   {result.exit_code}")
    print(f"  stop_reason: {result.stop_reason}")
    if result.error:
        print(f"  error:       {result.error}")
    print(f"  task file:   {q.root / 'failed' / f'{stem}.md'}")
    if result.error == "error_max_turns":
        print("This task hit the --max-turns cap mid-work (not a crash). Re-run "
              "without --max-turns (the default is now unlimited) or with a "
              "higher value.")
    print("Inspect the agent's final output above. To retry, move the file back "
          "from failed/ to pending/ and run `odin run` again.")
    return 1


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------

# Per-state next-action hint (keyed by state name). Built per-task.
_STATE_HINT = {
    "held":    lambda t: f"odin resume {t.stem}",
    "backlog": lambda t: "promote: move to pending/",
    "failed":  lambda t: "retry: move to pending/",
}


def _cmd_status(args: argparse.Namespace) -> int:
    args.queue = _resolve_queue_arg(args.queue)
    q = Queue(args.queue, create=False)  # read-only: never create dirs
    if _is_container(q):
        _print_container_overview(q, args.queue, detail=args.detail)
        return 0
    _print_queue_detail(q)
    return 0


def _summarize_counts(counts: dict[str, int]) -> tuple[str, list[str]]:
    """Return (summary, attention_flags) for one queue's counts.

    Summary leads with progress as done/total so you can tell 3-of-3 from
    3-of-10. `total` is the planned lifecycle (pending+running+held+done+failed);
    backlog is discovered-later work, shown separately as '+N backlog'.
    """
    total = sum(counts[s] for s in ("pending", "running", "held", "done", "failed"))
    if total == 0 and counts["backlog"] == 0:
        return "(empty)", []
    parts = [f"{counts['done']}/{total} done"]
    for state in ("pending", "running", "held", "failed"):
        if counts[state]:
            parts.append(f"{counts[state]} {state}")
    if counts["backlog"]:
        parts.append(f"+{counts['backlog']} backlog")
    flags = []
    if counts["held"]:
        flags.append("needs input")
    if counts["failed"]:
        flags.append("has failures")
    return ", ".join(parts), flags


def _print_container_overview(q: Queue, queue_arg: Path, *, detail: bool) -> None:
    """One-line progress summary per sub-queue, most-recently-active first. With
    detail=True, also print the full task-level view of each."""
    # Newest-first: the queue you last touched sorts to the top — most likely
    # the one to keep working.
    subs = sorted(
        q.subqueues(),
        key=lambda name: Queue(q.root / name, create=False).last_activity(),
        reverse=True,
    )
    print(f"queue overview: {q.root}  ({len(subs)} sub-queue(s))")
    print()
    width = max(len(s) for s in subs)
    for name in subs:
        summary, flags = _summarize_counts(Queue(q.root / name, create=False).counts())
        tail = f"   ({'; '.join(flags)})" if flags else ""
        print(f"  {name.ljust(width)}   {summary}{tail}")
    print()
    print("Listed newest first — the top sub-queue is the most recently active.")
    archived = archived_subqueues(q.root)
    if archived:
        print(f"{len(archived)} archived in {q.root / 'archive'}/ "
              "(move one back out of archive/ to restore it).")
    if not detail:
        print(f"Detail for one: odin status {Path(queue_arg) / '<name>'}   "
              "(or --detail for all)")
        return
    for name in subs:
        print("─" * 60)
        _print_queue_detail(Queue(q.root / name, create=False))


def _print_queue_detail(q: Queue) -> None:
    """Task-level listing for a single queue: counts, names, ages, hints."""
    print(f"queue: {q.root}")
    sections = (
        ("pending", q.pending()),
        ("running", q.running()),
        ("held",    q.held()),
        ("done",    q.done()),
        ("failed",  q.failed()),
        ("backlog", q.backlog()),
    )
    for name, tasks in sections:
        print(f"  {name:8s} ({len(tasks)})")
        hint = _STATE_HINT.get(name)
        for t in tasks:
            line = f"    - {t.name}  ({_age(t.path)})"
            if hint:
                line += f"  → {hint(t)}"
            print(line)


def _age(path: Path) -> str:
    """Compact 'time since last modified' for a queue file (e.g. '2h ago')."""
    try:
        secs = max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        return "?"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit} ago"
    return f"{secs}s ago"


# ----------------------------------------------------------------------
# archive
# ----------------------------------------------------------------------

def _cmd_archive(args: argparse.Namespace) -> int:
    args.queue = _resolve_queue_arg(args.queue)
    container = Queue(args.queue, create=False)
    subs = container.subqueues()
    if not subs:
        print(
            f"odin: {container.root} holds no sub-queues to archive. "
            "Archiving moves whole finished sub-queues of a container out of the "
            "`odin status` overview into <container>/archive/.",
            file=sys.stderr,
        )
        return 0

    archived, skipped = archive_finished_subqueues(container.root)
    if not archived:
        print("odin: nothing to archive — no fully finished sub-queues.")
        for name, reason in skipped:
            print(f"  kept {name}  ({reason})")
        return 0

    dest = container.root / "archive"
    print(f"odin: archived {len(archived)} sub-queue(s) -> {dest}/")
    for name, archived_as in archived:
        rename = f"  (as {archived_as})" if archived_as != name else ""
        print(f"  - {name}{rename}")
    if skipped:
        print("Kept (still have work):")
        for name, reason in skipped:
            print(f"  - {name}  ({reason})")
    return 0


# ----------------------------------------------------------------------
# metrics
# ----------------------------------------------------------------------

def _cmd_metrics(args: argparse.Namespace) -> int:
    path = args.file.expanduser() if args.file else metrics.events_path()
    events = metrics.read_events(path)
    agg = metrics.aggregate(events, project_filter=args.project, path=path)
    if args.html is not None:
        out = Path(args.html).expanduser()
        try:
            out.write_text(metrics.render_html(agg), encoding="utf-8")
        except OSError as e:
            print(f"odin: could not write {out}: {e}", file=sys.stderr)
            return 2
        print(f"odin: wrote HTML report -> {out}")
        return 0
    print(metrics.render_text(agg), end="")
    return 0


# ----------------------------------------------------------------------
# resume
# ----------------------------------------------------------------------

def _cmd_resume(args: argparse.Namespace) -> int:
    args.queue = _resolve_queue_arg(args.queue)
    q = Queue(args.queue)
    try:
        moved = q.resume_held(args.stem)
    except FileNotFoundError as e:
        print(f"odin: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"odin: {e}", file=sys.stderr)
        return 2
    print(f"odin: {args.stem} moved back to pending/ ({moved.path}).")
    print("Run `odin run` to continue the queue.")
    return 0


# ----------------------------------------------------------------------
# demo
# ----------------------------------------------------------------------

def _cmd_demo(args: argparse.Namespace) -> int:
    dest = args.dir.resolve()
    try:
        written = create_demo(dest, force=args.force)
    except DemoExists as e:
        print(f"odin: {e}", file=sys.stderr)
        return 2
    except DemoError as e:
        print(f"odin: {e}", file=sys.stderr)
        return 2

    print(f"odin: wrote demo project to {dest} ({len(written)} files, 7 queued tasks).")
    print("Next steps:")
    print(f"  cd {dest}")
    print("  odin run --no-git          # build greeter; task 005 will ask you a question")
    print(f"  odin demo {dest} --force   # reset and start over anytime")
    return 0


# ----------------------------------------------------------------------
# guide
# ----------------------------------------------------------------------

def _cmd_guide(args: argparse.Namespace) -> int:
    print(render_guide(args.topic), end="")
    return 0


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _resolve_queue_arg(arg: Path) -> Path:
    """Let a bare queue name be found inside ./queue/.

    Tasks normally live in a named sub-queue (queue/<name>/), so `odin run
    add-search` should work without the `queue/` prefix. Resolution order:
      1. If `arg` exists as given (relative or absolute path), use it.
      2. Else if `queue/<arg>` exists, use that — the bare-name shortcut.
      3. Else use `arg` unchanged, so the normal not-found handling applies.
    An existing local dir (step 1) wins over the queue/ shortcut, so an explicit
    path is never overridden.
    """
    if arg.exists():
        return arg
    nested = Path("queue") / arg
    if nested.exists():
        return nested
    return arg


def _is_container(q: Queue) -> bool:
    """True if `q` is a directory of named sub-queues rather than a queue with
    its own tasks — so commands can redirect instead of acting on an empty
    (or fabricated) queue."""
    return bool(q.subqueues()) and q.is_empty()


def _indent(text: str, prefix: str = "  | ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
