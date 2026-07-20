# backends/ — the platform taxonomy

Every headless coding-agent product Odin can drive is an `AgentBackend`. The generic loop
(`runner.run_agent`) treats **all platforms as peers** — there is no first-class backend. This subpackage
holds the *contract* (`base.py`), the *resolver* (`registry.py`), and the *peer implementations*
(`claude.py`, `cursor.py`, `grok.py`). It's a **pattern-instance family**: adding a platform = adding one
peer — see [`SKILL.md`](SKILL.md).

## The contract (`base.py`)

`AgentBackend` (ABC) — five methods every peer implements:

| Method | Returns / does |
|--------|----------------|
| `default_binary()` | the CLI name (`claude` / `agent` / `grok`) |
| `instruction_files()` | which target files carry workflow rules (`CLAUDE.md`, `AGENTS.md`, …) — used by `lint` and the missing-instruction warning |
| `build_invoke(prompt, project_dir, system_prompt, run_options)` | an **`AgentInvokeSpec`**: `argv`, `prompt`, `cwd`, `prompt_via` (`"stdin"` \| `"file"`), `prompt_file_flag`. Where protocol injection + flag mapping happen. |
| `handle_stream_event(event, out, project_dir)` | interpret one NDJSON event → live display + `CapturedFields` (terminal marker, text deltas, session/usage/cost) |
| `normalise_result(terminal_event, exit_code, wall_ms, stderr, accumulated_text)` | build the `RunResult` — **owns the success gate** |

Value types (also `base.py`):
- **`AgentInvokeSpec`** — `argv`, `prompt`, `cwd`, `prompt_via="stdin"|"file"`, `prompt_file_flag="--prompt-file"`.
- **`RunOptions`** (frozen, platform-agnostic knobs) — `binary`, `model`, `permission_mode`, `allowed_tools`,
  `disallowed_tools`, `max_turns`, `sandbox`, `approve_mcps` (last two tri-state, Cursor-oriented).
- **`CapturedFields`** — what `handle_stream_event` hands back (terminal flag, `text_delta`, session id, usage, cost).

## The registry (`registry.py`)

`get_backend(name) -> AgentBackend` is the **single** resolution point. Case-insensitive; an unknown name is a
**hard `ValueError`** (no silent fallback). `available_platforms()` lists the registered peers. To add a
platform, add **one** entry to `_BACKENDS` (name → class). The *default-product* choice lives in
`config.resolve_platform`, not here.

## The three peers

| | Claude (`claude`) | Cursor (`agent`) | Grok (`grok`) |
|---|---|---|---|
| Invoke | `claude -p --output-format stream-json` | `agent` + `--force`/`--trust`/`--workspace` | `grok` |
| Protocol injection | `--append-system-prompt` | **prepend** to stdin prompt (no flag) | `--rules` |
| Prompt delivery | stdin | stdin | **temp file** (`prompt_via="file"`) |
| Assistant text | `assistant`/`result` events | events | `{type:text}` **chunk deltas** (accumulated) |
| Terminal event | `result` (`stop_reason`) | terminal event (no `stop_reason`) | `{type:end}` (camelCase `stopReason`/`sessionId`) or `{type:error}` |
| Success gate | exit 0 + no error + good `stop_reason` + text | exit 0 + terminal-event present + text | exit 0 + terminal present + not-error + text |
| Metrics | `usage`, `total_cost_usd`, `num_turns` | camelCase usage; cost/turns **not reported** (None) | `usage` (snake, no cache_creation) + `num_turns` + `modelUsage`; `total_cost_usd` **only when complete** (often None on cheap calls) |
| Per-platform config | none | `[platforms.cursor]` (binary/sandbox/approve_mcps), lazy | `[platforms.grok]`, lazy |

## Notes for backend authors

- **Shared render helpers.** `runner.py` exports underscore-prefixed helpers (`_assistant_text`,
  `_render_agent_text`, `_write_tool_line`, `_abbrev_path`, `_short_session`, `_truncate`, `_safe_write`) that
  claude/cursor reuse (grok reuses `_safe_write`). They're an *informal shared toolkit* a new backend **may**
  reuse — convenient, but underscore-prefixed (no stability guarantee); a backend may also render its own.
- **Per-platform config is as-needed**, not required: `claude` reads no config; `cursor`/`grok` read a
  `[platforms.<name>]` section (lazy `from odin import config`) only because they have extra CLI knobs.
- **Cross-refs (source of truth — don't duplicate):** invoke details in `docs/agent-backends.md`; design +
  the backend implementer's checklist in `docs/multi-platform-agents-proposal.md` §11.
