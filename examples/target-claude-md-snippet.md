# Odin-compatible snippet for a target project's CLAUDE.md

**You no longer need to paste the sentinel protocol into your CLAUDE.md.**
Odin injects it for every task via `--append-system-prompt` (see
`src/odin/contract.py`): the `<<<NEXT_CONTEXT>>>` / `<<<NEEDS_INPUT>>>`
blocks, the JSON question schema, and a "stay on this one branch, no PRs"
directive when Odin is managing the branch.

What your project's CLAUDE.md still owns is the **workflow** — when to
test, what blocks a commit, how to phrase the carry-forward. Paste a
section like the one below.

A well-formed target project's CLAUDE.md describes a "next session
prompt" — exactly what the `<<<NEXT_CONTEXT>>>` block should contain.

---

## For reference: the protocol Odin injects

You don't write this — Odin does — but it's what the agent is told. Every
task ends with **exactly one** block, nothing after `<<<END>>>`:

```
<<<NEXT_CONTEXT>>>
<carry-forward: next task ID, files/decisions not to rediscover,
 deviations made, pre-flight checks like baseline test counts>
<<<END>>>
```

When blocked on a substantive, hard-to-reverse decision (don't guess —
commit nothing), the body is JSON so Odin can render it interactively:

```
<<<NEEDS_INPUT>>>
{"questions": [
  {"problem": "one-line context",
   "question": "the decision needed?",
   "options": [{"key": "a", "label": "short", "detail": "trade-off"},
               {"key": "b", "label": "short", "detail": "trade-off"}],
   "recommended": "a",
   "why": "one short sentence"}
]}
<<<END>>>
```

Keep it brief; include `recommended`/`why` only when you have a genuine
preference. Trivial reversible choices (variable names, private helper
organisation) don't count — make those calls and proceed.

## Workflow rules to put in YOUR CLAUDE.md

- **Clean tree precheck.** Odin already refuses to start a batch on a
  dirty tree, but check `git status` before starting anyway; if dirty,
  emit `<<<NEEDS_INPUT>>>` rather than mixing in-flight work into a task.
- **Stay on Odin's branch.** Odin checks out one branch for the whole
  batch and tells you its name. Commit there; don't switch branches or
  open a PR per task.
- **Test-to-green before commit.** Failing tests block the commit.
  Don't comment out tests, don't loosen assertions, don't `--no-verify`.
- **Commit and (optionally) push** only after green tests. The commit
  message describes the "why," not the "what."
- **No partial work in completion.** If you can't finish, emit
  `<<<NEEDS_INPUT>>>` and leave the tree clean — don't half-land and
  then say "next task should finish this."
