---
name: add-a-backend
description: >
  Add a new headless agent platform (a peer AgentBackend) to Odin — e.g. a new coding-agent CLI.
  Use when wiring Odin to drive another product headlessly. Covers the file-level steps, the three
  axes of variation the existing peers reveal, and tests. Not for changing the generic run loop or
  the queue — only for adding a platform behind the existing AgentBackend contract.
---

# Add a backend (a new platform peer)

Odin drives each product through an `AgentBackend`; every platform is a peer. Adding one is a small,
repeatable change against the existing contract. **Source of truth (read first, don't duplicate):**
`docs/multi-platform-agents-proposal.md` §11 (implementer checklist) and `docs/agent-backends.md` (invoke
details). Contract + peer comparison: [`AGENT.md`](AGENT.md).

## Steps

1. **New peer** — `src/odin/backends/<name>.py`, a class subclassing `backends.base.AgentBackend`,
   implementing the five methods: `default_binary`, `instruction_files`, `build_invoke`,
   `handle_stream_event`, `normalise_result`. Model it on the closest existing peer (see the axes below).
2. **Register** — add one entry to `backends.registry._BACKENDS` (`"<name>": <Name>Backend`). That's the
   only wiring; `get_backend` / `available_platforms` / the CLI pick it up automatically.
3. **Model suggestions (optional)** — add a `[platforms.<name>]` entry to `config.py`'s `MODEL_SUGGESTIONS`
   if the product has well-known model ids (drives the `odin config` menu).
4. **Per-platform config (as-needed only)** — if the product needs extra CLI knobs, read a
   `[platforms.<name>]` section via a lazy `from odin import config` inside `build_invoke` (see cursor/grok).
   Skip it if there are none (claude reads no config).
5. **Tests** — add fake-`<name>`-script tests mirroring `tests/test_backends.py`: the terminal-event shape,
   the success gate, and argv/flag mapping. Keep them decoupled from a real binary.

## The three axes of variation (pin these per platform)

The existing peers differ on exactly three things — decide each for the new one:

| Axis | Options (from the peers) |
|------|--------------------------|
| **Protocol injection** | `--append-system-prompt` (claude) · prepend to prompt (cursor) · `--rules` (grok) |
| **Prompt delivery** (`AgentInvokeSpec.prompt_via`) | `"stdin"` (claude/cursor) · `"file"` + a `prompt_file_flag` (grok) |
| **Success gate** (in `normalise_result`) | good `stop_reason` (claude) · terminal-event present (cursor) · terminal present + not-error (grok) |

Also decide what metrics the product emits (usage keys, whether cost/turns are reported — cursor reports
neither; grok reports `num_turns` but `total_cost_usd` only when complete) and map them to Odin's keys.

## Reuse

`runner.py`'s underscore render helpers (`_render_agent_text`, `_write_tool_line`, `_safe_write`, …) are an
informal shared toolkit peers reuse for live display — reuse them, or render your own. Nothing else in the
generic loop should change: if you find yourself editing `runner.run_agent`, the queue, or `protocol.py`,
you're outside "add a backend."

## Don't

- Don't add a *default* platform or a silent fallback (unknown platform must stay a hard error).
- Don't duplicate §11 / `agent-backends.md` here — link to them.
- Don't create a first-class backend; keep every platform a peer.
