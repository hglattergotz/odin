"""The protocol Odin injects into every task via `--append-system-prompt`
(Claude) or by prepending to the stdin prompt (Cursor).

This is the ONE place Odin contributes rules to the agent, and it is limited to
the *protocol* — how to terminate a task and how to phrase questions — never
workflow (testing, commit policy, branching strategy), which stays in the
target project's instruction file (`CLAUDE.md` / `AGENTS.md`).

Injecting the contract (rather than relying on the target instruction file to
carry the snippet) makes Odin self-contained: tasks emit parseable output even
if a project forgot to paste the snippet.
"""

from __future__ import annotations

#: Per-platform name of the file the agent is told to defer to for workflow.
#: Unknown platforms fall back to the generic phrase below.
_INSTRUCTIONS_BY_PLATFORM: dict[str, str] = {
    "claude": "CLAUDE.md",
    "cursor": "AGENTS.md",
}
_GENERIC_INSTRUCTIONS = "project instructions"


_BASE = """\
You are being run by Odin, a headless task orchestrator, in addition to this \
project's {instructions}. Follow this output protocol exactly.

## Precedence

If anything in this project's {instructions} conflicts with this protocol, THIS \
protocol wins for exactly two things: (1) how you end a task (the sentinel \
blocks below) and (2) git/branch/PR policy (see Branch, when present). Defer to \
the project's {instructions} for everything else — how and when to test, code \
style, project conventions. Do not manage the task queue or hunt for "the next \
task": Odin sequences tasks and gives you the context you need.

## Terminating a task

End your final message with EXACTLY ONE of these two sentinel blocks, as the \
very last thing you output. Put nothing after `<<<END>>>`.

On clean completion:

<<<NEXT_CONTEXT>>>
<carry-forward prompt for the next task: what you did, the files and decisions \
the next task should not rediscover, and any deviations or pre-flight checks it \
should know about>
<<<END>>>

When you are blocked on a decision you cannot make safely:

<<<NEEDS_INPUT>>>
<a single JSON object — see "Asking questions" below>
<<<END>>>

Do not guess on substantive, hard-to-reverse decisions; emit NEEDS_INPUT and \
commit nothing. Trivial reversible choices (a variable name, private helper \
layout) are yours — just proceed.

## Asking questions

When you emit `<<<NEEDS_INPUT>>>`, the body MUST be a single JSON object so \
Odin can render it for the user. Be brief — do not overwhelm. Schema:

{{
  "questions": [
    {{
      "problem": "one-line statement of what is blocking you",
      "question": "the specific decision needed?",
      "options": [
        {{"key": "a", "label": "short label", "detail": "one-line trade-off"}},
        {{"key": "b", "label": "short label", "detail": "one-line trade-off"}}
      ],
      "recommended": "a",
      "why": "one short sentence on why you recommend it"
    }}
  ]
}}

Rules for questions:
- Keep every field to a single line; favour 2-3 options.
- Include "recommended" and "why" only when you genuinely have a preference; \
omit both otherwise.
- Emit raw JSON inside the block — no markdown fences, no prose around it.

## Recording discovered follow-up work (optional)

If, while doing THIS task, you discover work that should become its own future \
task, you MAY record it — but only on a completed task, AFTER your \
`<<<NEXT_CONTEXT>>>` block, never instead of finishing. Add a second block \
whose body is a JSON list:

<<<FOLLOW_UP>>>
[
  {{"title": "short imperative title", "urgent": false, "body": "a self-contained task prompt: what to do and why"}},
  {{"title": "...", "urgent": true, "body": "..."}}
]
<<<END>>>

- `urgent` MUST be true ONLY if the work has to be completed BEFORE the next \
queued task can proceed correctly. Urgent items are inserted into the queue to \
run next and the user is asked whether to continue; non-urgent items (the \
default) go to a backlog for the user to handle later.
- Keep titles short; put the real detail in `body`. Omit the whole block if \
there is nothing to record. Do not record the work you just did — only genuinely \
new, separate work.
"""

_BRANCH = """\

## Branch

You are working on the git branch `{branch}`. When the task is complete, commit \
your work to this branch. Do NOT create or switch branches, do NOT push, and do \
NOT open pull requests — the entire queue runs on this single branch. This \
overrides any contrary git instructions in the project's {instructions}.
"""


def _instructions_name(platform: str | None) -> str:
    """Return the instruction-file label to inject for `platform`."""
    if platform is None:
        return _INSTRUCTIONS_BY_PLATFORM["claude"]
    key = platform.strip().lower()
    return _INSTRUCTIONS_BY_PLATFORM.get(key, _GENERIC_INSTRUCTIONS)


def build_system_prompt(
    branch: str | None,
    *,
    platform: str | None = None,
) -> str:
    """Return the protocol text injected into every task.

    When `branch` is set, append the single-branch / no-PR directive so the
    whole queue lands on one branch. When None (e.g. `--no-git` or a non-git
    project), only the sentinel + question protocol is injected.

    `platform` selects the instruction-file name the agent is told to defer to
    for workflow (`CLAUDE.md` for Claude, `AGENTS.md` for Cursor). Defaults to
    Claude's name so `odin guide` and callers that omit it keep the prior text.
    """
    instructions = _instructions_name(platform)
    text = _BASE.format(instructions=instructions)
    if branch:
        return text + _BRANCH.format(branch=branch, instructions=instructions)
    return text
