# Multi-platform agent backends for Odin

**Status:** Proposal (branch `cursor`)  
**Goal:** Let Odin orchestrate tasks through multiple headless agent CLIs — starting with **Claude Code** (`claude`) and **Cursor Agent** (`agent`), with a path to **Kiro** and others — without degrading existing behaviour.

This document is written to be split into an Odin task queue. It assumes you have read `CLAUDE.md` in this repo and understand how Odin works today.

---

## 1. What Odin does today (Claude-only)

Odin is intentionally dumb orchestration:

1. Pick the next task file from `queue/<name>/pending/`.
2. Prepend carry-context from the prior task.
3. Invoke **`claude -p`** in the target project (`cwd = --project`).
4. Stream NDJSON (`--output-format stream-json`) to the terminal.
5. Parse the terminal **`result`** event → classify via sentinel markers (`<<<NEXT_CONTEXT>>>`, `<<<NEEDS_INPUT>>>`).
6. Move the task file between queue states; repeat.

Everything else (tests, commits, branching per task) lives in the **target project's** instruction file (`CLAUDE.md`). Odin injects only the **protocol** (sentinels + question JSON + single-branch directive) via `--append-system-prompt`.

### Claude-specific coupling (must be abstracted)

| Area | File | Coupling |
|------|------|----------|
| Subprocess | `src/odin/runner.py` | `run_claude()`, hard-coded argv, Claude stream-json event shapes |
| CLI | `src/odin/cli.py` | `--claude-bin`, `--permission-mode`, `--allowed-tools`, `--disallowed-tools`; calls `run_claude` directly |
| Protocol injection | `src/odin/contract.py` | Assumes `--append-system-prompt` exists |
| Metrics | `src/odin/metrics.py` | Normalises Claude token keys; records `claude_duration_ms`; expects `total_cost_usd` |
| Startup lint | `src/odin/lint.py` | Scans `CLAUDE.md` only |
| Guide / docs | `src/odin/guide.py`, README | Claude-centric wording |
| Success criteria | `runner._GOOD_STOPS` | Requires `stop_reason ∈ {end_turn, stop_sequence}` — **Cursor does not emit this** |

The queue model, sentinel protocol, carry-context, held/resume flow, follow-ups, git startup, terminal signalling, and metrics *shape* are all platform-agnostic. Only the **agent backend** layer is Claude-specific.

---

## 2. Design principle: backends, not forks

Introduce a small **AgentBackend** interface. Odin's loop stays the same; each platform implements:

```python
@dataclass(frozen=True)
class AgentInvokeSpec:
    argv: list[str]           # full command (binary + flags)
    prompt: str               # bytes/text sent on stdin (or embedded — see below)
    cwd: Path

@dataclass(frozen=True)
class RunResult:              # already exists — keep as the universal outcome
    succeeded: bool
    final_text: str
    stop_reason: str | None
    error: str | None
    exit_code: int
    session_id: str | None
    wall_ms: int
    duration_ms: int | None
    api_ms: int | None
    num_turns: int | None
    usage: dict | None
    cost_usd: float | None
    platform: str             # NEW: "claude" | "cursor" | ...
```

Each backend provides:

- **`build_invoke(prompt, project_dir, system_prompt, run_options) -> AgentInvokeSpec`**
- **`parse_stream_line(event: dict) -> StreamUpdate | None`** (optional; can share a normaliser)
- **`normalise_result(event: dict, exit_code, wall_ms) -> RunResult`**
- **`default_binary() -> str`**

Odin selects a backend from **`--platform`** (or config default). The existing Claude path becomes `ClaudeBackend`; Cursor becomes `CursorBackend`.

**Non-goals for v1:** session resume across tasks (both CLIs support `--resume`, but Odin's model is *fresh session per task* — keep that), cloud/SDK runtimes (local CLI only).

---

## 3. CLI surface

### New flag (primary selector)

```
odin run [QUEUE] --platform {claude,cursor} ...
```

Resolution order:

1. `--platform` on the command line
2. `$ODIN_PLATFORM`
3. `default_platform` in config (see §4)
4. Fallback: `claude` (preserves today's behaviour)

### Platform-specific flags

Keep Claude flags working unchanged when `--platform claude` (or default). Add Cursor equivalents; unknown flags for the active platform should warn and be ignored (or rejected — pick one, document it).

| Odin flag (today) | Claude mapping | Cursor mapping |
|-------------------|----------------|----------------|
| `--claude-bin` | `claude` binary | *(ignored)* |
| `--agent-bin` *(new)* | *(ignored)* | `agent` binary |
| `--permission-mode` | `--permission-mode` | *(no direct equivalent)* |
| `--allowed-tools` | `--allowed-tools` | *(no CLI flag — use `~/.cursor/cli-config.json` permissions)* |
| `--disallowed-tools` | `--disallowed-tools` | *(same)* |
| `--max-turns` | `--max-turns` | *(not exposed by Cursor CLI today — omit or no-op)* |
| *(new)* `--model` | *(Claude picks via its own config)* | `--model` |
| *(new)* `--force` | implied by `bypassPermissions` | `--force` / `--yolo` |
| *(new)* `--trust` | N/A | `--trust` (required for headless) |
| *(new)* `--sandbox` | N/A | `--sandbox enabled\|disabled` |
| *(new)* `--approve-mcps` | N/A | `--approve-mcps` |

Rename help text from "claude" to "agent" where generic; keep backward-compatible aliases.

### Dry-run output

Dry-run should print the **resolved backend name**, argv, and prompt length — not hard-code `claude -p`.

---

## 4. Configuration file

**Location:** `$ODIN_HOME/config.toml` (default `~/.odin/config.toml`). Override with `$ODIN_CONFIG`.

Rationale: platform binaries, models, permission posture, and future Kiro settings differ per user/machine. CLI flags override config; config overrides baked-in defaults.

### Example config

```toml
# ~/.odin/config.toml

default_platform = "claude"

# ---------------------------------------------------------------------------
# Claude Code (current behaviour)
# ---------------------------------------------------------------------------
[platforms.claude]
binary = "claude"
permission_mode = "bypassPermissions"
output_format = "stream-json"
verbose = true

# Optional passthrough lists (same as CLI defaults when unset)
# allowed_tools = ["Read", "Write", "Bash"]
# disallowed_tools = ["WebFetch"]

[platforms.claude.invoke]
prompt_via = "stdin"                    # claude -p reads stdin
system_prompt_via = "append_system_flag" # --append-system-prompt TEXT

[platforms.claude.metrics]
usage_input = "input_tokens"
usage_output = "output_tokens"
usage_cache_read = "cache_read_input_tokens"
usage_cache_write = "cache_creation_input_tokens"
cost_field = "total_cost_usd"           # on terminal result event

# ---------------------------------------------------------------------------
# Cursor Agent CLI
# ---------------------------------------------------------------------------
[platforms.cursor]
binary = "agent"
output_format = "stream-json"
force = true          # headless file edits (like bypassPermissions)
trust = true          # skip workspace-trust prompt in -p mode
approve_mcps = true   # unattended MCP approval
sandbox = "disabled"  # match Claude's full shell access by default
# model = "composer-2.5-fast"   # optional default; omit = account default

[platforms.cursor.invoke]
prompt_via = "stdin"           # `agent -p` reads stdin when no prompt arg
system_prompt_via = "prepend"  # no --append-system-prompt; prepend to user prompt

[platforms.cursor.metrics]
usage_input = "inputTokens"
usage_output = "outputTokens"
usage_cache_read = "cacheReadTokens"
usage_cache_write = "cacheWriteTokens"
cost_field = ""                # empty = no cost from CLI; store null

# ---------------------------------------------------------------------------
# Future: Kiro / Apple Foundation Models (placeholder shape)
# ---------------------------------------------------------------------------
# [platforms.kiro]
# binary = "kiro"
# ...
```

### Project-level overrides (optional v2)

`--project/.odin/config.toml` or `.odin.toml` merged over user config (project wins). Defer unless needed — user-level config is enough for v1.

---

## 5. Cursor CLI: equivalent invocation

Verified against local `agent` (CLI `2026.06.04-5fd875e`). Cursor's headless mode is **`agent -p`** (alias `--print`), not the `cursor` editor binary.

### Recommended argv (parity with today's Claude run)

```bash
agent \
  -p \
  --output-format stream-json \
  --force \
  --trust \
  --workspace "$PROJECT_DIR" \
  [--model "$MODEL"] \
  [--sandbox disabled] \
  [--approve-mcps] \
  # prompt on stdin
```

Working directory: set **`cwd`** and **`--workspace`** to `--project` so `AGENTS.md`, `.cursor/rules`, and git context load the same way as an interactive session in that folder.

**Do not** pass `--continue` / `--resume` — Odin needs a **fresh session per task** (same rule as Claude).

### Prompt delivery

Both CLIs accept **stdin** when no prompt arguments are given (verified for Cursor). Keep Odin's stdin-based prompt path; avoids shell-quoting bugs.

### System / protocol injection

Claude:

```
claude -p ... --append-system-prompt "<contract>"
# stdin = task prompt (+ carry context)
```

Cursor has **no** `--append-system-prompt`. Options:

| Approach | Pros | Cons |
|----------|------|------|
| **A. Prepend contract to prompt** *(recommended v1)* | No project file changes; works immediately; mirrors "system + user" | Slightly more input tokens; contract not in true system role |
| B. Write `.odin-protocol.md` + "read this first" | Cleaner separation | Agent may skip; file churn |
| C. Require protocol in `AGENTS.md` | Persistent | Drift vs `contract.py`; duplicates Claude's injection story |

**Recommendation:** Use **A** for v1 (`system_prompt_via = "prepend"`), with a clear delimiter:

```markdown
<!-- ODIN_PROTOCOL (injected; takes precedence for task termination and git policy) -->
... contents of build_system_prompt(branch) ...
<!-- END ODIN_PROTOCOL -->

## Context from previous task
...
---
(task body)
```

Long-term, if Cursor adds a system-prompt flag, switch via config without changing the loop.

### Autonomy / permissions parity

| Intent | Claude | Cursor |
|--------|--------|--------|
| Full autonomy (Odin default) | `--permission-mode bypassPermissions` | `--force` + `--trust` + `sandbox=disabled` |
| Restricted | `acceptEdits` / tool allowlists | `~/.cursor/cli-config.json` `permissions.allow/deny` + no `--force` |
| MCP auto-approve | default in bypass mode | `--approve-mcps` |

Document that Cursor tool allow/deny is **config-file driven**, not per-invocation flags like Claude.

---

## 6. Stream-json compatibility

Cursor and Claude both emit **NDJSON** with overlapping event types. Odin can reuse most of the loop; differences need normalisation.

### Terminal `result` event (what Odin parses)

**Claude** (today):

```json
{
  "type": "result",
  "subtype": "success",
  "stop_reason": "end_turn",
  "result": "...",
  "session_id": "...",
  "usage": { "input_tokens": 1, "output_tokens": 2, ... },
  "total_cost_usd": 0.04,
  "duration_ms": 1234,
  "duration_api_ms": 1200,
  "num_turns": 5
}
```

**Cursor** (observed):

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "result": "...",
  "session_id": "...",
  "usage": {
    "inputTokens": 10888,
    "outputTokens": 31,
    "cacheReadTokens": 5440,
    "cacheWriteTokens": 0
  },
  "duration_ms": 1333,
  "duration_api_ms": 1333,
  "request_id": "..."
}
```

**Gaps:**

| Field | Claude | Cursor | Odin action |
|-------|--------|--------|-------------|
| `stop_reason` | present | **absent** | Synthesise `"end_turn"` when `subtype == success` && `is_error == false` |
| `total_cost_usd` | present | **absent** | Store `null`; optional future: estimate from model + tokens |
| `num_turns` | present | absent | Store `null` |
| Token keys | snake_case | camelCase | Normalise in backend or extend `metrics._norm_usage` |

### Live display (stdout styling)

| Event | Claude (today) | Cursor |
|-------|----------------|--------|
| Init | `system/init` + session | Same |
| Assistant text | `assistant` + `message.content[].text` | Same |
| Tool activity | `tool_use` blocks inside `assistant` | Separate `tool_call` events (`readToolCall`, `writeToolCall`, …) |

Extend `_handle_event` (or add `CursorStreamRenderer`) to show Cursor tool lines:

- `tool_call` + `subtype: started` → `→ read path/to/file`
- Map `writeToolCall`, `shellToolCall` (if present), etc.

Without this, runs still **work** — only live output is quieter.

### Success detection (`RunResult.succeeded`)

Today:

```python
succeeded = (
    exit_code == 0
    and error is None
    and stop_reason in {"end_turn", "stop_sequence"}
    and bool(final_text)
)
```

Backend normalisation must ensure Cursor successes get a synthetic `stop_reason` so classification reaches `protocol.parse(final_text)`.

---

## 7. Target project setup: CLAUDE.md vs AGENTS.md

Odin's queue format is **unchanged**. Platform affects which **instruction file** the agent reads from project root.

| Platform | Primary instructions | Odin startup warning if missing |
|----------|---------------------|----------------------------------|
| Claude | `CLAUDE.md` | warn if no `CLAUDE.md` |
| Cursor | `AGENTS.md` (also reads `CLAUDE.md`, `.cursor/rules`) | warn if no `AGENTS.md` and no `.cursor/rules` |

### Cross-platform projects (recommended layout)

Use one workflow source of truth:

```
myproject/
├── AGENTS.md          # workflow rules (platform-neutral)
├── CLAUDE.md          # one line: "Follow AGENTS.md for workflow."
├── .cursor/rules/     # optional Cursor-specific scoped rules
└── queue/...
```

Or the inverse (workflow in `CLAUDE.md`, Cursor stub in `AGENTS.md`). Odin's `guide` should gain a topic **`agent-md`** describing both.

### What goes in the target instruction file (workflow only)

Same content as today's `examples/target-claude-md-snippet.md` workflow section:

- test-to-green before commit
- one branch for the batch (Odin injects branch name in protocol)
- no partial work — use `<<<NEEDS_INPUT>>>`
- don't manage the queue

The **sentinel protocol** stays injected by Odin (Claude via system prompt; Cursor via prepend).

### Cursor-specific notes for authors

- **`--force` is required** for headless edits — document in guide.
- **Git repo:** Cursor applies rules more reliably in a git repo (documented Cursor behaviour).
- **Permissions:** tune `~/.cursor/cli-config.json` for deny patterns (e.g. `Shell(rm:*)`).
- **Model:** set via config or `--model`; default is account-dependent (`agent about` shows current).

---

## 8. Metrics parity

Keep the existing JSONL schema. Add optional fields rather than breaking consumers:

```json
{
  "type": "task",
  "platform": "cursor",
  "cost_usd": null,
  "cost_usd_estimated": false,
  "tokens": { "input": 10888, "output": 31, "cache_read": 5440, "cache_creation": 0 },
  "agent_duration_ms": 1333,
  ...
}
```

Changes:

- Rename `claude_duration_ms` → **`agent_duration_ms`** in new records; accept both when reading (backward compatible).
- **`platform`** on task and run records.
- **`cost_usd`:** populate for Claude; `null` for Cursor until/unless the CLI exposes cost.
- Token normalisation: one internal shape; map per backend via config `usage_*` keys.

`odin metrics` reports should show platform breakdown when mixed data exists.

---

## 9. Lint and guide updates

| Component | Change |
|-----------|--------|
| `lint.py` | Generalise to `scan_project_instructions(path, platform)` — same git-conflict patterns, any markdown file |
| `cli._cmd_run` | Warn based on platform's expected instruction file |
| `guide.py` | Platform-aware intro; document `AGENTS.md`; `odin guide agent-md` |
| `contract.py` | Rename comment only; `build_system_prompt()` stays platform-neutral |
| Tests | Fake backend scripts per platform (mirror `tests/test_runner.py`) |

---

## 10. Implementation task queue (suggested Odin batches)

Break into small, reviewable tasks. Order matters.

### Batch A — Backend skeleton (no behaviour change)

1. **A1.** Add `src/odin/backends/` package: `base.py`, `claude.py`, `registry.py`.
2. **A2.** Move `run_claude` logic into `ClaudeBackend`; `runner.run_agent(..., platform=...)` delegates.
3. **A3.** Wire `--platform claude` (default); all existing tests green with zero diff in behaviour.
4. **A4.** Add `config.py` — load TOML from `~/.odin/config.toml` (stdlib `tomllib`); no TOML dependency (Python 3.11+).

### Batch B — Cursor backend

5. **B1.** Implement `CursorBackend.build_invoke` + `normalise_result` (synthetic `stop_reason`, token mapping).
6. **B2.** System prompt prepend path in invoke builder.
7. **B3.** Stream renderer for `tool_call` events.
8. **B4.** CLI: `--platform cursor`, `--agent-bin`, `--model`, `--force`, `--trust`, `--sandbox`, `--approve-mcps`.
9. **B5.** Tests with fake `agent` script emitting Cursor-shaped NDJSON.

### Batch C — Metrics + docs

10. **C1.** Metrics: `platform` field, `agent_duration_ms`, generalised `_norm_usage`.
11. **C2.** Guide + `examples/target-agents-md-snippet.md`.
12. **C3.** Update `CLAUDE.md` (Odin repo) architecture section.
13. **C4.** Demo project: optional `--platform cursor` instructions in readme.

### Batch D — Extensibility (when adding Kiro)

14. **D1.** Document backend plugin checklist in this file.
15. **D2.** Add `platforms.kiro` stub in example config only.

---

## 11. Backend implementer's checklist (Kiro, Codex, …)

To add a platform:

1. **Binary + headless flag** — e.g. `foo --print`, `foo -p`, `foo --headless`.
2. **Structured output** — prefer NDJSON stream with terminal `result` event; else wrap plain text.
3. **Prompt stdin vs argv** — implement `prompt_via` in config.
4. **System/protocol injection** — flag, prepend, or project file.
5. **Autonomy defaults** — match Odin's "unattended batch" posture.
6. **Normalise to `RunResult`** — especially `stop_reason`, `final_text`, `usage`, `cost_usd`.
7. **Stream display** — map tool events to one-line summaries (optional but nice).
8. **Instruction file** — what the agent loads from project root.
9. **Tests** — fake script + scenario env var pattern from `tests/test_runner.py`.

---

## 12. Risk register

| Risk | Mitigation |
|------|------------|
| Cursor `-p` hangs on some versions | Document minimum CLI version; integration test; clear timeout guidance (future) |
| No dollar cost from Cursor | `cost_usd: null` in metrics; show tokens + duration in banner |
| Protocol in user prompt ignored | Contract text already states precedence; monitor `failed/` for unparseable output |
| Tool permission models differ | Document Cursor `cli-config.json`; don't pretend `--allowed-tools` works cross-platform |
| `--max-turns` missing on Cursor | Omit flag; document circuit-breaker as Claude-only until Cursor adds it |
| Instruction file fragmentation | Guide cross-platform `AGENTS.md` + stub pattern |

---

## 13. Quick reference: run the same queue on Cursor

```sh
# One-time: install Cursor CLI, login, optional ~/.odin/config.toml
agent login
agent about

# In target project: add AGENTS.md with workflow rules (see §7)

# Run queue
cd ~/code/myproject
odin run add-feature --platform cursor --project . --branch add-feature --force --trust

# Or set default
export ODIN_PLATFORM=cursor
odin run add-feature
```

Equivalent bare `agent` invocation for one task (what Odin wraps):

```sh
cd ~/code/myproject
agent -p --output-format stream-json --force --trust --workspace . <<'EOF'
<!-- ODIN_PROTOCOL -->
(you are being run by Odin … sentinel rules …)
<!-- END ODIN_PROTOCOL -->

## Context from previous task
...

---
(task markdown body)
EOF
```

---

## 14. Open questions (decide before coding)

1. **Config format:** TOML only, or also support JSON? (Recommend TOML — stdlib `tomllib`, no deps.)
2. **Flag aliases:** Keep `--claude-bin` forever, or deprecate in favour of `--agent-bin` + `--platform`?
3. **Cost estimation:** Attempt list-price estimate from tokens + model for Cursor, or leave null until official?
4. **Project config v2:** Needed for monorepos with different models per project?
5. **Rename `CLAUDE.md` references in Odin repo docs** to "project instructions" in user-facing text?

---

## Appendix A — Side-by-side command mapping

| Concern | Claude Code | Cursor Agent |
|---------|-------------|--------------|
| Binary | `claude` | `agent` |
| Headless | `-p` | `-p` / `--print` |
| Stream JSON | `--output-format stream-json` | same |
| Verbose stream | `--verbose` | *(not needed)* |
| Full autonomy | `--permission-mode bypassPermissions` | `--force --trust` |
| System prompt | `--append-system-prompt` | prepend to prompt |
| Workspace | `--project` cwd | `--workspace` + cwd |
| Model | config / env | `--model` |
| Fresh session | no `--resume` | no `--continue` |
| Metrics cost | `total_cost_usd` | not available (v1) |
| Metrics tokens | snake_case | camelCase |

## Appendix B — Files to touch (implementation estimate)

| File | Change size |
|------|-------------|
| `src/odin/backends/*.py` | **new** |
| `src/odin/config.py` | **new** |
| `src/odin/runner.py` | refactor → thin wrapper |
| `src/odin/cli.py` | medium |
| `src/odin/metrics.py` | small |
| `src/odin/lint.py` | small |
| `src/odin/guide.py` | medium |
| `tests/test_runner.py` | extend |
| `tests/test_backends.py` | **new** |
| `tests/test_config.py` | **new** |
| `CLAUDE.md` | doc update |
| `examples/target-agents-md-snippet.md` | **new** |

---

*End of proposal.*
