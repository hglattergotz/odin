# Multi-platform agent backends for Odin

**Status:** Proposal (branch `cursor`, PR #1) — **approved to lock** (round-3 review, commit `8fb2e2e` + R3 clarifications below)  
**Goal:** Let Odin orchestrate tasks through multiple headless agent CLIs — starting with **Claude Code** (`claude`) and **Cursor Agent** (`agent`), with a path to **Kiro** and others — without degrading existing behaviour.

This document is written to be split into an Odin task queue. It assumes you have read `CLAUDE.md` in this repo and understand how Odin works today.

**Round-1 review:** A Claude-side reviewer verified repo-truth items against source; a Cursor-side pass ran the empirical verification checklist (Appendix C) against real `agent` output. Both are incorporated below.

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
| Subprocess loop | `src/odin/runner.py` | `run_claude()` hard-codes argv; reads prompt on stdin; drains stderr on a daemon thread; computes `succeeded` at `:174-179` |
| argv / system-prompt flag | `src/odin/runner.py:87-88` | Passes `--append-system-prompt` when `system_prompt` is set (wired from `cli.py`) |
| Protocol *text* | `src/odin/contract.py:16-17,21,24,107` | `_BASE` / `_BRANCH` **runtime strings** hard-code "this project's **CLAUDE.md**" — wrong for Cursor (`AGENTS.md`) |
| CLI invoke site | `src/odin/cli.py:630` | Single call site to `run_claude`; dry-run hard-codes `"claude -p"` at `:621`; early-returns after first task at `:626` |
| CLI flags | `src/odin/cli.py` | `--claude-bin`, `--permission-mode`, `--allowed-tools`, `--disallowed-tools`; **no `--model` today** |
| Startup instruction warning | `src/odin/cli.py:313-318` | Always warns if `{project}/CLAUDE.md` missing — fires on AGENTS.md-only Cursor projects |
| Git-workflow lint hook | `src/odin/cli.py:747-761` | `_warn_claude_md_conflicts` reads `project / "CLAUDE.md"` literally; no-ops silently for Cursor |
| Metrics | `src/odin/metrics.py:207,244` | `claude_duration_ms`, `claude_api_ms` field names; run summary `cost_usd_total` sums only non-null task costs → **0.0** when all null |
| Startup lint | `src/odin/lint.py` | Scans `CLAUDE.md` only |
| Guide / docs | `src/odin/guide.py:139`, README | Claude-centric wording; guide protocol section generated from `build_system_prompt(None)` |
| Success criteria | `runner._GOOD_STOPS` (`runner.py:49`) | Requires `stop_reason ∈ {end_turn, stop_sequence}` — **Cursor does not emit `stop_reason`** |
| Stale display strings | `runner.py:5-6,170` | Docstring lists `--max-turns` in baseline argv (stale vs `:82-86`); stderr banner reads `[claude stderr]` |

The queue model, sentinel protocol, carry-context, held/resume flow, follow-ups, git startup, terminal signalling, and metrics *shape* are all platform-agnostic. Only the **agent backend** layer is Claude-specific.

---

## 2. Design principle: backends, not forks

Introduce a small **AgentBackend** interface. Odin's task loop stays the same; each platform implements invoke-building, stream interpretation, and result normalisation.

### Refactor scope (explicit)

**Stays generic in `runner.py`:**

- Subprocess exec (`Popen`), stdin write, concurrent stderr drain (daemon thread)
- NDJSON line loop, wall-clock timing
- Best-effort stream display dispatch to the active backend

**Moves into per-platform backends:**

- `build_invoke(...)` — argv + final prompt text (incl. prepend vs flag injection)
- `handle_stream_event(event)` — live terminal rendering (tool lines differ)
- `normalise_result(terminal_event, exit_code, wall_ms) -> RunResult` — token/cost/stop_reason/succeeded

Do **not** move the whole subprocess loop into `ClaudeBackend`; only the platform-specific pieces.

### Types

```python
@dataclass(frozen=True)
class AgentInvokeSpec:
    argv: list[str]           # binary + flags (not including prompt arg when using stdin)
    prompt: str               # text sent on stdin
    cwd: Path

@dataclass(frozen=True)
class RunResult:              # already exists — extend carefully (frozen dataclass)
    succeeded: bool
    final_text: str
    stop_reason: str | None
    error: str | None
    exit_code: int
    session_id: str | None
    platform: str = "claude"  # NEW — must have a default (non-default after defaulted fields breaks dataclass)
    wall_ms: int = 0
    duration_ms: int | None = None
    api_ms: int | None = None
    num_turns: int | None = None
    usage: dict | None = None
    cost_usd: float | None = None
```

`RunResult` is `@dataclass(frozen=True)` today (`runner.py:31`). Backends must **construct fresh** instances; never mutate.

### Success gate placement

Today `succeeded` is computed inside the runner (`runner.py:174-179`) *after* stream parsing. For Cursor, **`stop_reason` is absent on success**, so a naive gate would route every Cursor success to `failed/`.

**Recommendation:** move success classification into `normalise_result`. The runner calls the backend and trusts `RunResult.succeeded`. Backends may still populate a synthetic `stop_reason` for metrics/debug, but **`succeeded` must not depend on faking `stop_reason` through the old gate**.

**Concrete predicates (pin before coding):**

- **ClaudeBackend:** `succeeded = (exit_code == 0 and error is None and stop_reason in {"end_turn", "stop_sequence"} and bool(final_text))` — same logic as today (`runner.py:174-179`), just relocated.
- **CursorBackend:** `succeeded = (exit_code == 0 and terminal_result_present and event.get("is_error") is not True and bool(final_text))` — missing terminal `result` or non-zero exit → failure; **missing `is_error` counts as not-an-error** (consistent with `runner.py:254`). Do not require `stop_reason`.

Each backend provides:

- **`build_invoke(prompt, project_dir, system_prompt, run_options) -> AgentInvokeSpec`**
- **`handle_stream_event(event, out, project_dir) -> CapturedFields | None`**
- **`normalise_result(terminal_event | None, exit_code, wall_ms, stderr) -> RunResult`**
- **`default_binary() -> str`**
- **`instruction_files() -> list[Path]`** — for startup warnings / lint

Odin selects a backend from **`--platform`** (or config). The existing Claude path becomes `ClaudeBackend`; Cursor becomes `CursorBackend`.

**Non-goals for v1:** session resume across tasks (both CLIs support `--resume`; Odin keeps fresh session per task), cloud/SDK runtimes (local CLI only).

---

## 3. CLI surface

### Platform selector

```
odin run [QUEUE] --platform {claude,cursor} ...
```

Resolution order:

1. `--platform` on the command line
2. `$ODIN_PLATFORM`
3. `default_platform` in config (see §4)
4. Fallback: `claude` (preserves today's behaviour)

### Model selector (all platforms)

Today Odin passes **no `--model`** to any CLI (verified: zero `model` handling in `src/odin/*.py`). Add a **platform-agnostic** flag:

```
odin run ... --model MODEL
```

Resolution order:

1. `--model` on the command line
2. `$ODIN_MODEL`
3. `platforms.<platform>.model` in config
4. Unset → platform CLI default (today's behaviour; no flag emitted)

Maps to `claude --model …` and `agent --model …` when set.

### Platform-specific flags

Keep Claude flags working unchanged when `--platform claude` (or default). Add Cursor equivalents; warn when a flag is meaningless for the active platform.

| Odin flag | Claude mapping | Cursor mapping |
|-----------|----------------|----------------|
| `--claude-bin` | `claude` binary | *(ignored)* |
| `--agent-bin` *(new)* | *(ignored)* | `agent` binary |
| `--permission-mode` | `--permission-mode` | *(no direct equivalent)* |
| `--allowed-tools` | `--allowed-tools` | *(no CLI flag — `~/.cursor/cli-config.json`)* |
| `--disallowed-tools` | `--disallowed-tools` | *(same)* |
| `--max-turns` | `--max-turns` | *(not exposed — no-op with warning)* |
| `--model` | `--model` | `--model` |
| `--force` | implied by `bypassPermissions` default | `--force` / `--yolo` |
| `--trust` | N/A | `--trust` (recommended for headless) |
| `--sandbox` | N/A | `--sandbox enabled\|disabled` |
| `--approve-mcps` | N/A | `--approve-mcps` |

**Flag aliases (decision B4):** keep `--claude-bin` **forever** for backward compatibility. Add `--agent-bin` + `--platform`; do not deprecate in v1.

### Dry-run output

Dry-run (`cli.py:621-626`) must:

- Source argv from `backend.build_invoke(...)` — not hard-code `"claude -p"`
- Print resolved **platform**, argv, and prompt length
- Preserve today's behaviour: preview **one** task and return (early exit)

### New subcommand: `odin config`

Interactive config setter (see §4). Added to subcommand list: `{run,status,resume,demo,guide,archive,metrics,config}`.

---

## 4. Configuration file

**Location:** `$ODIN_HOME/config.toml` (default `~/.odin/config.toml`). Override with `$ODIN_CONFIG`.

**Read:** on every `odin run` (merge with CLI flags; flags win).

**Write:** **only** via explicit `odin config` — never silent auto-scaffolding during `odin run`. This adds a second Odin write surface beside metrics; update Odin repo `CLAUDE.md` "one write" rule to enumerate (1) metrics telemetry and (2) user-initiated config writes.

Rationale: platform binaries, models, permission posture, and future Kiro settings differ per user/machine. The owner does not want to hand-edit TOML.

### Example config

```toml
# ~/.odin/config.toml
# Written by `odin config` — hand-edited changes may be overwritten on next `odin config set`.

default_platform = "claude"

# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------
[platforms.claude]
binary = "claude"
permission_mode = "bypassPermissions"
output_format = "stream-json"
verbose = true
# model = "claude-sonnet-4-6"   # unset = claude CLI default (no --model passed)

[platforms.claude.invoke]
prompt_via = "stdin"
system_prompt_via = "append_system_flag"   # --append-system-prompt TEXT

[platforms.claude.metrics]
usage_input = "input_tokens"
usage_output = "output_tokens"
usage_cache_read = "cache_read_input_tokens"
usage_cache_write = "cache_creation_input_tokens"
cost_field = "total_cost_usd"

# ---------------------------------------------------------------------------
# Cursor Agent CLI
# ---------------------------------------------------------------------------
[platforms.cursor]
binary = "agent"
output_format = "stream-json"
force = true
trust = true
approve_mcps = true
sandbox = "disabled"
# model = "composer-2.5-fast"   # unset = account default

[platforms.cursor.invoke]
prompt_via = "stdin"
system_prompt_via = "prepend"

[platforms.cursor.metrics]
usage_input = "inputTokens"
usage_output = "outputTokens"
usage_cache_read = "cacheReadTokens"
usage_cache_write = "cacheWriteTokens"
cost_field = ""                # empty = no cost field; store null

# ---------------------------------------------------------------------------
# Future: Kiro (placeholder)
# ---------------------------------------------------------------------------
# [platforms.kiro]
# binary = "kiro"
```

### `odin config` command (interactive setter)

**Motivation:** `/model`-style UX without editing TOML by hand.

| Command | Behaviour |
|---------|-----------|
| `odin config` | Interactive menu (TTY): pick `default_platform`; per-platform settings (model, binary, autonomy posture) |
| `odin config show` | Print effective config (merged with env hints) |
| `odin config get KEY` | e.g. `platforms.claude.model` |
| `odin config set KEY VALUE` | Non-interactive for CI/scripts, e.g. `odin config set platforms.claude.model claude-sonnet-4-6` |

**Model picker UX:** curated list per platform + **"Other (type a value)"** + **"Use platform default (unset)"** — model IDs drift; free-text required.

**Implementation constraints:**

- Reuse stdlib interactive infra in `src/odin/prompts.py` (branch selector / held Q&A pattern); TTY-gated like resume.
- **`tomllib` is read-only** (Python 3.11+) — implement a **minimal hand-rolled TOML writer** for the flat/small schema. **No new dependencies** (`tomli-w` forbidden per supply-chain rules).
- Write **atomically** (`write temp → rename`).
- Merge updates: don't clobber keys the command didn't touch. Document that round-tripping may drop user comments in the file.

**First-run:** `odin config` may create `~/.odin/` and an empty/minimal config on first interactive use; no implicit creation during `odin run`.

### Project-level overrides (optional v2)

`--project/.odin/config.toml` merged over user config. Defer unless needed.

---

## 5. Cursor CLI: equivalent invocation

Empirically verified on **`agent` CLI `2026.06.04-5fd875e`** (see Appendix C). Headless mode is **`agent -p`** (`--print`), not the `cursor` editor binary.

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

Set **`cwd`** and **`--workspace`** to `--project`. Verified: `AGENTS.md` is found with `--workspace` set; also works with cwd alone when stdin is piped from that directory (Appendix C.6).

**Do not** pass `--continue` / `--resume` — fresh session per task.

### Prompt delivery

Both CLIs accept **stdin** when no prompt arguments are given (verified). Keep Odin's stdin path.

### System / protocol injection

Claude (`runner.py:87-88`):

```
claude -p ... --append-system-prompt "<contract>"
# stdin = task prompt (+ carry context)
```

Cursor: **no `--append-system-prompt`** (verified: `agent -p --help | grep -i system` matches only `about … system` — Appendix C.7).

| Approach | Pros | Cons |
|----------|------|------|
| **A. Prepend contract to prompt** *(v1)* | Works immediately; no project file changes | More input tokens; not true system role |
| B. Sidecar file + "read first" | Cleaner separation | Agent may skip |
| C. Require protocol in `AGENTS.md` | Persistent | Drift vs `contract.py` |

**Recommendation:** **A** with delimiters:

```markdown
<!-- ODIN_PROTOCOL (injected; takes precedence for task termination and git policy) -->
... build_system_prompt(branch, instructions_name=...) ...
<!-- END ODIN_PROTOCOL -->

## Context from previous task
...
---
(task body)
```

### Protocol text must be platform-neutral (review A1)

`contract.build_system_prompt()` must stop naming `CLAUDE.md` in injected runtime strings. Parameterise, e.g.:

- `instructions_name="project instructions"` (generic), or
- per-platform: `"CLAUDE.md"` / `"AGENTS.md"`

Also affects `odin guide` output (`guide.py:139` calls `build_system_prompt(None)`).

### Autonomy / permissions parity

| Intent | Claude | Cursor |
|--------|--------|--------|
| Full autonomy (Odin default) | `--permission-mode bypassPermissions` | `--force` + `--trust` + `sandbox=disabled` + `--approve-mcps` |
| Restricted | `acceptEdits` / tool allowlists | `~/.cursor/cli-config.json` + no `--force` |

Appendix C.5: on the test system, `--force --trust` allowed file edits + shell without blocking in piped/non-TTY mode. **Still pass both by default** — behaviour may differ by Cursor version and account policy; a hung trust/MCP prompt blocks Odin forever (no timeout).

---

## 6. Stream-json compatibility

### Terminal `result` event

**Claude:**

```json
{
  "type": "result",
  "subtype": "success",
  "stop_reason": "end_turn",
  "result": "...",
  "usage": { "input_tokens": 1, "output_tokens": 2 },
  "total_cost_usd": 0.04,
  "duration_ms": 1234,
  "duration_api_ms": 1200,
  "num_turns": 5
}
```

**Cursor (verbatim, Appendix C.3):**

```json
{
  "type": "result",
  "subtype": "success",
  "duration_ms": 4993,
  "duration_api_ms": 4993,
  "is_error": false,
  "result": "pong",
  "session_id": "7bfeef23-655d-4191-bdf4-61b9bdc0621f",
  "request_id": "0cc5c6e7-6e8e-4035-851c-d888633106d8",
  "usage": {
    "inputTokens": 10760,
    "outputTokens": 46,
    "cacheReadTokens": 448,
    "cacheWriteTokens": 0
  }
}
```

**Confirmed absent on Cursor success:** `stop_reason`, `total_cost_usd`, `num_turns`.

**Error paths (Appendix C.4):**

- Invalid model: **exit 1**, error text on **stderr only**, **no** terminal `result` event → Odin treats as failure (same as Claude silence).
- Tool/shell failures inside a run: may still end with `subtype: success`, `is_error: false`, exit 0 (e.g. `exit 42` shell command) — task outcome is still driven by **sentinel parse**, not subprocess/tool exit codes.

### Field normalisation

| Field | Claude | Cursor | Odin action |
|-------|--------|--------|-------------|
| `stop_reason` | present | absent | Optional synthetic value for logs; **`succeeded` set by backend** |
| `is_error` | not relied on (success driven by `stop_reason` / `subtype`) | present on success (`false`) | Normalisation: **missing = not-an-error** (consistent with `runner.py:254`) |
| `total_cost_usd` | present | absent | `cost_usd: null` |
| Token keys | snake_case | camelCase | Map via config `usage_*` keys |
| `thinking` events | N/A | emitted in stream | Ignore for display (Cursor docs: suppressed in some modes; observed in tests) |

### Live display

| Event | Claude | Cursor |
|-------|--------|--------|
| Init | `system/init` | Same |
| Assistant text | `assistant` + `message.content[].text` | Same |
| Tool activity | `tool_use` inside `assistant` | `tool_call` started/completed (`readToolCall`, `editToolCall`, `shellToolCall`, …) |

Extend backend stream handler for Cursor tool lines. Runs work without this; output is just quieter.

---

## 7. Target project setup: CLAUDE.md vs AGENTS.md

| Platform | Primary instructions | Odin startup warning if missing |
|----------|---------------------|----------------------------------|
| Claude | `CLAUDE.md` | warn if no `CLAUDE.md` |
| Cursor | `AGENTS.md` (+ `.cursor/rules`) | warn if no `AGENTS.md` **and** no `.cursor/rules/` |

Implement in **`cli.py:313-318`** (platform-aware) and **`cli.py:747-761`** (scan the platform's instruction file, not always `CLAUDE.md`).

### Cross-platform layout

```
myproject/
├── AGENTS.md          # workflow rules (platform-neutral)
├── CLAUDE.md          # "Follow AGENTS.md for workflow."
├── .cursor/rules/     # optional scoped Cursor rules
└── queue/...
```

Add `odin guide agent-md` topic and `examples/target-agents-md-snippet.md`.

---

## 8. Metrics parity

Keep JSONL schema; add fields; stay backward compatible on read.

```json
{
  "type": "task",
  "platform": "cursor",
  "cost_usd": null,
  "tokens": { "input": 10760, "output": 46, "cache_read": 448, "cache_creation": 0 },
  "agent_duration_ms": 4993,
  "agent_api_ms": 4993,
  ...
}
```

Changes:

- Rename **`claude_duration_ms` → `agent_duration_ms`** and **`claude_api_ms` → `agent_api_ms`** in new records; accept both names when reading.
- **`platform`** on task and run records.
- **Task `cost_usd`:** Claude populates; Cursor `null`.
- **Run `cost_usd_total` (decision B2):** when no task recorded a numeric cost, write **`null`**, not **`0.0`**. This touches **two sites:** `record_task` accumulates into `cost_total` (initialised `0.0` at `metrics.py:167`; only adds when cost is numeric at `:181-182`) and `finish` emits `cost_usd_total` (`metrics.py:244`). Add an `_any_cost: bool` flag in `record_task`; set true when any task yields a numeric `cost_usd`. In `finish`, emit **`null`** when `_any_cost` is false — `finish` alone cannot distinguish "no costs seen" from "costs summed to 0.0".
- **Token normalisation:** backends map platform-specific usage keys to Odin's **internal** token dict — the same keys `_norm_usage` / `_TOKEN_KEYS` use today: `input`, `output`, `cache_read`, `cache_creation` (`metrics.py:45,110-117`). The `[platforms.*.metrics]` config maps *from* CLI field names *to* those four internal names so mixed-platform `odin metrics` aggregation sums correctly (R3-5).

`odin metrics`: platform breakdown when mixed data exists.

---

## 9. Lint, contract, and guide updates

| Component | Change |
|-----------|--------|
| `contract.py` | **Parameterise instruction-file name** in `_BASE` / `_BRANCH` runtime strings; `build_system_prompt(branch, platform=…)` |
| `lint.py` | `scan_project_instructions(path, platform)` — same git-conflict patterns |
| `cli._cmd_run` | Platform-aware missing-instruction warning (`:313-318`) |
| `cli._setup_branch` | Platform-aware `_warn_claude_md_conflicts` → rename/generalise (`:747-761`) |
| `guide.py` | Platform-aware intro; `odin guide agent-md`; protocol section uses parameterized contract |
| `runner.py` | Generic loop + backend dispatch; fix stale docstring (`:5-6`); generic stderr label (`:170`) |
| Tests | Fake backend scripts per platform (`tests/test_runner.py` pattern) |

---

## 10. Implementation task queue (reordered)

**Rule:** every batch ends with the **full test suite green**. No batch leaves a broken build.

### Batch A — Backend skeleton + config (no behaviour change)

1. **A1.** Add `src/odin/backends/`: `base.py`, `claude.py`, `registry.py`.
2. **A2.** Add `config.py` — load TOML via stdlib `tomllib`; resolution for platform + model.
3. **A2b.** Add `odin config` — interactive + `set`/`get`/`show`; hand-rolled TOML writer; `tests/test_config.py`.
4. **A3.** Refactor `runner.py`: generic subprocess loop; `ClaudeBackend` owns argv/event/result; **`run_agent(..., backend=...)`**. **Also switch `cli.py:630` to the default `ClaudeBackend`** so tests stay green before A4 lands.
5. **A4.** Wire `--platform claude` (default), `--model`; all existing tests green — zero behaviour diff.

### Batch B — Cursor backend

6. **B1.** `CursorBackend.build_invoke` + `normalise_result` using the **pinned Cursor success predicate** (§2).
7. **B2.** System prompt prepend in invoke builder.
8. **B3.** Stream renderer for `tool_call` events.
9. **B4.** CLI: `--platform cursor`, `--agent-bin`, Cursor autonomy flags.
10. **B5.** Tests with fake `agent` script (Cursor-shaped NDJSON).

### Batch C — Cross-platform polish

11. **C1.** Metrics: `platform`, `agent_duration_ms`, `agent_api_ms`, null `cost_usd_total`.
12. **C2.** **`contract.py` instruction-file parameterisation** + guide regeneration path.
13. **C3.** Platform-aware `cli.py` warnings (`:313`, `:747`) + generalised `lint.py`.
14. **C4.** Guide + `examples/target-agents-md-snippet.md`; update Odin repo `CLAUDE.md` architecture + metrics write-surface wording.
15. **C5.** Dry-run uses `backend.build_invoke` (fix `:621-626`).
16. **C6.** Demo fixture (**decision B3**): **keep demo Claude-only for v1**; document `odin run --platform cursor` in demo readme as manual step; optional future `AGENTS.md` variant in `otest`.

### Batch D — Extensibility (Kiro, …)

17. **D1.** Backend implementer's checklist (§11).
18. **D2.** `platforms.kiro` stub in example config.

---

## 11. Backend implementer's checklist (Kiro, Codex, …)

1. Binary + headless flag (`-p`, `--print`, …)
2. Structured output — prefer NDJSON + terminal `result`
3. Prompt stdin vs argv
4. System/protocol injection — flag, prepend, or project file
5. Autonomy defaults for unattended batches
6. **`normalise_result` → `RunResult` with correct `succeeded`**
7. Stream display (optional)
8. Instruction file(s) for startup warnings
9. Fake-script tests

---

## 12. Risk register

| Risk | Mitigation |
|------|------------|
| Cursor `-p` hangs on some versions | Document min CLI version; Appendix C as baseline |
| No dollar cost from Cursor | `cost_usd: null`; show tokens + duration in banner |
| Prepend protocol ignored | Contract states precedence; monitor `failed/` unparseable rate |
| Tool permission models differ | Document Cursor `cli-config.json`; no fake `--allowed-tools` parity |
| `--max-turns` Claude-only | No-op + warning on Cursor |
| Pre-startup errors without `result` event | Non-zero exit + stderr → `failed/` (same as today) |
| Hung trust/MCP prompt | Default `--trust --approve-mcps`; document requirement |
| TOML writer drops comments | Document; only `odin config` writes |

---

## 13. Quick reference: run on Cursor

```sh
agent login
agent about

# Set defaults interactively
odin config

cd ~/code/myproject   # needs AGENTS.md (or .cursor/rules)
odin run add-feature --platform cursor --branch add-feature

# Or env defaults
export ODIN_PLATFORM=cursor
export ODIN_MODEL=composer-2.5-fast
odin run add-feature
```

---

## 14. Open questions

1. **Config format:** TOML only? *(Recommend yes — stdlib read, hand-rolled write.)*
2. **`$ODIN_MODEL` env tier:** included above — confirm?
3. **`odin config` model validation:** accept any string vs validate against `agent models` / `claude` list?
4. **`odin config init`:** separate command vs create-on-first-`odin config`?
5. **Cost estimation:** derive Cursor `$` from tokens + model, or stay null until CLI exposes cost?
6. **Project config v2:** monorepo overrides needed?
7. **Demo `AGENTS.md` variant:** defer (B3) or ship in v1?

---

## Appendix A — Side-by-side command mapping

| Concern | Claude Code | Cursor Agent |
|---------|-------------|--------------|
| Binary | `claude` | `agent` |
| Headless | `-p` | `-p` / `--print` |
| Stream JSON | `--output-format stream-json` | same |
| Verbose | `--verbose` | not needed |
| Full autonomy | `--permission-mode bypassPermissions` | `--force --trust` |
| System prompt | `--append-system-prompt` (`runner.py:87-88`) | prepend to prompt |
| Workspace | cwd | `--workspace` + cwd |
| Model | `--model` (new in Odin) | `--model` |
| Fresh session | no `--resume` | no `--continue` |
| Cost in result | `total_cost_usd` | absent |
| Tokens | snake_case | camelCase |
| `stop_reason` | present | absent |

## Appendix B — Files to touch

| File | Change |
|------|--------|
| `src/odin/backends/*.py` | **new** |
| `src/odin/config.py` | **new** (read + hand-rolled write) |
| `src/odin/runner.py` | refactor — generic loop, backend dispatch, stderr label |
| `src/odin/cli.py` | medium — platform, model, config subcommand, warnings, dry-run |
| `src/odin/contract.py` | **parameterise instruction-file strings** |
| `src/odin/metrics.py` | rename fields, null total cost |
| `src/odin/lint.py` | generalise |
| `src/odin/guide.py` | platform topics |
| `src/odin/prompts.py` | reuse for `odin config` menus |
| `tests/test_runner.py` | extend |
| `tests/test_backends.py` | **new** |
| `tests/test_config.py` | **new** |
| `CLAUDE.md` | architecture + write surfaces |
| `examples/target-agents-md-snippet.md` | **new** |

## Appendix C — Empirical verification (Cursor system)

Environment: macOS, `agent` **`2026.06.04-5fd875e`**, logged in (Team). Run 2026-06-09.

### C.1 Binary, version, flags

```
$ agent --version
2026.06.04-5fd875e
```

Relevant flags confirmed on `agent -p --help`: `--output-format`, `--model`, `--force`/`--yolo`, `--trust`, `--workspace`, `--sandbox`, `--approve-mcps`. No system-prompt flag.

### C.2 stdin prompt, piped, stream-json

```bash
printf 'Reply with the single word: pong' | agent -p --output-format stream-json --force --trust --workspace "$PWD"
```

Exit 0. Full stream ended with terminal `result` (C.3).

### C.3 Terminal `result` event (verbatim)

```json
{"type":"result","subtype":"success","duration_ms":4993,"duration_api_ms":4993,"is_error":false,"result":"pong","session_id":"7bfeef23-655d-4191-bdf4-61b9bdc0621f","request_id":"0cc5c6e7-6e8e-4035-851c-d888633106d8","usage":{"inputTokens":10760,"outputTokens":46,"cacheReadTokens":448,"cacheWriteTokens":0}}
```

**Absent:** `stop_reason`, `total_cost_usd`, `num_turns`. **Present:** `is_error: false`, `duration_ms`, `duration_api_ms`, camelCase `usage`.

### C.4 Non-happy paths

| Case | Terminal `result`? | Exit code |
|------|-------------------|-----------|
| Invalid `--model totally-invalid-model-xyz` | **No** — stderr only: `Cannot use this model: … Available models: …` | **1** |
| Agent completes but shell tool returns 42 | **Yes** — `subtype: success`, `is_error: false` | **0** |
| Fictional tool request (agent explains) | **Yes** — `subtype: success` | **0** |

Odin must treat **missing terminal `result` + non-zero exit** as failure (already true for Claude).

### C.5 Full autonomy (non-TTY)

```bash
printf 'Create autonomy-test.txt … then cat …' | agent -p --output-format stream-json --force --trust --sandbox disabled --approve-mcps --workspace "$PWD"
```

Exit 0. File created; `editToolCall` + `shellToolCall` completed without blocking prompts.

### C.6 Instruction files

Project with `AGENTS.md` containing `# test agents`. Prompt: "What is the exact first heading line in AGENTS.md?"

- With `--workspace "$PWD"`: agent `readToolCall` on `AGENTS.md`; answered `# test agents`.
- Without `--workspace` but cwd set: same behaviour.

Recommend still passing **`--workspace`** explicitly for parity with Odin's `--project`.

### C.7 System prompt mechanism

```bash
$ agent -p --help 2>&1 | grep -i system
  about [options]              Display version, system, and account information
```

No `--append-system-prompt` or equivalent → **prepend design validated**.

---

*End of proposal.*
