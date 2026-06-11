"""Interactive terminal prompts for Odin's human-in-the-loop paths.

Two surfaces:
  - ask_branch_choice: at startup, pick the single branch the queue runs on.
  - render_questions / ask_questions: when a task emits NEEDS_INPUT, show the
    structured question(s) and collect answers in-terminal.

Every function takes injectable in_/out streams (defaulting to sys.stdin /
sys.stdout) so tests can drive them with StringIO instead of a real TTY.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

from .protocol import Question

_YELLOW = "\033[93m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _use_color(out: TextIO) -> bool:
    """Color only on a real terminal, and never when NO_COLOR is set.

    StringIO (tests) and pipes report isatty() False, so captured output stays
    plain — no escape codes to assert around.
    """
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(out.isatty())
    except Exception:
        return False


def _yellow(text: str, on: bool) -> str:
    return f"{_BOLD}{_YELLOW}{text}{_RESET}" if on else text


@dataclass(frozen=True)
class BranchPlan:
    name: str          # branch to end up on
    base: str | None   # branch point when creating; None when reusing
    create: bool       # True -> create from base; False -> checkout/stay


# ----------------------------------------------------------------------
# branch selection
# ----------------------------------------------------------------------

def ask_branch_choice(
    current: str,
    *,
    in_: TextIO | None = None,
    out: TextIO | None = None,
) -> BranchPlan:
    in_ = in_ or sys.stdin
    out = out or sys.stdout
    shown = current or "(detached HEAD)"
    out.write("\nOdin runs the whole queue on one branch (no per-task PRs).\n")
    out.write(f"Current branch: {shown}\n\n")
    out.write("  [1] Use the current branch\n")
    out.write("  [2] Create a new branch\n")
    out.write("  [3] Switch to an existing branch\n")
    out.flush()

    while True:
        choice = _readline(in_, out, "\n  Choose 1/2/3 [default: 1]: ")
        if choice in (None, "", "1"):
            return BranchPlan(name=current, base=None, create=False)
        if choice == "2":
            name = _require(in_, out, "  New branch name: ")
            base = _readline(in_, out, f"  Branch from [default: {shown}]: ") or current
            return BranchPlan(name=name, base=base or None, create=True)
        if choice == "3":
            name = _require(in_, out, "  Existing branch name: ")
            return BranchPlan(name=name, base=None, create=False)
        out.write("  Please enter 1, 2, or 3.\n")


# ----------------------------------------------------------------------
# questions
# ----------------------------------------------------------------------

def render_questions(questions: list[Question]) -> str:
    """Plain-text rendering of questions (no input) for the held audit file."""
    out: list[str] = []
    for i, q in enumerate(questions, 1):
        out.append(f"### Question {i}")
        if q.problem:
            out.append(f"_{q.problem}_")
        out.append("")
        out.append(q.question)
        if q.options:
            out.append("")
            for opt in q.options:
                rec = " (recommended)" if q.recommended == opt.key else ""
                line = f"- **{opt.key}** — {opt.label}{rec}"
                if opt.detail:
                    line += f": {opt.detail}"
                out.append(line)
        if q.recommended and q.why:
            out.append("")
            out.append(f"Recommended: {q.recommended} — {q.why}")
        out.append("")
    return "\n".join(out).strip() + "\n"


def ask_questions(
    questions: list[Question],
    *,
    in_: TextIO | None = None,
    out: TextIO | None = None,
) -> str:
    """Render each question, collect a choice, return a human-readable Q+A block.

    Empty input accepts the recommended option when one exists. A response that
    doesn't match an option key is recorded verbatim as a free-form answer.
    """
    in_ = in_ or sys.stdin
    out = out or sys.stdout
    color = _use_color(out)
    answers: list[str] = []
    total = len(questions)
    for i, q in enumerate(questions, 1):
        out.write(f"\n{_yellow(f'── Question {i} of {total} ──', color)}\n")
        if q.problem:
            out.write(f"{q.problem}\n")
        out.write(f"\n{_yellow(q.question, color)}\n\n")
        for opt in q.options:
            rec = "  (recommended)" if q.recommended == opt.key else ""
            out.write(f"  [{opt.key}] {opt.label}{rec}\n")
            if opt.detail:
                out.write(f"      {opt.detail}\n")
        if q.recommended and q.why:
            out.write(f"\n  Why {q.recommended}: {q.why}\n")
        out.flush()
        answers.append(f"Q{i}: {q.question}\n   → {_resolve_choice(q, in_, out)}")
    return "\n\n".join(answers) + "\n"


def _resolve_choice(q: Question, in_: TextIO, out: TextIO) -> str:
    by_key = {opt.key.lower(): opt for opt in q.options}
    hint = f" [default: {q.recommended}]" if q.recommended else ""
    while True:
        raw = _readline(in_, out, f"\n  Your choice{hint} (option key, or a free-form answer): ")
        if raw is None:  # EOF — fall back to the recommendation if there is one
            return _default_answer(q)
        if raw == "":
            if q.recommended and q.recommended.lower() in by_key:
                return _format_option(by_key[q.recommended.lower()])
            out.write("  Please choose an option or type an answer.\n")
            continue
        key = raw.lower()
        if key in by_key:
            return _format_option(by_key[key])
        return raw  # free-form answer


def _default_answer(q: Question) -> str:
    by_key = {opt.key.lower(): opt for opt in q.options}
    if q.recommended and q.recommended.lower() in by_key:
        return _format_option(by_key[q.recommended.lower()])
    return "(no answer provided)"


def _format_option(opt) -> str:
    return f"{opt.key}) {opt.label}" if opt.label else opt.key


# ----------------------------------------------------------------------
# config setter (interactive)
# ----------------------------------------------------------------------

def ask_config(
    config: dict,
    *,
    platforms: list[str],
    suggestions: dict[str, list[str]] | None = None,
    in_: TextIO | None = None,
    out: TextIO | None = None,
) -> dict:
    """Interactive editor for `~/.odin/config.toml`; returns the updated config.

    A small menu loop: set the default platform, or set/unset the model for a
    platform. Mutates and returns `config` (the caller persists it). EOF/empty
    at the top menu exits. Reuses the same low-level IO as the branch selector.
    """
    in_ = in_ or sys.stdin
    out = out or sys.stdout
    suggestions = suggestions or {}
    color = _use_color(out)

    out.write(f"\n{_yellow('Odin configuration', color)}\n")
    while True:
        cur_default = config.get("default_platform") or "(unset)"
        out.write("\n")
        out.write(f"  [1] Default platform (current: {cur_default})\n")
        out.write("  [2] Set model for a platform\n")
        out.write("  [3] Done\n")
        out.flush()
        choice = _readline(in_, out, "\n  Choose 1/2/3 [default: 3]: ")
        if choice in (None, "", "3"):
            return config
        if choice == "1":
            picked = _pick_from_list(
                "Default platform", platforms, config.get("default_platform"), in_, out
            )
            if picked:
                config["default_platform"] = picked
        elif choice == "2":
            platform = _pick_from_list(
                "Which platform to set a model for", platforms, None, in_, out
            )
            if platform:
                _set_model(config, platform, suggestions.get(platform, []), in_, out)
        else:
            out.write("  Please enter 1, 2, or 3.\n")


def _pick_from_list(
    label: str, items: list[str], current: str | None, in_: TextIO, out: TextIO
) -> str | None:
    """Numbered pick from `items` (empty = keep current/skip). Returns the value."""
    if not items:
        out.write(f"  No {label.lower()} available.\n")
        return None
    out.write(f"\n  {label}:\n")
    for i, item in enumerate(items, 1):
        mark = "  (current)" if item == current else ""
        out.write(f"    [{i}] {item}{mark}\n")
    out.flush()
    while True:
        raw = _readline(in_, out, "    Choose a number (empty = keep): ")
        if raw in (None, ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        out.write(f"    Please enter 1-{len(items)}.\n")


def _set_model(
    config: dict, platform: str, suggested: list[str], in_: TextIO, out: TextIO
) -> None:
    """Model picker: curated suggestions + Other (free text) + Use default (unset)."""
    section = config.setdefault("platforms", {}).setdefault(platform, {})
    current = section.get("model")
    out.write(f"\n  Model for {platform}")
    out.write(f" (current: {current})\n" if current else " (current: platform default)\n")
    for i, model in enumerate(suggested, 1):
        mark = "  (current)" if model == current else ""
        out.write(f"    [{i}] {model}{mark}\n")
    other = len(suggested) + 1
    unset = len(suggested) + 2
    out.write(f"    [{other}] Other (type a value)\n")
    out.write(f"    [{unset}] Use platform default (unset)\n")
    out.flush()
    while True:
        raw = _readline(in_, out, "    Choose a number (empty = keep): ")
        if raw in (None, ""):
            return
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(suggested):
                section["model"] = suggested[n - 1]
                return
            if n == other:
                val = _readline(in_, out, "    Model value: ")
                if val:
                    section["model"] = val
                return
            if n == unset:
                section.pop("model", None)
                return
        out.write(f"    Please enter 1-{unset}.\n")


# ----------------------------------------------------------------------
# low-level IO
# ----------------------------------------------------------------------

def ask_continue(
    *,
    in_: TextIO | None = None,
    out: TextIO | None = None,
) -> bool:
    """Ask whether to continue the run after an urgent insert. True=continue.

    Empty input defaults to continue; EOF defaults to stop (the safe choice
    when there's nobody to answer).
    """
    in_ = in_ or sys.stdin
    out = out or sys.stdout
    while True:
        ans = _readline(in_, out, "  Continue the run now? [C]ontinue / [s]top: ")
        if ans is None:
            return False
        a = ans.strip().lower()
        if a in ("", "c", "continue"):
            return True
        if a in ("s", "stop"):
            return False
        out.write("  Please type c or s.\n")


def _readline(in_: TextIO, out: TextIO, prompt: str) -> str | None:
    """Write prompt, read one line. Returns the stripped line, or None on EOF."""
    out.write(prompt)
    out.flush()
    line = in_.readline()
    if line == "":  # EOF
        return None
    return line.strip()


def _require(in_: TextIO, out: TextIO, prompt: str) -> str:
    while True:
        val = _readline(in_, out, prompt)
        if val:
            return val
        if val is None:  # EOF — give up rather than loop forever
            return ""
        out.write("  A value is required.\n")
