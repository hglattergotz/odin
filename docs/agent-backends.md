# Agent backends: supported products

Odin drives a queue of tasks through a **headless coding-agent CLI**, one fresh
session per task. Which product it uses is selected with `--platform`. Every
backend is a peer implementing the same `AgentBackend` interface in
`src/odin/backends/` — Claude Code is the default, not a privileged code path.

## Public names (use these)

| Public product | `--platform` | Binary on `PATH` | Odin class |
|----------------|--------------|------------------|------------|
| **[Claude Code](https://code.claude.com/docs)** (Anthropic) | `claude` (default) | `claude` | `ClaudeBackend` |
| **[Cursor CLI](https://cursor.com/docs/cli/overview)** (Cursor) | `cursor` | `agent` | `CursorBackend` |
| **[Grok Build](https://docs.x.ai/build/overview)** (xAI) | `grok` | `grok` | `GrokBackend` |

The short `--platform` key matches the usual binary name (`claude` / `cursor` /
`grok`). In docs and UI copy, prefer the **public product name** so readers
know which tool is supported.

**Not the same thing:** running Cursor CLI with a Grok *model*
(`--platform cursor --model cursor-grok-4.5-high`) is still Cursor CLI.
`--platform grok` means the **Grok Build** product (`grok` on PATH).

For design rationale and roadmap, see
[`multi-platform-agents-proposal.md`](multi-platform-agents-proposal.md).

## Selecting a backend

```
odin run <queue>                          # Claude Code (default)
odin run <queue> --platform cursor        # Cursor CLI
odin run <queue> --platform grok          # Grok Build
ODIN_PLATFORM=cursor odin run <queue>     # via environment
```

Resolution: **`--platform` → `$ODIN_PLATFORM` → `default_platform` in
`~/.odin/config.toml` → `claude`**. Unknown names fail via the registry
(`available platforms: claude, cursor, grok`).

Binary override (any platform): **`--agent-bin`** → config
`platforms.<p>.binary` → backend default. `--claude-bin` remains a deprecated
alias that only applies when the resolved platform is `claude`.

Model: **`--model` → `$ODIN_MODEL` → `platforms.<p>.model` → unset** (CLI default).

## What the generic loop owns vs what a backend owns

`runner.run_agent` is platform-neutral:

- spawn / concurrent stderr drain / NDJSON line loop / wall timing
- **prompt delivery** from `AgentInvokeSpec.prompt_via` (`stdin` or temp `file`)
- **terminal detection** from `handle_stream_event` returning `{"terminal": True}`
- **text delta accumulation** from `{"text_delta": "…"}` captures

Each backend supplies `build_invoke`, `handle_stream_event`, `normalise_result`,
`default_binary`, and `instruction_files`.

## Claude Code (`--platform claude`)

Binary: `claude`. Product: [Claude Code](https://code.claude.com/docs).

```
claude -p --output-format stream-json --verbose \
  --permission-mode <mode> \
  [--model …] [--max-turns N] [--append-system-prompt <protocol>] \
  [--allowed-tools <csv>] [--disallowed-tools <csv>]
# prompt on STDIN
```

- Terminal event: `{"type":"result", …}` (marked `terminal` by the backend)
- Success: exit 0, good `stop_reason`, non-empty final text
- Instruction file: `CLAUDE.md`

## Cursor CLI (`--platform cursor`)

Binary: `agent` (Cursor also documents `cursor-agent` as an alias). Product:
[Cursor CLI](https://cursor.com/docs/cli/overview).

```
agent -p --output-format stream-json --force --trust \
  --workspace <project> \
  [--model …] [--sandbox …] [--approve-mcps]
# prompt on STDIN with ODIN_PROTOCOL prepended (no --append-system-prompt)
```

- Terminal event: `{"type":"result", …}` (no `stop_reason`; camelCase usage)
- Success: exit 0, terminal present, `is_error` not true, non-empty text
- Instruction files: `AGENTS.md`, `.cursor/rules`

## Grok Build (`--platform grok`)

Binary: `grok`. Product: [Grok Build](https://docs.x.ai/build/overview) (xAI).
Install: `curl -fsSL https://x.ai/cli/install.sh | bash`.

```
grok --output-format streaming-json --permission-mode <mode> \
  --prompt-file <tmp> \
  [--model …] [--max-turns N] [--rules <protocol>] \
  [--tools <csv>] [--disallowed-tools <csv>]
# Grok Build does NOT read stdin — Odin writes a temp --prompt-file and deletes it
```

- Assistant text: `{"type":"text","data":"…"}` **chunk deltas** (accumulated)
- Terminal event: `{"type":"end", …}` (camelCase `stopReason`/`sessionId`)
- Failure: `{type:"error"}` line and/or non-zero exit
- Success: exit 0, clean `end`, non-empty accumulated text

## Adding another backend (Kiro, Codex, …)

1. Implement `AgentBackend` in `src/odin/backends/<name>.py`
2. Register it in `registry._BACKENDS`
3. Choose `prompt_via` (`stdin` or `file`) and mark terminal / text deltas in
   `handle_stream_event`
4. Put success / usage / cost logic in `normalise_result`
5. Document the **public product name**, `--platform` key, and binary together
6. Add fake-binary tests (see `tests/test_backends.py`, `tests/test_runner.py`)
7. Follow the checklist in the proposal §11

Do **not** add a new `--<name>-bin` flag — use `--agent-bin` + config.
