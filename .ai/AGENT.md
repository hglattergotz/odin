# Odin — codebase map

Agent-facing map of the Odin source. Odin is a **headless multi-platform task orchestrator**: a queue drives
a coding-agent CLI — **Claude Code** (`claude`), **Cursor CLI** (`agent`), or **Grok Build** (`grok`),
selected by `--platform` — one fresh session per task. Odin parses the structured output, carries context
forward, and halts cleanly when the agent needs input. **Orchestration is intentionally dumb**: workflow
(tests, commits, branching) lives in the *target project's* instruction file; Odin only invokes, observes,
routes, and carries context.

- **Language:** Python 3.11+, **stdlib-only at runtime** (pytest is the sole dev dep), **uv-managed**.
- **Layout:** flat package `src/odin/` + one subpackage `src/odin/backends/` (the platform taxonomy).
- **Entry point:** `odin.cli:_entry` (console script `odin`).
- **Working on Odin:** read `CLAUDE.md` (architecture, rules, supply-chain constraints). **Authoring a
  queue** for a target project: run `odin guide`.

## Engine data flow

```
cli._cmd_run
  → config.resolve_platform / resolve_model      (--flag → $ENV → config; platform required, no default)
  → backends.registry.get_backend(platform)      (name → AgentBackend; unknown = hard error)
  → git startup (clean check + one branch)        (git.py; never commits/pushes)
  → _run_loop, per task:
      queue.next_pending → _build_prompt (+ carry-context) → contract.build_system_prompt (injected protocol)
      → runner.run_agent(prompt, project, backend, run_options)     (generic subprocess loop)
      → protocol.parse(final_text)  →  done / held / failed  →  queue moves + carry/backlog
      → metrics.RunAccumulator.record_task
```

**The separation that matters:** Odin owns *sequencing* + the *sentinel protocol* (`protocol.py` /
`contract.py`); the *target project's* `CLAUDE.md` / `AGENTS.md` owns *workflow*. Platform-specific behavior
is delegated entirely to a backend — see [`backends/AGENT.md`](backends/AGENT.md).

## Modules (`src/odin/`)

| Module | Responsibility | Key API |
|--------|----------------|---------|
| `cli.py` | argparse + command controller (run/exec/status/resume/demo/guide/archive/metrics/config). `_cmd_run` resolves platform+model+backend, git startup, builds `RunOptions`, drives `_run_loop`. | `main`, `_entry` |
| `runner.py` | **Generic, platform-agnostic** subprocess loop: exec, prompt delivery (stdin or temp file per `AgentInvokeSpec.prompt_via`), concurrent stderr drain, NDJSON loop, timing. Delegates argv/event/terminal-classification to the backend. Also hosts shared stream-render helpers. | `run_agent`, `RunResult` |
| `backends/` | **Platform taxonomy** — one `AgentBackend` per product; every platform is a peer. Contract + registry + peers. | see [`backends/AGENT.md`](backends/AGENT.md) |
| `config.py` | `config.toml` load + hand-rolled minimal TOML writer; resolution order `--flag → $ENV → config → error/None`. No default platform. | `resolve_platform`, `resolve_model`, `load_config`, `save_config`, `PlatformRequiredError` |
| `protocol.py` | Pure parser for the sentinel protocol: `<<<NEXT_CONTEXT>>>` / `<<<NEEDS_INPUT>>>` / `<<<FOLLOW_UP>>>` + question/follow-up JSON. | `parse`, `parse_questions`, `parse_follow_ups` |
| `contract.py` | Builds the protocol text Odin injects (platform-aware: `--append-system-prompt` / prepend / `--rules`). The one rule Odin contributes. | `build_system_prompt` |
| `queue.py` | Filesystem queue state machine: `pending/running/done/failed/held/carry/backlog`, **move-only** (`os.replace`), carry-context, held Q&A resume, container archiving. | `Queue`, `Task`, `archive_finished_subqueues` |
| `metrics.py` | Central append-only JSONL run/task metrics (flock-guarded, metadata-only, best-effort) + aggregation/reporting. | `RunAccumulator`, `aggregate`, `render_text`, `render_html` |
| `git.py` | **Startup-only** git (clean check + select/create the one branch). Never commits/pushes/PRs. | `is_clean`, `checkout`, `create_and_checkout` |
| `lint.py` | Advisory scan of a target's instruction files for git-workflow conflicts; resolves which files via the backend registry. | `scan_project_instructions` |
| `guide.py` | `odin guide` — self-contained authoring manual; protocol section rendered from `contract` so it can't drift. | `render`, `TOPICS` |
| `prompts.py` | Interactive terminal H-I-T-L: platform/model confirmation, branch choice, `NEEDS_INPUT` Q&A, `odin config` menu. | `ask_run_confirmation`, `ask_questions`, `ask_branch_choice` |
| `completed.py` | Opt-in `COMPLETED.md` mailbox — metadata-only run-outcome handoff to a paired session. | `write_record` |
| `demo.py` / `_demo_files.py` | `odin demo` scaffolder + its **generated** `otest` fixture (do not hand-edit `_demo_files.py`). | `create_demo`; `FILES` |
| `style.py` / `term.py` | Best-effort ANSI styling / OSC terminal signaling — TTY-gated, error-swallowing cosmetics. | `paint`, `dim`; `set_title`, `set_progress` |
| `__init__.py` | Package marker; `__version__` via importlib.metadata. | `__version__` |

## Conventions & invariants (don't regress)

- **Stdlib-only runtime; uv-managed.** See `CLAUDE.md` supply-chain rules (zero deps, package-age, no system
  Python) — changes to them need explicit approval.
- **Every platform is a peer** — there is no first-class backend and **no default platform** (`--platform`
  required; unknown = hard error).
- **Queue is move-only** — never delete a task file; the audit trail matters.
- **Best-effort side channels** (`metrics`, `term`, `style`, `completed`): wrapped, TTY-gated, metadata-only —
  never sink a run.
- **Git is startup-only** — clean check + one branch, then hands off; per-task commits are the target's job.
- **One write surface** outside the queue/target: the metrics JSONL (`odin config` also writes, only on
  explicit `config set`).
- **Protocol is the only rule Odin injects** (via the backend's system-prompt mechanism); everything else is
  the target's instruction file.
- **Fresh session per task** — never `--resume`; carry-context is explicit, not conversational.

## Tests

`tests/` (pytest via `uv run`). Runner/backend/loop tests use fake agent shell scripts (see
`tests/test_backends.py`, `tests/test_runner.py`) so they're fast and decoupled from real agent binaries.
