# iTerm2 setup guide — Odin live tab status

Odin paints its own terminal tab while a run is in flight: a title, an in-tab
progress bar, and (opt-in) attention, notification, and tab color. This guide
covers what works out of the box, what needs `--notify`, and the per-project
tab-color shell hook that Odin cooperates with.

All signaling is **best-effort and stdlib-only** — escape sequences are plain
byte writes Odin emits on its own stdout (never the `claude -p` child), gated on
that stdout being a TTY. Nothing leaks into pipes, logs, or CI capture.

## Works everywhere (no setup)

On by default; suppress with `--no-title` (or `ODIN_NO_TITLE=1`):

- **Tab title** (OSC 0) — `<prefix> <glyph> <n>/<total> <queue>`, e.g.
  `odin ✓ 3/7 add-search`. Works in Terminal.app, iTerm2, and most others.
- **Progress bar** (OSC 9;4) — fills as the queue drains; turns to an error
  state on failure. Supported by iTerm2 ≥ 3.6.6, Ghostty, Kitty, WezTerm, and
  Windows Terminal; harmlessly ignored elsewhere.

`--tab-title PREFIX` (default `odin`) sets the leading token so two projects'
Odin tabs are distinguishable at a glance.

> The title is **transient by design**: it shows live progress while Odin runs.
> If your shell reasserts the tab title on each prompt (a `precmd` hook), the
> title reverts to the folder name the instant Odin exits. Durable post-run
> status lives in the tab color, `COMPLETED.md`, the exit code, and scrollback.

## iTerm2 extras (opt-in: `--notify`)

Pass `--notify` (or `ODIN_NOTIFY=1`) to enable iTerm2-specific signals:

- **Attention** — bounces the dock icon once on held/failed/urgent.
- **Notification** (OSC 9) — e.g. `odin: add-search needs input`.
- **Tab color** (OSC 1337) — amber on held, red on failed, green on drain.

These are off by default to avoid bell-spam on terminals that map them to a
beep.

### Per-project tab color (`$PROJECT_TAB_COLOR`)

Many setups color each terminal tab by project via a `chpwd` hook. Odin
cooperates rather than clobbering it:

1. Export the project's base hue when you `cd` into it. Example (zsh):

   ```sh
   # ~/.zshrc — color the tab per project and publish the hue for Odin
   _odin_tab_color() {
     case "$PWD" in
       */code/odin)       export PROJECT_TAB_COLOR=4060c0 ;;
       */code/myproject)  export PROJECT_TAB_COLOR=40a060 ;;
       *)                 unset PROJECT_TAB_COLOR ;;
     esac
     [ -n "$PROJECT_TAB_COLOR" ] && \
       printf '\033]1337;SetColors=tab=%s\a' "$PROJECT_TAB_COLOR"
   }
   autoload -U add-zsh-hook && add-zsh-hook chpwd _odin_tab_color
   _odin_tab_color  # run for the initial shell
   ```

2. Odin resolves its base color as `--tab-color` → `$PROJECT_TAB_COLOR` →
   unset. On task start and on success/drain it reverts to that base; on
   held/failed it **leaves** the amber/red flag so the tab keeps flagging until
   you act. Odin never resets the color to iTerm2 `default` when a base is set.

Override per run with `--tab-color HEX` (hex, with or without a leading `#`).

## The `COMPLETED.md` mailbox (opt-in: `--completed-file`)

With `--completed-file` (or `ODIN_COMPLETED=1`), Odin writes a metadata-only
`COMPLETED.md` into the queue dir on every exit (drain/fail/hold/max-tasks;
skipped on `--dry-run`). Pairing is **by directory**: the project's interactive
Claude session runs in that cwd, so it can read the record deterministically on
your next prompt instead of guessing whether Odin finished. The file carries
run_id, queue, branch, exit code + outcome, per-task final states, and
token/cost totals — **never** task bodies, carry-context, or agent output.

## tmux

When `$TMUX` is set, Odin wraps the iTerm2 OSC 1337 / OSC 9 sequences in tmux
DCS passthrough automatically, so color/attention/notification survive the
multiplexer. Titles (OSC 0) pass through tmux untouched. No configuration
needed.

## Claude Code's own notifications

Separate from Odin: Claude Code can post its own notifications via hooks in
`~/.claude/settings.json` (e.g. a `Notification` hook). That's independent of
Odin's signaling and out of scope here — see the Claude Code docs.
