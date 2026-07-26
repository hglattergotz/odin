# Changelog

All notable changes to Odin are documented here. The format roughly follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/). Releases are git tags (`vX.Y.Z`); install one
with `uv tool install --from 'git+https://github.com/hglattergotz/odin@vX.Y.Z' odin`.

## [Unreleased]

### Added
- **Interruption recovery.** A task cut off mid-work — a provider usage limit,
  a hard kill, or an odin process that died — is now told apart from a task
  that failed, and can be put back to work with one command. New
  `interrupted/` queue state (task body + `NNN-slug.recovery.md` sidecar) and
  a new `odin recover [STEM] [QUEUE]`.
  See [`docs/interruption-recovery-proposal.md`](docs/interruption-recovery-proposal.md).
- `odin run` detects an interrupted task (or one stranded in `running/`) at
  startup and offers to recover it on a TTY — plain `odin run` is the primary
  door. Non-interactive runs halt at the new exit code **12** and print the
  command instead.
- **Resumption brief.** A recovered task is prepended with a synthesized
  account of its own interrupted attempt: which commit holds the partial work,
  which commit was the last real milestone, the dying agent's last words, and
  instructions to reconcile before writing. Fresh sessions previously left the
  retrying agent with no idea half-finished work in the tree was its own.
- **WIP checkpoint commit** (`odin recover`, `--no-wip-commit` to opt out) —
  the one and only commit Odin makes; see "Changed". Left in history carrying
  `Odin-WIP:` / `Odin-Run:` trailers (`git log --grep=Odin-WIP`).
- Waiting for a provider reset window: `--wait-for-reset` / `--max-wait`, or an
  interactive offer when Odin can parse the stated reset time. Combined with
  `--recover`, an interrupted overnight queue can finish by itself.
- Recovery is previewable (`odin recover --dry-run` prints the plan and the
  exact brief), configurable (`[recovery]` in `~/.odin/config.toml`), and can
  fold a verification command's output into the brief (`--verify-cmd`).
- `--allow-dirty` on `odin run` as a general clean-tree escape hatch.
- `interrupted` is a first-class metrics outcome, counted apart from failures.

### Changed
- **Odin now makes exactly one kind of commit.** The "never commits" non-goal
  is narrowed: the recovery WIP checkpoint. It happens only on the recovery
  path, only with consent (TTY prompt or `--recover`), and never pushes,
  merges, rebases, amends, or rewrites history. Deliberate — it automates the
  manual commit users were already doing, and keeps the clean-tree check
  working unchanged.
- `-y` / `--yes` no longer implies consent to recover. It skips the
  platform/model confirmation only; writing a commit requires `--recover`.
- README documents interruption recovery and carries a **Command examples**
  section: complete, copy-pasteable invocations with the queue name and flags
  (including `odin run <queue> --recover --wait-for-reset` for an unattended
  run that survives a usage limit) in place of the demo walkthrough. The same
  examples are in `odin -h` as a **common commands** block below quickstart.
- `odin -h` no longer describes Odin as running tasks through `claude -p`; it
  names all three products and how to select one. Same for `odin run -h`,
  which also now mentions stopping on an interruption.

### Removed
- **`odin demo` and the `otest` fixture project.** It was a second product
  surface to keep working — its own scaffolded project, embedded file blobs
  (`_demo_files.py`) and a regeneration script — that no user path went
  through and that drifted behind the real CLI. Onboarding is `odin guide`
  plus the README's command examples; end-to-end verification is the test
  suite and a real queue. Gone: `src/odin/demo.py`, `src/odin/_demo_files.py`,
  `scripts/regen_demo_files.py`, `tests/test_demo.py`.

### Fixed
- The `[recovery]` config table is now actually consulted. `auto_recover`,
  `wait_for_reset` and `max_wait_minutes` had accessors in `config.py` that
  nothing ever called, so only `verify_command` worked. An explicit flag still
  beats config, and no config key can authorise the WIP commit — that stays
  `--recover`, per invocation.
- `error: "success"` — Claude Code sets `is_error` while leaving `subtype` at
  `"success"` when a session is cut short, which Odin reported as the literal
  string `success`. Five of six historical failures carried this nonsense
  label; it now names what actually happened.
- Task metrics records now include `exit_code`, so the log can answer "was this
  an interruption?" retrospectively.
- Claude's streamed assistant text is retained (bounded tail), so a run killed
  before it emits a terminal `result` event still has final text to diagnose
  and to build a brief from. Previously it was rendered and then dropped.

## [0.2.4] — 2026-07-19

### Added
- **Multi-platform agent backends:** run the same queue through **Claude Code**
  (`claude`), **Cursor CLI** (`agent`), or **Grok Build** (`grok`) via peer
  `AgentBackend` implementations. See `docs/agent-backends.md`.
- `--platform` / `$ODIN_PLATFORM` / `default_platform` in config; `--model` /
  `$ODIN_MODEL` / per-platform model in config; `odin config` to view and edit
  `~/.odin/config.toml`.
- Universal `--agent-bin` (any platform); `--claude-bin` remains a Claude-only
  deprecated alias. Cursor-only flags: `--force`, `--trust`, `--sandbox`,
  `--approve-mcps`.
- TTY pre-run platform/model confirmation (`--yes` / `-y` to skip).
- `scripts/install-tool.sh` to install or refresh the global `odin` uv-tool
  from a local checkout (`--editable` optional).

### Changed
- **No built-in default platform.** `--platform` is required unless
  `$ODIN_PLATFORM` or `default_platform` is set, so a model id cannot silently
  pair with the wrong product.
- Guide, README, and contract/lint/metrics are platform-aware (instruction
  file names, product wording, null cost totals where a CLI omits cost).

[0.2.4]: https://github.com/hglattergotz/odin/releases/tag/v0.2.4

## [0.2.2] — 2026-06-06

### Added
- `odin guide terminal` — an agent-executable topic that configures a Mac
  terminal (iTerm2) for Odin's tab signaling: install, the per-project
  tab-color/title/badge shell hook, the manual iTerm2 toggles, and optional
  Claude notifications. Point your Claude agent at it to set everything up.
- `odin metrics`: the **By project** breakdown (text and HTML) now ends with a
  **TOTAL** row summing runs / tasks / outcomes / cost; its avg-task shows the
  overall mean.

### Changed
- `docs/` now holds end-user documentation only — implementation specs were
  moved out of the repo.

[0.2.2]: https://github.com/hglattergotz/odin/releases/tag/v0.2.2

## [0.2.1] — 2026-06-06

### Added
- `odin --version` now also reports where Odin is running from (e.g.
  `odin 0.2.1 (from .../site-packages/odin)`), to tell the global install
  apart from a `uv run` / source checkout; added `-V` as a short alias.

## [0.2.0] — 2026-06-06

### Added
- **Terminal tab signaling** (best-effort, stdlib-only, TTY-gated): live tab
  title (`odin ⏵ N/total <queue>`) and an OSC 9;4 queue **progress bar**, both
  on by default and silently ignored where unsupported. `--notify` /
  `ODIN_NOTIFY` opt in to iTerm2 tab color, dock attention, and desktop
  notifications. `--no-title` / `ODIN_NO_TITLE` disable; `--tab-title PREFIX`
  and `--tab-color HEX` (defaults to `$PROJECT_TAB_COLOR`) tune them.
- **Opt-in `COMPLETED.md` mailbox** (`--completed-file` / `ODIN_COMPLETED`) —
  a metadata-only run summary written into the queue dir for an Odin→Claude
  handoff.
- **Restyled streamed output**: colored section markers, indentation, blank-line
  spacing, a `✓`/`✗`/`⏸` task footer with a turns·time·cost run summary, and
  project-relative tool paths. `--no-color` / `NO_COLOR` / `ODIN_NO_COLOR`
  disable color while keeping the layout.

### Changed
- `queue/` run-state is no longer tracked in git (gitignored).

## [0.1.0] — 2026-06-06

First tagged release.

### Added
- `odin run` — run a named queue of task files through `claude -p`, one fresh
  session per task, carrying context forward; halts to ask when the agent
  needs input.
- Sentinel protocol injected via `--append-system-prompt`
  (`NEXT_CONTEXT` / `NEEDS_INPUT` / `FOLLOW_UP`), so target projects need no
  Odin-specific boilerplate.
- Startup-only git: clean-tree check and one-branch-per-batch selection.
- `odin status` and `odin archive` for managing named queues.
- `odin resume` for the unattended held-task flow.
- Central JSONL run/task metrics and `odin metrics` (text or `--html` report).
- `odin guide` self-contained authoring manual; `odin demo` end-to-end fixture.
