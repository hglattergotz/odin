"""`odin guide` — a self-contained authoring manual, printed to stdout.

The point of this command is self-discovery: an agent working in *another*
project (one being built with Odin) can run `odin guide` and learn everything
it needs to author a valid Odin queue and compatible project instructions
(`CLAUDE.md` / `AGENTS.md`) — no other context, no human explanation. The
global `odin` is on PATH even when the Odin source tree isn't reachable, so
the CLI is the reliable discovery path.

The protocol section is generated from `contract.build_system_prompt`, so what
the guide documents and what Odin actually injects can never drift apart.
"""

from __future__ import annotations

from .contract import build_system_prompt

_INTRO = """\
# Authoring tasks for Odin

Odin runs a queue of tasks through a headless agent CLI — Claude Code
(`claude -p`) by default, or Cursor Agent (`agent -p`) with
`--platform cursor` — one at a time, each in a fresh session, carrying
context forward and pausing for input. Your job as the author is to produce
two things: a **queue of task files** and (optionally) a **project
instruction file** with your workflow rules (`CLAUDE.md` for Claude,
`AGENTS.md` / `.cursor/rules` for Cursor). This guide is everything you
need to do that.
"""

_QUEUE = """\
## 1. The queue and task files

- **Always organize tasks into a *named* queue** — one sub-queue per batch of
  related work (a feature, a fix-set, a milestone). The single, consistent
  layout is:

      queue/<name>/pending/NNN-slug.md

  where `<name>` is a short kebab-case label for the batch (e.g. `add-auth`,
  `payment-flow`). Do NOT drop task files directly in a bare `queue/pending/` —
  always put them under a `<name>`, even for a single task.
- You only create the `pending/` dir. Odin creates and manages the sibling
  state dirs (`running/ done/ failed/ held/ carry/ backlog/`) inside the same
  `queue/<name>/`.
- **One task = one file** in `queue/<name>/pending/`, named `NNN-slug.md`
  (e.g. `001-init-db.md`, `002-add-health-endpoint.md`).
- The file **body IS the prompt** — plain Markdown, **no frontmatter**, no
  special wrapper. Odin does not parse the body; it passes it to the agent.
- The `NNN` prefix sets run order (lexicographic) *within that named queue*.
  Tasks run in sequence; many files = many tasks. There is no single "master"
  file format — it's always one file per task.
- Keep each task to **one focused change**. Continuity between tasks is handled
  by carry-context (see §5), so you don't repeat earlier context in every file.
- **Why named queues?** `odin status queue` then shows a clean overview, one
  line per batch (most-recently-active first); you run, branch, and archive each
  batch independently; and adding more work later means a fresh `queue/<name>/`
  rather than piling unrelated tasks onto one undifferentiated `pending/` list.
- **Version control — suggest, don't change:** as Odin runs it creates state
  dirs under `queue/` (`running/ done/ held/ failed/ carry/`) that churn the git
  tree. Most projects add `queue/` to `.gitignore` and leave it untracked —
  unless they want the task files kept in history, in which case track them.
  **Surface this to the user as a suggestion and let them decide. Do NOT edit
  `.gitignore` yourself without asking.**
"""

_TASK_BODY = """\
## 2. Writing a good task body

- Write it as an instruction: state the change, the concrete requirements, and
  what "done" looks like (acceptance criteria, files to touch).
- Make it self-contained for a fresh session, but don't restate what the prior
  task already established — the carry-context prefix covers that.
- **Don't pre-decide things you don't actually know.** If a real, hard-to-
  reverse decision isn't derivable from the task, the existing code, or
  CLAUDE.md, leave it open: the runtime agent is told to *ask* rather than
  guess (see §3), and Odin will surface the question to the user.

Example — `queue/add-auth/pending/002-add-health-endpoint.md`:

    # Task: add a health-check endpoint

    Add `GET /healthz` to the existing API that returns HTTP 200 with body
    `{"status": "ok"}`.

    Requirements:
    - Route registered alongside the other routes (don't restructure routing).
    - A test asserting 200 and the JSON body.
    - No new runtime dependencies.
"""

_CLAUDE_MD = """\
## 3. The target project's CLAUDE.md

Odin **injects the protocol for you** (see §5) via the agent's system prompt,
so your project's CLAUDE.md does NOT need to teach the sentinel blocks or the
question format. It should own the **workflow** — the rules Odin deliberately
stays out of. A good minimal section:

    ## Workflow (rules for tasks run by Odin)

    - Test-to-green where tests exist: run them (e.g. `uv run pytest`) before
      declaring a task done. Don't disable tests, loosen assertions, or use
      `--no-verify`.
    - One task = one focused change. Don't sneak in refactors or cleanups the
      task body didn't ask for.
    - Commit policy is yours: Odin positions the branch (or skips git) but
      never commits itself. If you commit, do it only when tests are green;
      the message explains the "why," not the "what."
    - No partial work: if you can't finish safely, emit a question (NEEDS_INPUT)
      and leave the tree clean — don't half-land and defer to "the next task."

CLAUDE.md is optional but recommended; without it the agent still gets the
protocol (injected), it just won't know your workflow conventions.

It is also worth recording, near the top of your CLAUDE.md, that the project is
run by Odin — so both humans and the agent know git and sequencing are managed.
Paste this marker block:

    ## This project is run by Odin

    Tasks here are executed by Odin, a headless orchestrator. When a task runs:
    - **Git is batch-managed.** The whole task queue runs on ONE branch. Commit
      your work to the current branch when a task is done; do NOT create or
      switch branches, push, or open pull requests.
    - **Don't manage the queue.** Odin decides task order and feeds you the
      context you need; never look for "the next task" yourself.
    - **End every task** with exactly one Odin sentinel block (run
      `odin guide protocol` for the exact contract).

Odin's injected protocol takes precedence over the project's CLAUDE.md for task
termination and git/branch/PR policy, so the marker mainly keeps a human reader
(and any non-Odin run of the agent) from being surprised.

For Cursor (`--platform cursor`), the equivalent file is **AGENTS.md** — see
`odin guide agent-md`.
"""

_AGENT_MD = """\
## 3b. Target project instructions for Cursor (AGENTS.md)

When you run with `--platform cursor`, Odin looks for Cursor's instruction
files instead of CLAUDE.md:

| Platform | Instruction paths | Missing warn when… |
|----------|-------------------|--------------------|
| `claude` | `CLAUDE.md` | no `CLAUDE.md` |
| `cursor` | `AGENTS.md`, `.cursor/rules` | neither exists |

- **AGENTS.md alone** is enough — no spurious CLAUDE.md warning.
- **`.cursor/rules/` alone** also suppresses the missing-instruction warn.
- Claude ignores AGENTS.md for missing/conflict lint; Cursor ignores CLAUDE.md.

Odin **injects the protocol for you** (see §4) by prepending it to the Cursor
prompt, so AGENTS.md does NOT need to teach the sentinel blocks. It should own
the **workflow** — same rules as CLAUDE.md, different file. A good minimal
section:

    ## Workflow (rules for tasks run by Odin)

    - Test-to-green where tests exist: run them before declaring a task done.
      Don't disable tests, loosen assertions, or use `--no-verify`.
    - One task = one focused change. Don't sneak in refactors the task body
      didn't ask for.
    - Commit policy is yours: Odin positions the branch (or skips git) but
      never commits itself. If you commit, do it only when tests are green.
    - No partial work: if you can't finish safely, emit NEEDS_INPUT and leave
      the tree clean.

Paste this marker near the top of AGENTS.md:

    ## This project is run by Odin

    Tasks here are executed by Odin, a headless orchestrator. When a task runs:
    - **Git is batch-managed.** The whole task queue runs on ONE branch. Commit
      your work to the current branch when a task is done; do NOT create or
      switch branches, push, or open pull requests.
    - **Don't manage the queue.** Odin decides task order and feeds you the
      context you need; never look for "the next task" yourself.
    - **End every task** with exactly one Odin sentinel block (run
      `odin guide protocol` for the exact contract).

### Cross-platform layout

If the same project may run under either Claude or Cursor, keep workflow in
one place and point the other file at it:

    myproject/
    ├── AGENTS.md          # workflow rules (platform-neutral source of truth)
    ├── CLAUDE.md          # "Follow AGENTS.md for workflow."
    ├── .cursor/rules/     # optional scoped Cursor rules
    └── queue/...

A focused pasteable snippet lives at `examples/target-agents-md-snippet.md`
(Claude equivalent: `examples/target-claude-md-snippet.md`).

### Running under Cursor

    odin run <name> --platform cursor
    # or set a default once:
    odin config set default_platform cursor

`--agent-bin` overrides the Cursor `agent` binary; Claude keeps using
`--claude-bin`. See `odin run -h` for Cursor-only flags (`--sandbox`,
`--approve-mcps`, `--force`, `--trust`).
"""


def _protocol_section(*, platform: str | None = None) -> str:
    heading = (
        "## 4. The protocol Odin injects (reference — you don't write this)\n\n"
    )
    if platform == "cursor":
        note = (
            "Shown with Cursor wording (`AGENTS.md`). The Claude default "
            "substitutes `CLAUDE.md` — same protocol, different instruction "
            "file name. Odin prepends this contract to every Cursor task "
            "prompt:\n\n"
        )
    else:
        note = (
            "Shown with Claude wording (`CLAUDE.md`); Cursor runs get "
            "`AGENTS.md` via `--platform cursor`. Odin injects the following "
            "contract into every task (Claude: `--append-system-prompt`; "
            "Cursor: prepended to the prompt):\n\n"
        )
    return heading + note + _indent_block(
        build_system_prompt(None, platform=platform)
    )


_FLOW = """\
## 5. How tasks flow (so the queue makes sense)

- **Carry-context.** When a task completes, its `<<<NEXT_CONTEXT>>>` block is
  saved and **prepended** to the next task's prompt under
  `## Context from previous task`. That's why later tasks can be terse.
- **Held → resume.** When a task emits `<<<NEEDS_INPUT>>>`, Odin pauses: at a
  terminal it shows the question and you answer inline; unattended it writes a
  `held/` file you fill in and `odin resume <stem>`. Either way the task re-runs
  in a fresh session with your answer in context.
- **Routing.** Clean completion → `done/`; questions → `held/`; anything else
  (crash, no protocol block, hit max turns) → `failed/`. Files are moved,
  never deleted — the queue is the audit trail.
- **Discovered work.** A completed task may also emit a `<<<FOLLOW_UP>>>` block
  (see the protocol section). Non-urgent items are filed in that queue's
  `backlog/` and called out when the queue finishes; urgent items are inserted
  to run next and you're asked whether to continue.
"""

_TERMINAL = '''\
## 7. Set up the terminal for Odin's tab signaling (agent-executable)

This section is written so an **agent** can configure a Mac terminal (iTerm2) to
take full advantage of Odin's live tab signaling — title, in-tab progress bar,
and the opt-in `--notify` attention/color. Run the numbered steps in order.

Some steps are **manual GUI clicks** (iTerm2 settings can't all be scripted) —
those are flagged **[MANUAL — ask the user to click]**. Steps that edit user
dotfiles are flagged **[EDITS DOTFILE]**.

**Rules for the editing steps (non-negotiable):**
- **Confirm with the user before editing `~/.zshrc` or
  `~/.claude/settings.json`.** State exactly what you will append.
- **Back the file up first** (e.g. `cp ~/.zshrc ~/.zshrc.odin-bak`).
- **Be idempotent:** grep for the Odin marker comment first and **skip** if the
  block is already present. Never append a second copy.

### Step 1 — Pre-check (no changes)

    which brew                       # is Homebrew installed?
    ls -d /Applications/iTerm.app    # is iTerm2 installed?
    echo $TERM_PROGRAM               # "iTerm.app" if you're already in iTerm2

### Step 2 — Install iTerm2 if missing

If `/Applications/iTerm.app` was absent in step 1:

    brew install --cask iterm2

(If `brew` itself is missing, ask the user to install Homebrew first — do not
attempt an unattended Homebrew install.)

### Step 3 — Per-project tab identity hook  **[EDITS DOTFILE: ~/.zshrc]**

Ask the user which directory holds their projects and set `_PROJECT_ROOT` to it
(default `$HOME/code`). This block colors, titles, and badges each tab
by project and exports `PROJECT_TAB_COLOR` so Odin reverts to the project hue
after a run instead of clobbering it. Confirm + back up + check for the marker
(`iTerm2 per-project tab identity`) before appending:

    # --- iTerm2 per-project tab identity (color + name) ---
    autoload -Uz add-zsh-hook
    _PROJECT_ROOT="$HOME/code"                     # <- the user's projects root
    _COLOR_REGISTRY="$HOME/.config/iterm-project-colors.tsv"
    _TAB_PALETTE=(1f6f6b 3a5fcd b5651d 7a4fb5 2e8b57 b5446e c08a00 4682b4
                  8b3a3a 4e9a06 8e44ad d35400 16a085 c0392b 2980b9 27ae60)
    _iterm_proj() { case "$PWD/" in "$_PROJECT_ROOT"/*) print -r -- "${${PWD#$_PROJECT_ROOT/}%%/*}";; *) print -r -- "";; esac; }
    _iterm_project_identity() {
      local proj=$(_iterm_proj)
      if [[ -z "$proj" ]]; then
        unset PROJECT_TAB_COLOR
        [[ "$TERM_PROGRAM" == "iTerm.app" ]] && { printf '\\033]1337;SetColors=tab=default\\a'; printf '\\033]1337;SetBadgeFormat=\\a'; }
        return
      fi
      mkdir -p "${_COLOR_REGISTRY:h}"; touch "$_COLOR_REGISTRY"
      local hex=$(awk -F'\\t' -v p="$proj" '$1==p{print $2; exit}' "$_COLOR_REGISTRY")
      if [[ -z "$hex" ]]; then local n=$(wc -l < "$_COLOR_REGISTRY"); hex="${_TAB_PALETTE[$(( n % ${#_TAB_PALETTE[@]} + 1 ))]}"; printf '%s\\t%s\\n' "$proj" "$hex" >> "$_COLOR_REGISTRY"; fi
      export PROJECT_TAB_COLOR="$hex"
      [[ "$TERM_PROGRAM" == "iTerm.app" ]] && { printf '\\033]1337;SetColors=tab=%s\\a' "$hex"; printf '\\033]1337;SetBadgeFormat=%s\\a' "$(print -rn -- "$proj" | base64)"; }
    }
    _iterm_project_title() { local proj=$(_iterm_proj); printf '\\033]0;%s\\007' "${proj:-${PWD:t}}"; }
    add-zsh-hook chpwd _iterm_project_identity
    add-zsh-hook precmd _iterm_project_title
    _iterm_project_identity; _iterm_project_title
    # ------------------------------------------------------

After appending, tell the user to `source ~/.zshrc` (or open a new tab).

### Step 4 — Enable escape-sequence alerts  **[MANUAL — ask the user to click]**

`--notify` notifications need one iTerm2 setting that has no scripting hook.
Ask the user to:

  iTerm2 → Settings → Profiles → Terminal → Notification Center Alerts →
  Filter Alerts → check **"Send escape sequence-generated alerts"**

Also approve the macOS notification-permission prompt the first time one fires.

### Step 5 — Claude Code desktop notifications (OPTIONAL)

Wholly optional, and separate from Odin's own signaling.

- **Simplest path:** Claude Code has a built-in `preferredNotifChannel` setting
  (e.g. `iterm2_with_bell`) — no scripts, no hooks.
- **Richer path:** `brew install terminal-notifier`, create `~/.claude/cc-notify.sh`
  that reads the hook JSON on stdin and posts a `terminal-notifier` alert (use
  `plutil -extract ... -o - -` to pull fields — no `jq`/`python` dependency),
  then add a `Notification` hook to `~/.claude/settings.json` pointing at it.
  **[EDITS DOTFILE: ~/.claude/settings.json]** — same confirm/back-up/idempotent
  rules as step 3.

### Step 6 — Verify

Run each and watch the tab react:

    printf '\\033]0;hello\\007'                  # title → "hello"
    printf '\\033]1337;SetColors=tab=cc3333\\a'  # tab turns red (iTerm2)
    printf '\\033]9;4;3;0\\a'                     # progress bar → indeterminate spinner

Then run any real `odin run` and watch the tab show `<prefix> <glyph> <n>/<total>
<queue>` with the progress bar filling as the queue drains.

---

All of this is **optional polish**: Odin runs fine in any terminal. Titles and
the progress bar are silently ignored where unsupported, and `--notify`/color are
opt-in — nothing here is required to run a queue.
'''

_RUN = """\
## 6. Run it

    mkdir -p queue/<name>/pending      # one named sub-queue per batch of work
    # write your NNN-slug.md task files into queue/<name>/pending/
    odin run <name>                    # bare name resolves under ./queue/
                                       # (same as: odin run queue/<name>)

    odin status queue                  # overview of every named queue, newest first
    odin status <name>                 # drill into one (bare name works here too)

A natural pattern is one branch per batch: `odin run queue/<name> --branch
<name>`. See `odin run -h` for platform selection (`--platform`/`--model`),
branch selection (`--branch`/`--base`/`--no-git`), permissions
(`--allowed-tools`), and limits (`--max-tasks`). Run `odin demo DIR` to
scaffold a complete working example you can study and run (Claude-only for
v1; see the demo readme for a manual Cursor smoke-test).

On a TTY the Odin tab shows live status (title + progress bar; `--notify` adds
iTerm2 attention/color), so you can leave a batch running and glance over.
"""


# Topic -> ordered section builders. "all" (default) prints everything.
def _section(name: str) -> str:
    return {
        "queue": _QUEUE,
        "task": _TASK_BODY,
        "claude-md": _CLAUDE_MD,
        "agent-md": _AGENT_MD,
        "protocol": _protocol_section(),
        "protocol-cursor": _protocol_section(platform="cursor"),
        "flow": _FLOW,
        "run": _RUN,
        "terminal": _TERMINAL,
    }[name]


TOPICS = {
    "tasks": ("queue", "task", "flow", "run"),
    "claude-md": ("claude-md", "protocol"),
    "agent-md": ("agent-md", "protocol-cursor"),
    "protocol": ("protocol",),
    "terminal": ("terminal",),
}


def render(topic: str | None = None) -> str:
    """Return the guide text. `topic` None/"all" → the full manual; otherwise a
    focused subset (see TOPICS). Unknown topics fall back to the full manual."""
    if topic in (None, "all"):
        parts = [
            _INTRO, _QUEUE, _TASK_BODY, _CLAUDE_MD, _AGENT_MD,
            _protocol_section(), _FLOW, _RUN, _TERMINAL,
        ]
    else:
        names = TOPICS.get(topic)
        if names is None:
            parts = [
                _INTRO, _QUEUE, _TASK_BODY, _CLAUDE_MD, _AGENT_MD,
                _protocol_section(), _FLOW, _RUN, _TERMINAL,
            ]
        else:
            parts = [_section(n) for n in names]
    return "\n\n".join(p.strip() for p in parts) + "\n"


def _indent_block(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())
