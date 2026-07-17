# Agent backends: how Odin invokes Claude Code and grok-build

Odin drives a queue of tasks through a **headless agent CLI**, one fresh session
per task. Which CLI it uses is a **backend**, selected with `--platform`. This
doc describes how each backend is invoked and how to add more. For the design
rationale and the broader roadmap (config file, `--model`, Cursor, Kiro), see
[`multi-platform-agents-proposal.md`](multi-platform-agents-proposal.md).

## Selecting a backend

```
odin run <queue>                     # claude (default — unchanged behaviour)
odin run <queue> --platform grok     # grok-build
ODIN_PLATFORM=grok odin run <queue>  # via environment
```

Resolution order: **`--platform` flag → `$ODIN_PLATFORM` → `claude`**. Anything
other than `claude`/`grok` is rejected by argument parsing.

Binaries default to `claude` / `grok` on `PATH`; override with `--claude-bin` /
`--grok-bin`.

## Prerequisites

| Backend | Binary | Auth |
|---------|--------|------|
| `claude` | `claude` (Claude Code) on `PATH` | Claude Code logged in |
| `grok` | `grok` (grok-build) on `PATH` | `grok login`, or `XAI_API_KEY` for CI/headless |

## What Odin runs under the hood

The generic run loop (`src/odin/runner.py:run_agent`) is platform-neutral —
spawn, prompt delivery, concurrent stderr drain, NDJSON parsing, live display,
wall timing. Each backend supplies only `build_cmd`, `handle_event`, and
`succeeded`.

### Claude backend

```
claude -p --output-format stream-json --verbose \
  --permission-mode <mode> \
  [--max-turns N] [--append-system-prompt <protocol>] \
  [--allowed-tools <csv>] [--disallowed-tools <csv>]
# prompt is written to the child's STDIN
```

- Terminal event: `{"type":"result", ...}` — `result` (final text), `stop_reason`,
  `usage` (snake_case), `total_cost_usd`, `session_id`.
- Success: exit 0, no error, `stop_reason ∈ {end_turn, stop_sequence}`, non-empty text.

### grok backend

```
grok --output-format streaming-json \
  --permission-mode <mode> \
  --prompt-file <tmp> \
  [--max-turns N] [--rules <protocol>] \
  [--tools <csv>] [--disallowed-tools <csv>]
# grok does NOT read stdin — the prompt is written to a temp file passed via
# --prompt-file, which Odin deletes after the run
```

- Assistant text arrives as `{"type":"text","data":"…"}` **chunk deltas**, which
  Odin concatenates into the final text (so `protocol.parse` can find the
  sentinel).
- Terminal event: `{"type":"end", ...}` — `stopReason`/`sessionId` (camelCase),
  `usage.*` (snake_case; **no `cache_creation` field**), `total_cost_usd`,
  `num_turns`.
- Failure: non-zero exit, a `{"type":"error","message":"…"}` line, or a missing
  terminal `end`.
- Success: exit 0, no error, a terminal `end` event, non-empty text. (grok's
  `stopReason` values differ from Claude's, so success is not gated on a specific
  stop reason.)

## Flag mapping

| Concept | Odin flag | Claude | grok |
|---------|-----------|--------|------|
| Headless | (implicit) | `-p` | `--prompt-file` (implies headless) |
| Structured output | (implicit) | `--output-format stream-json` | `--output-format streaming-json` |
| Autonomy | `--permission-mode` | `--permission-mode` | `--permission-mode` (`bypassPermissions` = full) |
| Protocol / system prompt | (internal) | `--append-system-prompt` | `--rules` (grok's `--append-system-prompt` alias) |
| Tool allowlist | `--allowed-tools` | `--allowed-tools` | `--tools` |
| Tool denylist | `--disallowed-tools` | `--disallowed-tools` | `--disallowed-tools` |
| Turn cap | `--max-turns` | `--max-turns` | `--max-turns` |
| Prompt delivery | (internal) | stdin | temp `--prompt-file` |

Everything above the backend line — the queue model, sentinel protocol
(`<<<NEXT_CONTEXT>>>` / `<<<NEEDS_INPUT>>>` / `<<<FOLLOW_UP>>>`), carry-context,
held/resume, git startup, and metrics — is identical regardless of platform.
Odin only swaps *which CLI it shells out to and how it reads that CLI's output*.

## Metrics

Task records carry a `platform` field. Token usage is normalised to Odin's
internal keys; grok reuses Claude's snake_case names for the shared token fields
(`input_tokens` / `output_tokens` / `cache_read_input_tokens`) and omits
`cache_creation`, so `metrics._norm_usage` maps both directly.

## Adding another backend (Kiro, Codex, …)

Implement a `_Backend` subclass in `src/odin/runner.py` with `build_cmd`,
`handle_event`, `succeeded`, `name`, `default_bin`, and `prompt_via`
(`"stdin"` | `"file"`), then register it in `_BACKENDS`. Follow the
implementer's checklist in
[`multi-platform-agents-proposal.md`](multi-platform-agents-proposal.md) §11.
Add fake-binary tests alongside `tests/test_backends.py`.
