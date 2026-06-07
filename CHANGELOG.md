# Changelog

All notable changes to Odin are documented here. The format roughly follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/). Releases are git tags (`vX.Y.Z`); install one
with `uv tool install --from 'git+https://github.com/hglattergotz/odin@vX.Y.Z' odin`.

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

[0.2.1]: https://github.com/hglattergotz/odin/releases/tag/v0.2.1

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

[0.2.0]: https://github.com/hglattergotz/odin/releases/tag/v0.2.0

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

[0.1.0]: https://github.com/hglattergotz/odin/releases/tag/v0.1.0
