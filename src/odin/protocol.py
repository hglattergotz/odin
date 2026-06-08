"""Sentinel marker parsing for agent output.

The target project's CLAUDE.md teaches the agent to terminate every task
with exactly one of two blocks:

    <<<NEXT_CONTEXT>>>
    ...carry-forward prompt for the next task...
    <<<END>>>

    <<<NEEDS_INPUT>>>
    ...questions that must be answered before the task can proceed...
    <<<END>>>

This module finds those blocks in the final assistant message and
classifies the outcome. It is pure (no I/O) so it is trivially testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

NEXT_CONTEXT_OPEN = "<<<NEXT_CONTEXT>>>"
NEEDS_INPUT_OPEN = "<<<NEEDS_INPUT>>>"
FOLLOW_UP_OPEN = "<<<FOLLOW_UP>>>"
END_MARKER = "<<<END>>>"


def _marker_re(marker: str) -> re.Pattern[str]:
    """A marker counts only as a standalone line (optionally indented).

    The contract requires each sentinel on its own line as the last thing the
    agent outputs. Matching only standalone lines means an inline, back-ticked
    mention in prose (e.g. quoting `<<<NEEDS_INPUT>>>` in a summary) is NOT
    treated as an emitted block — which previously routed otherwise-complete
    tasks to failed/ with "both markers present".
    """
    return re.compile(r"(?m)^[ \t]*" + re.escape(marker) + r"[ \t]*$")


_NEXT_RE = _marker_re(NEXT_CONTEXT_OPEN)
_NEEDS_RE = _marker_re(NEEDS_INPUT_OPEN)
_FOLLOW_RE = _marker_re(FOLLOW_UP_OPEN)
_END_RE = _marker_re(END_MARKER)


class Outcome(str, Enum):
    COMPLETED = "completed"   # NEXT_CONTEXT block found
    HELD = "held"             # NEEDS_INPUT block found
    UNPARSEABLE = "unparseable"  # no recognised block, malformed, or both


@dataclass(frozen=True)
class ParseResult:
    outcome: Outcome
    body: str               # block body for COMPLETED/HELD; reason for UNPARSEABLE
    raw: str                # original text we parsed
    follow_up: str | None = None  # raw FOLLOW_UP body (COMPLETED only), if present


def _block_body(text: str, open_re: re.Pattern[str]) -> str | None:
    """Body of the LAST `open_marker … <<<END>>>` block, or None if absent.

    Markers are matched only as standalone lines (see `_marker_re`). Uses the
    last open marker (so quoting the protocol earlier doesn't win) and the FIRST
    standalone END after it (so it pairs with its own close even when other
    blocks — e.g. FOLLOW_UP — follow). Returns "" for a present-but-empty block.
    """
    opens = list(open_re.finditer(text))
    if not opens:
        return None
    start = opens[-1].end()
    end_match = _END_RE.search(text, start)
    if end_match is None:
        return None
    return text[start:end_match.start()].strip()


def parse(text: str) -> ParseResult:
    """Classify an agent's final message.

    Rules:
      - Exactly one of NEXT_CONTEXT / NEEDS_INPUT must appear, each followed by
        an END. Neither → UNPARSEABLE; both → UNPARSEABLE; open with no END →
        UNPARSEABLE; empty body → UNPARSEABLE.
      - A FOLLOW_UP block MAY accompany a NEXT_CONTEXT (completion) and is
        returned raw in `follow_up`; it is ignored on a held task.
    """
    if text is None:
        return ParseResult(Outcome.UNPARSEABLE, "no output", "")

    has_next = _NEXT_RE.search(text) is not None
    has_held = _NEEDS_RE.search(text) is not None

    if has_next and has_held:
        return ParseResult(
            Outcome.UNPARSEABLE,
            "both NEXT_CONTEXT and NEEDS_INPUT markers present",
            text,
        )
    if not has_next and not has_held:
        return ParseResult(
            Outcome.UNPARSEABLE,
            "no sentinel block found in agent output",
            text,
        )

    open_marker = NEXT_CONTEXT_OPEN if has_next else NEEDS_INPUT_OPEN
    open_re = _NEXT_RE if has_next else _NEEDS_RE
    outcome = Outcome.COMPLETED if has_next else Outcome.HELD

    body = _block_body(text, open_re)
    if body is None:
        return ParseResult(
            Outcome.UNPARSEABLE,
            f"{open_marker} found but no matching {END_MARKER} after it",
            text,
        )
    if not body:
        return ParseResult(Outcome.UNPARSEABLE, f"{open_marker} block is empty", text)

    follow_up = None
    if outcome is Outcome.COMPLETED and _FOLLOW_RE.search(text) is not None:
        follow_up = _block_body(text, _FOLLOW_RE) or None

    return ParseResult(outcome, body, text, follow_up=follow_up)


@dataclass(frozen=True)
class Option:
    key: str        # e.g. "a"
    label: str      # short choice text
    detail: str = ""  # one-line trade-off


@dataclass(frozen=True)
class Question:
    question: str
    options: tuple[Option, ...] = ()
    problem: str = ""
    recommended: str | None = None  # an option key, or None
    why: str | None = None


def parse_questions(body: str) -> list[Question] | None:
    """Parse a NEEDS_INPUT body as structured questions.

    Returns a list of Question on success, or None when the body is not the
    expected JSON shape. Callers fall back to treating the body as freeform
    text on None, so plain-text questions keep working — the JSON form just
    unlocks the rich interactive prompt.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("questions")
    if not isinstance(raw, list) or not raw:
        return None

    questions: list[Question] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        q = item.get("question")
        if not isinstance(q, str) or not q.strip():
            return None
        options: list[Option] = []
        for o in item.get("options") or []:
            if not isinstance(o, dict):
                continue
            key = str(o.get("key") or "").strip()
            label = str(o.get("label") or "").strip()
            if not key or not label:
                continue
            options.append(Option(key=key, label=label, detail=str(o.get("detail") or "").strip()))
        rec = item.get("recommended")
        rec = rec.strip() if isinstance(rec, str) and rec.strip() else None
        why = item.get("why")
        why = why.strip() if isinstance(why, str) and why.strip() else None
        questions.append(
            Question(
                question=q.strip(),
                options=tuple(options),
                problem=str(item.get("problem") or "").strip(),
                recommended=rec,
                why=why,
            )
        )
    return questions or None


@dataclass(frozen=True)
class FollowUp:
    title: str
    body: str = ""
    urgent: bool = False  # True → must run before the next queued task


def parse_follow_ups(text: str) -> list[FollowUp] | None:
    """Parse a FOLLOW_UP body into discovered tasks, or None if not valid JSON.

    Accepts a JSON list of objects, or a `{"tasks": [...]}`/`{"follow_ups": [...]}`
    wrapper. Each item needs a non-empty `title`; `body` and `urgent` optional.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("tasks") or data.get("follow_ups") or data.get("followups")
    if not isinstance(data, list) or not data:
        return None

    items: list[FollowUp] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        items.append(
            FollowUp(
                title=title,
                body=str(it.get("body") or "").strip(),
                urgent=bool(it.get("urgent", False)),
            )
        )
    return items or None


_FENCE_RE = re.compile(r"^```[\w-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def unwrap_fence(body: str) -> str:
    """If `body` is wrapped in a single ```fence```, return the inner content.

    The agent often emits the block as fenced markdown for readability;
    when we prepend it to the next task's prompt the fence is just noise.
    Returns `body` unchanged if it isn't a clean single fence.
    """
    m = _FENCE_RE.match(body.strip())
    return m.group(1) if m else body
