# Odin — Headless Multi-Platform Task Orchestrator

## Purpose

Odin is a minimal CLI that runs a queue of tasks through a headless agent
CLI: **Claude Code** (`claude`), **Cursor CLI** (`agent`), or **Grok Build**
(`grok`). You select the product with `--platform` (or `$ODIN_PLATFORM` /
`default_platform` in config). There is no built-in default. Tasks run one
at a time. Odin parses the structured output, carries context forward
between tasks, and halts cleanly when the agent needs human input.

Orchestration is intentionally dumb. Odin does not dictate workflow
(clean-tree checks, branching, commits, tests). All of that lives in the
**target project's** instruction file (`CLAUDE.md` for Claude Code, `AGENTS.md`
/ `.cursor/rules` for Cursor CLI). Odin's job is to invoke, observe, route, and
carry context. Snippets: `examples/target-claude-md-snippet.md` and
`examples/target-agents-md-snippet.md`.

## Multi-platform architecture

The task loop (queue, carry-context, held/resume, follow-ups, git startup,
terminal signaling, metrics shape) is platform-agnostic. Platform-specific
pieces live behind an `AgentBackend` in `src/odin/backends/`. Prefer **public
product names** in docs; the short `--platform` key matches the binary:

| Public product | `--platform` | Binary | Class |
|----------------|--------------|--------|-------|
| Claude Code | `claude` | `claude` | `ClaudeBackend` |
| Cursor CLI | `cursor` | `agent` | `CursorBackend` |
| Grok Build | `grok` | `grok` | `GrokBackend` |

`odin platforms` renders this table live (plus binary/model/config-key detail)
from the registry — prefer pointing a user there over restating it. See
`docs/agent-backends.md` for invoke details. Design notes:
`docs/multi-platform-agents-proposal.md`.

`--platform` / `$ODIN_PLATFORM` / `default_platform` in config selects the
backend; if all are unset, `odin run` errors. Unknown names are a hard error.
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
│   ├── runner.py        # generic agent subprocess loop + NDJSON parsing
│   ├── backends/        # AgentBackend peers: claude, cursor, grok + registry
│   ├── config.py        # ~/.odin/config.toml load/save + resolution
│   ├── protocol.py      # sentinel markers + JSON question parsing
│   ├── contract.py      # the protocol Odin injects via system prompt
│   ├── git.py           # git wrapper: clean check + branch, and commit_wip
│   ├── prompts.py       # interactive terminal Q&A + branch selection
│   ├── guide.py         # `odin guide` authoring manual (self-discovery)
│   ├── platforms.py     # `odin platforms` report: what's supported + resolved
│   ├── lint.py          # startup instruction-file git-conflict warnings
│   ├── metrics.py       # central JSONL run/task metrics + report renderers
│   ├── recovery.py      # interruption sidecar + resumption brief
│   ├── wait.py          # sleeping until a provider limit window reopens
│   ├── term.py          # best-effort OSC terminal signaling (title/color/notify)
│   └── completed.py     # COMPLETED.md mailbox (Odin→agent handoff)
├── examples/
│   ├── queue/
│   │   ├── pending/
│   │   └── done/
│   ├── target-claude-md-snippet.md   # Claude target workflow snippet
│   └── target-agents-md-snippet.md   # Cursor target workflow snippet
└── tests/
```

## CLI surface

```
odin run    [QUEUE_DIR] [--project PATH] [--platform NAME] [--model MODEL]
            [--agent-bin PATH] [--claude-bin PATH] [--max-tasks N]
            [--allowed-tools LIST] [--disallowed-tools LIST] [--permission-mode MODE]
            [--force] [--trust] [--sandbox MODE] [--approve-mcps]
            [--branch NAME] [--base NAME] [--no-git] [--no-metrics] [--no-title]
            [--notify] [--tab-title PREFIX] [--tab-color HEX] [--no-color]
            [--completed-file] [--dry-run]
            [--recover] [--no-auto-recover] [--wait-for-reset] [--max-wait MIN]
            [--no-wip-commit] [--allow-dirty] [--verify-cmd CMD]
odin status [QUEUE_DIR]
odin resume HELD_TASK [QUEUE_DIR]
odin recover [STEM] [QUEUE_DIR] [--project PATH] [--dry-run] [--no-wip-commit]
            [--no-brief] [--verify-cmd CMD] [--wait-for-reset] [--max-wait MIN]
            [--platform NAME] [--model MODEL] [--branch NAME]
            [--run] [--no-run] [--force] [--yes]
odin platforms [--project PATH] [--no-color]
odin guide  [TOPIC]
odin archive [QUEUE_DIR]
odin metrics [--html [PATH]] [--project SUBSTR] [--file PATH]
odin config [show|get KEY|set KEY VALUE]
```

`odin recover` puts a task that was cut off **mid-work** back to work: it
commits whatever partial work is in the tree as a single WIP checkpoint (so the
tree is clean and the clean-tree check passes), merges a **resumption brief**
into the task body so the next agent knows it is continuing rather than
starting fresh, and moves the file back to `pending/`. Distinct from `odin
resume`, which merges a *held* task's questions with the user's answers. Both
converge on the same shape — a sidecar next to the body, merged in on the way
out — but they answer different questions ("what did you decide?" vs. "what did
your predecessor already do?"). Details and rationale:
[`docs/interruption-recovery-proposal.md`](docs/interruption-recovery-proposal.md).

`odin run` is the primary door: on a TTY it detects an interrupted task (or one
stranded in `running/` by an odin process that died) at startup and offers to
recover it. Non-interactive runs **halt at exit 12** and print the command
instead — `-y` skips the platform confirmation and is deliberately *not* consent
to write a commit; only `--recover` is.

Recovery restores state; **running the queue is a separate decision**, taken by
a TTY prompt (default yes) or `--run` / `--no-run`. `--platform`, `--model` and
`--branch` are forwarded into the continuation, which re-enters `main(["run",
…])` rather than reimplementing the loop. Recovery always ends by *saying* it
worked (`✓ recovered · … (N tasks ready)`) plus the literal next command: the
mechanical `→` lines alone once made a crash after them read as total failure.

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

`odin platforms` is the **discoverability surface** for platform and parameter
values: per registered platform it prints the key, product name, binary (and
whether it is on `PATH`), the model that would resolve right now *and its
source*, the accepted `--model` forms (`backend.model_help`), the instruction
files (marked present/absent against `--project`), platform-only flags, and the
`[platforms.<name>]` config keys — then a footer with the resolution order and
the few flags whose value set Odin actually owns. Content/logic lives in
`platforms.py` (same posture as `guide.py` / `metrics.py`: stdlib only,
TTY-gated color, never writes anything).

Nothing in it may be re-declared: platform names come from
`registry.available_platforms`, per-platform facts from the backend
(`product`, `default_binary`, `instruction_files`, `model_help`,
`config_keys`, `platform_flags`), and resolved values from the same
`config.resolve_*` functions `odin run` uses. The `--platform` argparse
`choices` (on `run` and `recover`) and `resolve_platform`'s error message are
generated from the registry for the same reason — the old hand-typed
"claude, cursor, grok" prose in `--platform`'s help was a list that could
silently fall behind a newly registered backend. `--platform` normalises via
`cli._platform_name` before `choices` is tested, because `get_backend` has
always been case-insensitive and the flag must not be stricter.

`choices` is for value sets **Odin** owns (`--platform`, `--sandbox`). It is
deliberately *not* used for `--model` or `--permission-mode`: those belong to
the provider, and pinning an allowlist would reject values that actually work —
the same reasoning that keeps `validate_model` shape-based rather than a
catalogue. Those get *discoverability* (listed in `odin platforms`) without
enforcement. Registry `choices` also cannot police `$ODIN_PLATFORM` or
`default_platform` in config, so the runtime `get_backend` error stays the
backstop; both paths are pinned by tests.

`odin guide` prints a self-contained authoring manual to stdout (queue layout,
task-file format, CLAUDE.md / AGENTS.md workflow, the injected protocol, the
run flow) so an agent in another project can self-discover the format with no
other context. `TOPIC` ∈ {all (default), tasks, claude-md, agent-md, protocol,
terminal}. The `agent-md` topic covers Cursor instruction files and the
cross-platform layout; `terminal` is an agent-executable iTerm2 setup manual.
The protocol section is generated from `contract.build_system_prompt`, so it
can't drift from runtime. Content lives in `guide.py`.

There is deliberately **no `odin demo`**. The `otest` scaffolder was removed in
0.2.5: it was a second product surface to keep working (its own fixture
project, embedded file blobs, a regeneration script) that no user path went
through, and it drifted behind the real CLI. End-to-end verification is the
test suite plus running a real queue; the documented command examples in the
README are the onboarding path it was supposed to serve. Do not reintroduce it
without a concrete user need.

`odin metrics` reads the central metrics log and prints an aggregate summary
(run/task counts, outcomes, token usage, cost, average run/task times, peak
concurrent runs, per-project breakdown). `--html [PATH]` renders a
self-contained HTML report instead of text (default `odin-metrics.html`);
`--project SUBSTR` filters by project path substring; `--file` overrides the
log path. Content/logic lives in `metrics.py`.

`odin config` views or edits `$ODIN_HOME/config.toml` (default
`~/.odin/config.toml`; `$ODIN_CONFIG` overrides). Interactive menu on a TTY;
`show` / `get KEY` / `set KEY VALUE` for non-interactive use. This is the
**only** command that writes the config — `odin run` never auto-scaffolds it.

Defaults:
- `QUEUE_DIR` = `./queue`. A bare name (no path separator that already exists)
  resolves under `./queue/`: `odin run add-search` → `queue/add-search` when it
  exists, so the `queue/` prefix is optional. An existing path as-given always
  wins; nonexistent falls through unchanged. Shared by run/status/resume/archive
  via `cli._resolve_queue_arg`.
- `--project` = current working directory
- `--platform` = unset → `$ODIN_PLATFORM` → `default_platform` in config →
  error if still unset (no built-in product default). Registered peers:
  `claude`, `cursor`, `grok` (see `docs/agent-backends.md`). Unknown names
  are a hard registry error. `--model` = unset → `$ODIN_MODEL` →
  `platforms.<platform>.model` → platform CLI default (no `--model` flag
  emitted).
- `--agent-bin` = unset → config `platforms.<p>.binary` → backend default
  (works for every platform). `--claude-bin` is a deprecated alias that only
  applies when platform is `claude`. Cursor autonomy flags
  (`--force`/`--trust`/`--sandbox`/`--approve-mcps`) are ignored on non-cursor
  platforms with a warning.
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
- `--dry-run` = preview one task's resolved platform + argv (from
  `backend.build_invoke`) and prompt, without spawning the agent.

## Write surfaces outside the queue/target project

Odin writes **two** things outside the queue and target project:

1. **Metrics telemetry** — append-only JSONL under `$ODIN_HOME/metrics/`
   (see Metrics below). Written automatically on every `odin run` unless
   disabled.
2. **User-initiated config** — `$ODIN_HOME/config.toml`, written **only**
   via explicit `odin config` (interactive or `set`). Never silent
   auto-scaffolding during `odin run`.

Everything else Odin touches stays inside the queue dir or is a best-effort
TTY escape / opt-in `COMPLETED.md` mailbox in the queue.

## Metrics

Every `odin run` appends two record types — `task` (one per execution) and
`run` (one summary per invocation), linked by a `run_id` — to a single central
JSONL log shared across all projects: `$ODIN_HOME/metrics/events.jsonl`
(default `~/.odin/metrics/events.jsonl`; `$ODIN_METRICS_FILE` overrides the
file). Together with user-initiated `odin config` writes, this is one of the
two things Odin writes outside the queue/target project.

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
├── failed/     # the agent finished its turn but broke the protocol
├── held/       # blocked on questions; resume with `odin resume`
├── interrupted/# cut off mid-work; recover with `odin recover`
│               #   NNN-slug.md + NNN-slug.recovery.md (evidence + brief)
├── carry/      # NNN-slug.next-context.md — emitted by the prior task
└── backlog/    # non-urgent follow-up tasks an agent discovered mid-run
```

`failed/` and `interrupted/` are kept apart on purpose. A **defect** — the agent
ended its turn on its own terms and emitted no sentinel — needs a human.
An **interruption** — a provider usage limit, a hard kill, Odin's own process
dying — says nothing about the quality of the work and is recoverable. Folding
them together is what made the common case (interruption) cost a manual cleanup
in another tool. Interrupted work blocks `odin archive`, and a recovered task
stays distinguishable afterwards: its sidecar survives in `interrupted/` with
the full attempt log.

There is a **third** kind that lands in neither dir: `FailureKind.CONFIG`, the
provider refusing the request (unknown model, bad key, no access —
`api_error_status` 4xx other than 429). No work was attempted, so the task goes
straight back to `pending/` via `queue.return_to_pending`, nothing is committed,
no sidecar is written, and no metrics task row is recorded (there was no session
to measure). Exit 2. Structural, not message-matching, exactly like the
interruption rule: 429 and 5xx stay interruptions. Getting this wrong is not
theoretical — a transposed `--model opus-claude-5` classified as an interruption
and had `git add -A` sweep 5000 lines of unrelated work into a WIP commit.

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

Odin **injects this protocol itself** via the active backend (Claude:
`--append-system-prompt`; Cursor: prepended to the stdin prompt — see
`contract.py`), so tasks emit parseable output even if a target project
forgot the snippet. This is the *one* exception to "Odin contributes no
rules": it injects the **protocol only** (sentinel + question schema + the
single-branch directive), never workflow. The question JSON schema (problem →
question → options → optional recommendation + why, all brief) lives in
`contract.py`.

Odin scans the final assistant `result` for these markers. Anything else
routes to `failed/` for human inspection — silence is treated as failure
on purpose.

A completed task MAY additionally emit a `<<<FOLLOW_UP>>>` block (JSON list
of `{title, body, urgent}`) recording newly-discovered work. Non-urgent
items are filed in `backlog/` and called out when the queue drains (exit 0);
`urgent` items are inserted into `pending/` to run next, with the user asked
to continue or stop (unattended → halt, exit 11). See `protocol.parse` /
`parse_follow_ups` and `_handle_follow_ups` in `cli.py`.

## Interruption recovery

A task can stop for a reason that says nothing about the work: the agent CLI
hits a provider usage limit mid-task, the process is killed, or Odin itself
dies. `backend.classify_failure` splits these from real defects **structurally**
— did the turn end on the agent's own terms? — so recognising a provider's
exact wording is never required for correct routing. Recognising it only
*enriches*: the reason label and the reset time. An unrecognised limit notice
still routes to `interrupted/`; a test pins that guarantee down.

The flow, all of it in `recovery.py` + `cli.py`'s recovery section:

1. **Interruption.** `_route` snapshots the working tree (`git.snapshot`, read
   only) and writes `interrupted/NNN-slug.recovery.md` — reason, confidence,
   reset time, the attempt log, and the dying agent's last words. No commit yet.
2. **Recovery** (`odin recover`, or the offer at `odin run` startup). Commits
   the partial work, then merges the brief and moves the body to `pending/`.
   Commit-first is deliberate: if the machine dies during a reset wait, the work
   is already safe.
3. **Resumption.** The brief tells the next agent it is continuing — which
   commit holds its predecessor's work, which commit was the last real
   milestone, what the predecessor last said, and to reconcile before writing.
   Merged between the carry-context and the task body, delimited by
   `<!-- odin:resumption-brief -->` so a re-recovery *replaces* it rather than
   stacking a second, staler account.

Repeated interruption of one task is normal — a large task legitimately spans
several usage windows — so the circuit breaker is **progress, not attempt
count**: two consecutive attempts with no turns and no file changes block
further recovery (`--force` overrides) on the grounds that the problem is
environmental, not a limit.

Exit codes: `10` held, `11` urgent halt, **`12` interrupted**, `2` config error
(refused request / bad model — the usage-error code, because that is what it is).

Two carve-outs worth knowing: `--max-turns` firing stays a **defect** (it is the
user's own circuit breaker; recovering it would re-run straight back into the
cap), and the WIP commit refuses to run if a dirty path looks like a credential
(`git.SECRET_GLOBS`) — an automated `git add -A` is exactly how the "never
commit secrets" rule gets broken by accident.

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
- **Target project instructions** — workflow rules (Claude: `CLAUDE.md`;
  Cursor: `AGENTS.md` / `.cursor/rules`). Sentinel emission is injected by
  Odin; see `examples/target-claude-md-snippet.md` and
  `examples/target-agents-md-snippet.md`.
- **Per-task `.md` files** — the body of the prompt. No frontmatter.

## Non-goals

- No UI. CLI only.
- No long-running server. One-shot `odin run`.
- No retry beyond what the target instructions describe. Failed tasks stay
  failed until the user moves them back to `pending/`.
- No parallelism. Tasks are strictly sequential.
- **Git is startup-only, plus exactly one commit.** Odin verifies a clean tree
  and selects/creates the one branch the whole queue lands on, then checks it
  out — once, before the loop. Per-task milestone commits stay the target
  instructions' job. (This narrows the original "no git operations" non-goal —
  approved deliberately. `--no-git` restores the zero-git behaviour for non-git
  projects.)

  The **one** exception is the recovery WIP checkpoint (`git.commit_wip`): when
  a run is interrupted mid-task, `odin recover` commits the partial work it left
  behind as a single `wip(odin): …` commit so the queue can restart against a
  clean tree. This was approved deliberately as decision 1 of the interruption
  recovery design — it automates a step the user was already performing by hand,
  and it removes the need for any dirty-tree waiver machinery. It is never
  silent: it happens only on the recovery path, only with consent (a TTY prompt
  or `--recover`), it is previewable with `--dry-run`, and `--no-wip-commit`
  opts out. Odin still **never** pushes, merges, rebases, amends, or opens PRs,
  and never rewrites history — including the WIP commit itself, which is left in
  place carrying `Odin-WIP:` / `Odin-Run:` trailers so it is greppable and
  squashable at the user's leisure.

## Install and invocation model

Odin is installed once and invoked from inside any target project:

```
# install once
uv tool install --from /path/to/odin odin

# from inside any project
cd ~/code/myproject
odin run --platform claude     # Claude Code (`claude`)
odin run --platform cursor     # Cursor CLI (`agent`)
odin run --platform grok       # Grok Build (`grok`)
```

The agent subprocess runs with `cwd` set to `--project` (default `$PWD`), so
it picks up the **target project's** instruction file, not this one. The only
rules Odin contributes are the **protocol** (sentinel + question schema +
single-branch directive), injected via `--append-system-prompt` (Claude) or
prompt prepend (Cursor); all workflow rules still come from the target
instructions.

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

- Stdlib only at runtime. The selected platform's binary must be on `PATH`
  (or passed via `--claude-bin` / `--agent-bin`); Odin shells out.
- Invocation is built by `backend.build_invoke` (never hard-code argv in the
  loop). Claude baseline: `--output-format stream-json --verbose
  --permission-mode bypassPermissions --append-system-prompt <protocol>`.
  Cursor baseline: `--output-format stream-json --force --trust --workspace
  <project>` with protocol prepended to stdin. **No `--max-turns` by
  default** — only pass it when the user sets it as a circuit-breaker.
  Never `--resume` — every task is a fresh session.
- **Drain stderr concurrently.** The runner reads the subprocess's stderr on a
  background thread, not after `wait()`. Reading only stdout while the child
  fills its ~64KB stderr pipe deadlocks the session (the agent then perceives
  delayed tool output and spam-probes with `echo`). Don't regress this.
- Set the subprocess `cwd` to `--project` so the target instructions load.
- **Git startup** (unless `--no-git` or non-git project): refuse to start
  on a dirty tree (the queue dir is excluded from the check); resolve the
  branch from `--branch`/`--base`, else the interactive prompt on a TTY,
  else the current branch. Odin only ever runs `status`, `switch`,
  `show-ref`, `rev-parse`, `symbolic-ref` — never anything that writes
  history. Per-task commits are the agent's job (per the target instructions).
- **Conflict safeguards.** The injected contract states it takes precedence
  over the target instructions for task-termination and git/branch/PR policy.
  When git is managed, `lint.scan_project_instructions(project, platform)`
  warns (never blocks) if the platform's instruction file mandates a
  conflicting workflow (PRs, branch-per-task, push, no-commit).
  `odin guide claude-md` / `odin guide agent-md` emit pasteable "This project
  is run by Odin" marker blocks.
- Stream stdout to the user's terminal live so they see progress; capture
  the final JSON for parsing. `--output-format stream-json` is the
  reliable way to get both.
- Failure signals are backend-owned (`RunResult.succeeded`): Claude uses
  non-zero exit / `.error` / bad `stop_reason`; Cursor uses non-zero exit /
  missing terminal `result` / `is_error`. Any → `failed/`.
- Never delete a queue file. Only move between subdirs. The audit trail
  matters more than tidy directories.
- Never assume the target project's git state — that's the target
  instructions' job. Odin reports what came back, nothing more.
