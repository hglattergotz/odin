# Odin — Headless Claude Code Task Orchestrator

## Purpose

Odin is a minimal CLI that runs a queue of tasks through Claude Code in
headless mode (`claude -p`). It feeds tasks one at a time into a target
project's Claude Code session, parses the structured output, carries
context forward between tasks, and halts cleanly when the agent needs
human input.

Orchestration is intentionally dumb. Odin does not dictate workflow
(clean-tree checks, branching, commits, tests). All of that lives in the
**target project's** `CLAUDE.md`. Odin's job is to invoke, observe, route,
and carry context. A well-formed target project's `CLAUDE.md` is the
reference; `examples/target-claude-md-snippet.md` shows the sentinel contract.

## Language and layout

Python 3.11+, **uv-managed**. Zero runtime dependencies (stdlib only).
`pytest` is the sole dev dep. Never invoke the system Python — always
go through `uv run`, `uvx`, or the project's `.venv`.

```
odin/
├── CLAUDE.md
├── README.md            # user-facing usage (created during build)
├── pyproject.toml
├── src/odin/
│   ├── __init__.py
│   ├── cli.py           # argparse + entry point
│   ├── queue.py         # filesystem queue model
│   ├── runner.py        # `claude -p` invocation + JSON parsing
│   ├── protocol.py      # sentinel markers + JSON question parsing
│   ├── contract.py      # the protocol Odin injects via system prompt
│   ├── git.py           # startup-only git wrapper (clean check + branch)
│   ├── prompts.py       # interactive terminal Q&A + branch selection
│   ├── demo.py          # `odin demo` scaffolder (test fixture)
│   ├── _demo_files.py   # generated: embedded otest fixture content
│   ├── guide.py         # `odin guide` authoring manual (self-discovery)
│   ├── lint.py          # startup CLAUDE.md git-conflict warnings (advisory)
│   ├── metrics.py       # central JSONL run/task metrics + report renderers
│   ├── term.py          # best-effort OSC terminal signaling (title/color/notify)
│   └── completed.py     # COMPLETED.md mailbox (Odin->Claude handoff)
├── examples/
│   ├── queue/
│   │   ├── pending/
│   │   └── done/
│   └── target-claude-md-snippet.md   # the bit a target project must add
└── tests/
```

## CLI surface

```
odin run    [QUEUE_DIR] [--project PATH] [--max-tasks N] [--allowed-tools LIST] [--disallowed-tools LIST] [--permission-mode MODE] [--branch NAME] [--base NAME] [--no-git] [--no-metrics] [--no-title] [--notify] [--tab-title PREFIX] [--tab-color HEX] [--no-color] [--completed-file] [--dry-run]
odin status [QUEUE_DIR]
odin resume HELD_TASK [QUEUE_DIR]
odin demo   DIR [--force]
odin guide  [TOPIC]
odin archive [QUEUE_DIR]
odin metrics [--html [PATH]] [--project SUBSTR] [--file PATH]
```

`odin archive` operates on a **container** of named sub-queues: it moves every
*fully finished* sub-queue (nothing in pending/running/held/failed/backlog and
≥1 done) as-is into `<CONTAINER>/archive/<name>/`, decluttering the `odin
status` overview. Sub-queues with work left are kept and reported with a reason;
a name clash gets a `-2`/`-3` suffix. Pure whole-dir move, never delete —
restore by moving a dir back out of `archive/`. `odin status` on a container
lists sub-queues **most-recently-active first** (top = the queue you last
worked) with a footer stating the ordering and the archived count; on a single
queue it lists each state with file ages and a next-action hint (held→resume,
backlog→promote, failed→retry). Logic in `queue.archive_finished_subqueues` /
`archive_state` / `last_activity` / `archived_subqueues`.

`odin guide` prints a self-contained authoring manual to stdout (queue layout,
task-file format, CLAUDE.md workflow, the injected protocol, the run flow) so
an agent in another project can self-discover the format with no other context.
`TOPIC` ∈ {all (default), tasks, claude-md, protocol, terminal}. The `terminal`
topic is an agent-executable iTerm2 setup manual (install, per-project tab-color
shell hook, `--notify` alerts, verify) — point an agent at it to configure a
terminal for Odin's tab signaling. The protocol section is generated from
`contract.build_system_prompt`, so it can't drift from runtime.
Content lives in `guide.py`.

`odin demo` scaffolds the `otest` throwaway target project (a `greeter` CLI
build with a 7-task queue, including a held→resume cycle on task 005) into
`DIR` — a repeatable end-to-end test fixture. `--force` wipes and recreates an
existing demo dir (refuses if it looks like a real repo). The fixture content
is embedded in `_demo_files.py` (generated from a pristine `../otest`); the
scaffolding logic is in `demo.py`.

`odin metrics` reads the central metrics log and prints an aggregate summary
(run/task counts, outcomes, token usage, cost, average run/task times, peak
concurrent runs, per-project breakdown). `--html [PATH]` renders a
self-contained HTML report instead of text (default `odin-metrics.html`);
`--project SUBSTR` filters by project path substring; `--file` overrides the
log path. Content/logic lives in `metrics.py`.

Defaults:
- `QUEUE_DIR` = `./queue`. A bare name (no path separator that already exists)
  resolves under `./queue/`: `odin run add-search` → `queue/add-search` when it
  exists, so the `queue/` prefix is optional. An existing path as-given always
  wins; nonexistent falls through unchanged. Shared by run/status/resume/archive
  via `cli._resolve_queue_arg`.
- `--project` = current working directory
- `--permission-mode` = `bypassPermissions` — full autonomy by default (the
  agent runs all tools, incl. Bash, ungated). A headless agent that must stop
  for per-command approval can't work and thrashes. The safety net is the
  startup model (clean-tree refusal + single-branch isolation), not per-command
  prompts. Restrict explicitly with `--permission-mode acceptEdits`/`default`,
  `--allowed-tools` (allowlist), or `--disallowed-tools` (denylist carve-outs).
- `--branch` / `--base` = unset → branch is chosen interactively at startup on
  a TTY, or defaults to the current branch when non-interactive.
- `--no-git` = skip all git startup (clean-tree check + branch selection); use
  for non-git projects.
- `--no-metrics` = don't record metrics for this run (metrics are on by
  default; `ODIN_NO_METRICS=1` disables them globally).

## Metrics

Every `odin run` appends two record types — `task` (one per execution) and
`run` (one summary per invocation), linked by a `run_id` — to a single central
JSONL log shared across all projects: `$ODIN_HOME/metrics/events.jsonl`
(default `~/.odin/metrics/events.jsonl`; `$ODIN_METRICS_FILE` overrides the
file). This is the *one* thing Odin writes outside the queue/target project.

Rules (see `metrics.py`):
- **JSONL, append-only.** One record per line so a torn trailing line (crash
  mid-write) is skippable and `jq`/`duckdb`/pandas read it natively.
- **Best-effort.** Every write is wrapped and swallowed — telemetry must never
  sink a run (same posture as `runner._safe_write`).
- **Metadata only.** Never task bodies or agent output (they can carry
  secrets); per the supply-chain rules, no secret-carrying values are logged.
- **Cross-process safe.** Appends take an advisory `fcntl.flock` so concurrent
  Odin processes (one per project) don't tear each other's lines.
- **Run summary on every exit path.** The `RunAccumulator` is fed one
  `record_task` per task and `finish(exit_code)` is called from a `finally` in
  `cli._cmd_run`, so the `run` record lands whether the queue drains, fails, or
  holds. Zero-task runs (empty queue, setup error) and `--dry-run` write
  nothing.

The token/cost/duration/turn fields come straight off the terminal `result`
stream-json event in `runner.py` (`usage`, `total_cost_usd`, `duration_ms`,
`duration_api_ms`, `num_turns`) plus Odin's own wall-clock timing; they live on
`RunResult`. `odin metrics` aggregates and renders (text or `--html`).

## Terminal signaling

`odin run` paints its own terminal tab with live status so a housekeeping tab is
readable at a glance, and can leave a completion record the paired interactive
Claude session reads on your next prompt. Three layers, all opt-out/opt-in so
they're safe everywhere:

- **Tab title + progress bar** (on by default; `--no-title` / `ODIN_NO_TITLE=1`
  to suppress). OSC 0 title (`<prefix> <glyph> <n>/<total> <queue>`) and an
  OSC 9;4 progress bar that fills as the queue drains. Both are universally
  safe — terminals that don't support them ignore them. `--tab-title PREFIX`
  (default `odin`) sets the leading token so two projects' tabs differ.
- **Attention + notification + tab color** (opt-in; `--notify` /
  `ODIN_NOTIFY=1`). iTerm2-specific: dock bounce + OSC 9 notification on
  held/failed/urgent/done, and a state tab color. `--tab-color HEX` (default
  `$PROJECT_TAB_COLOR`) sets the base hue; state colors revert to that base on
  success/drain and *leave* the amber/red flag on held/failed until you act.
  Odin never resets the color to iTerm2 `default` when a base is set — the
  user's shell hook owns the per-project hue and publishes it via env.
- **`COMPLETED.md` mailbox** (opt-in; `--completed-file` / `ODIN_COMPLETED=1`).
  A metadata-only record written into the queue dir on every exit path
  (drain/fail/hold/max-tasks; skipped on `--dry-run`). Pairing is by directory,
  not PID — the project's Claude runs in that cwd, so projects can't cross
  wires.

Posture (non-negotiable, same as metrics): **stdlib only** (escapes are plain
byte writes), **best-effort** (every emission wrapped like `runner._safe_write`
— signaling never sinks a run), **metadata only** (queue name + index + state +
counts; never task bodies, carry-context, or agent output), **TTY-gated** (no
escape junk in pipes/logs/CI), and **Odin emits — never the `claude -p` child**
(only Odin's own stdout is the user's TTY). Escape helpers live in `term.py`;
the mailbox renderer/writer in `completed.py`; both are wired into `_run_loop` /
the `_cmd_run` `finally` in `cli.py`. End-user setup is in
[`docs/iterm2-setup-guide.md`](docs/iterm2-setup-guide.md).

## Output styling

Independent of (and orthogonal to) the OSC tab signaling above, Odin styles its
**visible stdout** for scannability: each task is framed by a colored rule
header (`━━ ⏵ task N/total · <stem> ━…`) and a `✓`/`✗`/`⏸`/`‼` footer; the
streamed agent events get a `⏺` bullet per text block with tool calls indented
and paths shown relative to the project. The ANSI helpers live in `style.py`
(same posture as `term.py`: stdlib-only, best-effort, TTY-gated). Color is
emitted only when `out.isatty()` and none of `--no-color`, `NO_COLOR`, or
`ODIN_NO_COLOR` is set; when off, the glyphs, indentation, and blank lines
remain so the layout still reads. `--no-color` sets a process-global override in
`style` from `_cmd_run`. This layer never touches `protocol.parse` or the
`_Signaler`/`term.py` signaling.

## Queue layout

```
queue/
├── pending/    # NNN-slug.md       — waiting, picked in lexicographic order
├── running/    # the in-flight file lives here briefly
├── done/       # completed successfully
├── failed/     # non-zero exit, stop_reason != end_turn, or unparseable output
├── held/       # blocked on questions; resume with `odin resume`
├── carry/      # NNN-slug.next-context.md — emitted by the prior task
└── backlog/    # non-urgent follow-up tasks an agent discovered mid-run
```

A **container** (a dir of named sub-queues, not a queue itself) additionally
grows `archive/<name>/` — whole finished sub-queues `odin archive` moved out of
the `odin status` overview.

Convention: `001-slug.md`, `002-slug.md`, etc. The numeric prefix is the
only ordering signal; Odin does not parse the body.

## Sentinel protocol

Every task must terminate with exactly one of two fenced blocks:

- `<<<NEXT_CONTEXT>>> … <<<END>>>` — task complete; body is the
  carry-forward prompt for the next task (matches the "next session
  prompt" block a well-formed target CLAUDE.md describes).
- `<<<NEEDS_INPUT>>> … <<<END>>>` — blocked on questions; the body is a
  JSON object (`{"questions": [...]}`) Odin renders for the user. Nothing
  is committed when this is emitted.

Odin **injects this protocol itself** via `claude --append-system-prompt`
(see `contract.py`), so tasks emit parseable output even if a target
project forgot the snippet. This is the *one* exception to "Odin
contributes no rules": it injects the **protocol only** (sentinel +
question schema + the single-branch directive), never workflow. The
question JSON schema (problem → question → options → optional
recommendation + why, all brief) lives in `contract.py`.

Odin scans the final assistant `result` for these markers. Anything else
routes to `failed/` for human inspection — silence is treated as failure
on purpose.

A completed task MAY additionally emit a `<<<FOLLOW_UP>>>` block (JSON list
of `{title, body, urgent}`) recording newly-discovered work. Non-urgent
items are filed in `backlog/` and called out when the queue drains (exit 0);
`urgent` items are inserted into `pending/` to run next, with the user asked
to continue or stop (unattended → halt, exit 11). See `protocol.parse` /
`parse_follow_ups` and `_handle_follow_ups` in `cli.py`.

## Carry-forward context

When task N emits `<<<NEXT_CONTEXT>>>`, the body is written to
`queue/carry/NNN-slug.next-context.md` and **prepended** to task N+1's
prompt under a `## Context from previous task` heading before invocation.
The agent for task N+1 sees that block first, then the task body.

## Resume flow (the "interactive questions" answer)

The contract tells the agent never to make substantive assumptions, and
to emit `<<<NEEDS_INPUT>>>` (as question JSON) instead of guessing. When
that fires there are two paths, chosen by whether stdin is a TTY:

**Interactive (TTY).** Odin parses the question JSON, renders each
question in the terminal (problem, options, the recommendation + why),
reads the user's choices (empty = take the recommendation), records them
into the held questions file's `## Answers` section, and immediately
re-queues the task — the next loop iteration picks it up in a **fresh**
Claude session with the Q+A prepended. No file editing, no second command.

**File fallback (no TTY — CI/unattended).** Same as before:

1. Odin writes `queue/held/NNN-slug.questions.md` (rendered questions +
   the raw JSON block for audit) with a blank `## Answers` heading, and
   exits `10`.
2. The terminal prints the next command: fill in `## Answers`, then
   `odin resume NNN-slug`.
3. `odin resume` validates `## Answers` is non-empty, prepends the paired
   Q+A to the original task body, and moves it back to `pending/`.

Both paths converge on the same `resume_held()` merge, and both re-run in
a **fresh** session — the prior session is never resumed via `--resume`,
because fresh-context-per-task is the whole point.

## What lives where

- **This `CLAUDE.md`** — rules for working on Odin itself.
- **Target project `CLAUDE.md`** — workflow rules + sentinel emission.
  See `examples/target-claude-md-snippet.md` for a focused snippet
  showing just the sentinel contract.
- **Per-task `.md` files** — the body of the prompt. No frontmatter.

## Non-goals

- No UI. CLI only.
- No long-running server. One-shot `odin run`.
- No retry beyond what the target CLAUDE.md describes. Failed tasks stay
  failed until the user moves them back to `pending/`.
- No parallelism. Tasks are strictly sequential.
- **Git is startup-only.** Odin verifies a clean tree and selects/creates
  the one branch the whole queue lands on, then checks it out — once,
  before the loop. It never commits, pushes, merges, or opens PRs;
  per-task commits stay the target CLAUDE.md's job. (This narrows the
  original "no git operations" non-goal — approved deliberately. `--no-git`
  restores the zero-git behaviour for non-git projects.)

## Install and invocation model

Odin is installed once and invoked from inside any target project:

```
# install once
uv tool install --from /path/to/odin odin

# from inside any project
cd ~/code/myproject
odin run                       # uses ./queue, --project=$PWD
```

The `claude -p` subprocess runs with `cwd` set to `--project` (default
`$PWD`), so it picks up the **target project's** `CLAUDE.md`, not this one.
The only rules Odin contributes are the **protocol** (sentinel + question
schema + single-branch directive), injected via `--append-system-prompt`;
all workflow rules still come from the target `CLAUDE.md`.

## Supply chain rules

These rules are durable. Treat any change to them as a substantive decision
that needs explicit user approval.

- **Zero runtime dependencies.** Stdlib only on the hot path. Adding one
  later requires the rationale in the PR description.
- **14-day minimum package age.** No dependency version published in the
  last 14 days may be added or upgraded. Enforced two ways:
  - `pyproject.toml` pins exact versions.
  - `uv.lock` is generated with `uv lock --exclude-newer <today-14d>` and
    committed. CI installs with `uv sync --frozen --locked`.
- **No system Python.** Always `uv run`, `uvx`, or activate `.venv`.
  Never `pip install` globally. Never `python3 …` against a system
  interpreter.
- **No build-script execution from untrusted packages.** The `[tool.uv]`
  config sets `no-build-isolation-package = []` by default; do not add
  arbitrary packages to it. Avoid sdist-only dependencies — wheels only.
- **No `git+`, `file:`, or unscoped tarball deps.** PyPI sources only.
- **Never commit secrets.** No `.env` in the repo; no `echo $TOKEN` in
  scripts; no logging of secret-carrying values at any level.
- **Pinned dev tools too.** `pytest` and any future dev dep are pinned
  exact versions in `[dependency-groups]` dev and locked the same way.

## Implementation rules

- Stdlib only at runtime. `claude` must be on `PATH`; Odin shells out.
- `claude -p` invocation flags (baseline):
  `--output-format stream-json --verbose --permission-mode bypassPermissions
  --append-system-prompt <protocol>` plus any `--allowed-tools` /
  `--disallowed-tools` the user passed. **No `--max-turns` by default** — an
  arbitrary turn cap can kill a healthy in-progress session (it isn't imposed
  on an interactive run either); only pass `--max-turns` when the user sets it
  as a circuit-breaker. Never `--resume` — every task is a fresh session.
- **Drain stderr concurrently.** The runner reads the subprocess's stderr on a
  background thread, not after `wait()`. Reading only stdout while the child
  fills its ~64KB stderr pipe deadlocks the session (the agent then perceives
  delayed tool output and spam-probes with `echo`). Don't regress this.
- Set the subprocess `cwd` to `--project` so the target CLAUDE.md loads.
- **Git startup** (unless `--no-git` or non-git project): refuse to start
  on a dirty tree (the queue dir is excluded from the check); resolve the
  branch from `--branch`/`--base`, else the interactive prompt on a TTY,
  else the current branch. Odin only ever runs `status`, `switch`,
  `show-ref`, `rev-parse`, `symbolic-ref` — never anything that writes
  history. Per-task commits are the agent's job (per the target CLAUDE.md).
- **Conflict safeguards.** The injected contract states it takes precedence
  over the target CLAUDE.md for task-termination and git/branch/PR policy.
  When git is managed, `lint.scan_claude_md` warns (never blocks) if the
  target CLAUDE.md mandates a conflicting workflow (PRs, branch-per-task,
  push, no-commit). `odin guide claude-md` emits a pasteable "This project
  is run by Odin" marker block for target projects.
- Stream stdout to the user's terminal live so they see progress; capture
  the final JSON for parsing. `--output-format stream-json` is the
  reliable way to get both.
- Failure signals: non-zero exit, JSON `.error` set, or
  `stop_reason != end_turn`. Any → `failed/`.
- Never delete a queue file. Only move between subdirs. The audit trail
  matters more than tidy directories.
- Never assume the target project's git state — that's the target
  CLAUDE.md's job. Odin reports what came back, nothing more.
