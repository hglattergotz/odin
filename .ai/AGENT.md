# Odin — codebase map

Agent-facing map of the Odin source. Odin is a headless task orchestrator: it runs a queue of tasks through
a coding-agent CLI (`claude -p`) one fresh session at a time, parses the structured output, carries context
forward, and halts cleanly when the agent needs input. **Orchestration is intentionally dumb** — workflow
(tests, commits, branching) lives in the *target project's* `CLAUDE.md`; Odin only invokes, observes, routes,
and carries context.

- **Language:** Python 3.11+, **stdlib-only at runtime** (pytest is the sole dev dep), **uv-managed** — never
  the system Python.
- **Layout:** one flat package, `src/odin/` (no subpackages).
- **Entry point:** `odin.cli:_entry` (console script `odin`).
- **Working on Odin itself:** read `CLAUDE.md` (architecture, rules, supply-chain constraints). **Authoring a
  queue** for a target project: run `odin guide`.

## Modules (`src/odin/`)

| Module | Responsibility | Key API |
|--------|----------------|---------|
| `cli.py` | argparse + central orchestrator: the run loop (claim → run → route), git startup, terminal signaling, interactive held-answering, follow-ups, subcommand dispatch. | `main`, `_entry`, `_cmd_run`, `_run_loop`, `_route` |
| `queue.py` | Domain core — the filesystem task queue. **Move-only** (never delete) lifecycle across `pending/running/done/failed/held/carry/backlog`; carry-context; held Q&A resume; container/sub-queue archiving. | `Task`, `Queue`, `archive_finished_subqueues` |
| `runner.py` | Subprocess wrapper around `claude -p --output-format stream-json`: builds argv, feeds prompt on stdin, **drains stderr concurrently** (deadlock fix), parses stream-json live, captures the terminal `result` (text/stop/usage/cost) into `RunResult`. | `RunResult`, `run_claude` |
| `protocol.py` | Pure (no-I/O) parser for the sentinel protocol: classifies the final message COMPLETED/HELD/UNPARSEABLE from `<<<NEXT_CONTEXT>>>` / `<<<NEEDS_INPUT>>>` / `<<<FOLLOW_UP>>>`; parses question + follow-up JSON. | `parse`, `parse_questions`, `parse_follow_ups` |
| `contract.py` | The **one** rule Odin injects (`--append-system-prompt`): the protocol-only system prompt (sentinel termination, question schema, single-branch directive). Source of truth `guide.py` renders from. | `build_system_prompt` |
| `git.py` | Startup-only git wrapper (**read/position only** — never commits/pushes/merges): repo/branch/clean checks (queue dir excluded), checkout, create-and-checkout. | `is_clean`, `checkout`, `create_and_checkout` |
| `prompts.py` | Interactive terminal human-in-the-loop (injectable streams): branch selection, render + collect answers to `NEEDS_INPUT` (empty = take the recommendation), continue/stop after an urgent insert. | `ask_branch_choice`, `ask_questions`, `ask_continue` |
| `metrics.py` | Central **append-only JSONL** run/task metrics (flock-guarded, best-effort, metadata-only) at `~/.odin/metrics/events.jsonl`; `RunAccumulator` emits a run summary on every exit path; aggregate/render for `odin metrics`. | `RunAccumulator`, `aggregate`, `render_text`, `render_html` |
| `completed.py` | Opt-in `COMPLETED.md` mailbox — metadata-only Odin→Claude handoff written into the queue dir on every exit; pairing by directory, not PID. | `render`, `write_record` |
| `guide.py` | `odin guide` — self-contained queue-authoring manual (topics: tasks/claude-md/protocol/terminal); protocol section rendered from `contract` so it can't drift. | `render`, `TOPICS` |
| `lint.py` | Pure text scan of a target `CLAUDE.md` for git-workflow directives that conflict with Odin's one-branch model; **advisory only** (never blocks). | `scan_claude_md` |
| `demo.py` | `odin demo` scaffolder — writes the embedded `otest` fixture; refuses to `--force`-wipe a real repo/home/root. | `create_demo` |
| `_demo_files.py` | **Generated** data (do not hand-edit) — the `otest` fixture as a path→content `FILES` dict; regenerated from a pristine `../otest`. | `FILES`, `QUEUE_SUBDIRS` |
| `term.py` | Best-effort OSC terminal signaling (stdlib byte writes, TTY-gated, tmux-wrapped): title, tab color, attention, notify, progress bar. | `set_title`, `set_progress`, `notify` |
| `style.py` | Best-effort ANSI styling for stdout (TTY + `NO_COLOR`/`--no-color` gated): SGR paint + semantic helpers + glyphs; plain text with glyphs when color is off. | `paint`, `header`, `dim`, `GLYPH_*` |
| `__init__.py` | Package marker; resolves `__version__` (falls back to `0.0.0+unknown` from a source tree). | `__version__` |

## Core flows

**Run loop** (`_cmd_run` → `_run_loop`):
```
next pending task → queue.claim_running → prepend carry-context
  → runner.run_claude (claude -p, protocol injected via contract)
  → cli._route → protocol.parse(final_text)
     ├─ COMPLETED → done/ (+ write carry-context for next task; file backlog/urgent follow-ups)
     ├─ HELD      → held/ (see below)
     └─ UNPARSEABLE / non-zero exit / bad stop → failed/
  → metrics.RunAccumulator.record_task ; repeat
```

**Held → resume** (agent emitted `<<<NEEDS_INPUT>>>`):
- **TTY:** `protocol.parse_questions` → `prompts.ask_questions` → answers merged into the held file → task
  re-queued and re-run in a **fresh** session (no `--resume`).
- **No TTY (CI):** write `held/NNN.questions.md`, exit `10`; user fills `## Answers`, then `odin resume`.

## Conventions & invariants (don't regress)

- **Stdlib-only runtime; uv-managed.** See `CLAUDE.md` supply-chain rules (zero deps, 14-day package-age, no
  system Python) — treat changes to them as needing explicit approval.
- **Queue is move-only** — never delete a task file; the audit trail matters more than tidy dirs.
- **Best-effort side channels** (`metrics`, `term`, `style`, `completed`): every emission is wrapped and
  swallowed, TTY-gated, and **metadata-only** — they must never sink a run.
- **Git is startup-only** — clean-tree check + select/create the one branch, then hands off. Never commits,
  pushes, merges, or opens PRs; per-task commits are the target `CLAUDE.md`'s job.
- **One write surface** outside the queue/target project: the metrics JSONL.
- **Protocol is the only rule Odin injects** (via `--append-system-prompt`); everything else is the target
  project's `CLAUDE.md`.
- **Fresh session per task** — never `--resume`; carry-context is explicit, not conversational.

## Tests

`tests/` (pytest via `uv run`). Runner/loop tests use fake `claude` shell scripts (see `tests/test_runner.py`)
so they're fast and decoupled from a real agent binary.
