"""Interruption recovery: the sidecar record and the resumption brief.

When a run is cut off mid-task — a provider usage limit, a hard kill, Odin's own
process dying — the task lands in `interrupted/` with a `NNN-slug.recovery.md`
sidecar beside it. This module owns that file: what goes in it, how it reads
back, and how it becomes the brief that tells the *next* agent it is continuing
someone else's work rather than starting fresh.

Why a brief at all: every task runs in a fresh session, and the carry-context
Odin injects comes from the last task that *completed*. A retried task would
therefore arrive with context describing the milestone before it and nothing
about its own interrupted attempt — walking into a repository full of
half-finished work it has no memory of writing, unable to tell a real defect
from its predecessor's loose ends. The brief closes exactly that gap.

The sidecar's source of truth is a JSON block; the markdown around it is
rendered from that JSON, never parsed. See
`docs/interruption-recovery-proposal.md` §5.1 and §7.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

from odin.git import Snapshot

#: How much of the dying agent's last output to keep. Enough for a paragraph of
#: "here is where I got to", not enough to bloat the task prompt.
LAST_WORDS_LIMIT = 1200

_JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Attempt:
    """One interrupted execution of a task."""

    n: int = 1
    ts: str = field(default_factory=_now_iso)
    reason: str = "unknown"          # usage_limit | process_died | unknown
    confidence: str = "probable"     # confirmed | probable
    detail: str = ""
    resets_at: str | None = None     # ISO-8601, when the provider stated one
    # What the attempt achieved before it was cut off.
    turns: int | None = None
    wall_ms: int | None = None
    cost_usd: float | None = None
    # How it died.
    exit_code: int | None = None
    stop_reason: str | None = None
    error: str | None = None
    session_id: str | None = None
    # What it left behind.
    branch: str | None = None
    head_before: str | None = None
    head_subject: str | None = None
    wip_commit: str | None = None
    files: int = 0
    insertions: int = 0
    deletions: int = 0
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    last_words: str = ""

    @property
    def made_progress(self) -> bool:
        """Did this attempt actually do anything?

        The circuit breaker for repeated recovery. Attempt *count* is the wrong
        signal — a large task legitimately spans several usage windows — but an
        attempt that took no turns and changed no files did not run into a
        limit so much as fail to start, and repeating it will not help.
        """
        return bool(self.files) or (self.turns or 0) > 1

    @classmethod
    def from_dict(cls, data: dict) -> "Attempt":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Record:
    """The full recovery history of one task."""

    stem: str
    attempts: list[Attempt] = field(default_factory=list)
    blocked: bool = False  # set once two attempts in a row made no progress

    @property
    def latest(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    def add(self, attempt: Attempt) -> Attempt:
        attempt.n = len(self.attempts) + 1
        self.attempts.append(attempt)
        # Two consecutive dead attempts means something environmental is wrong
        # (missing binary, expired auth, broken MCP server) wearing an
        # interruption's clothes. Stop rather than loop.
        recent = self.attempts[-2:]
        self.blocked = len(recent) == 2 and not any(a.made_progress for a in recent)
        return attempt

    def to_json(self) -> str:
        return json.dumps(
            {
                "stem": self.stem,
                "blocked": self.blocked,
                "attempts": [asdict(a) for a in self.attempts],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Record":
        data = json.loads(text)
        return cls(
            stem=data.get("stem", ""),
            attempts=[Attempt.from_dict(a) for a in data.get("attempts", [])],
            blocked=bool(data.get("blocked", False)),
        )


def load(sidecar_text: str, *, stem: str) -> Record:
    """Read a sidecar back into a Record; a missing/corrupt one starts fresh.

    Best-effort by design: a hand-edited or truncated sidecar must never be the
    thing that stops a user from recovering their work.
    """
    m = _JSON_BLOCK_RE.search(sidecar_text or "")
    if m:
        try:
            return Record.from_json(m.group(1))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return Record(stem=stem)


def attempt_from(
    *,
    result=None,
    failure=None,
    snap: Snapshot | None = None,
    wip_commit: str | None = None,
    reason: str | None = None,
) -> Attempt:
    """Build an Attempt from a finished run's result, classification and tree."""
    groups = snap.by_kind() if snap is not None else {}
    resets = getattr(failure, "resets_at", None)
    return Attempt(
        reason=reason or getattr(failure, "reason", "unknown"),
        confidence=getattr(failure, "confidence", "probable"),
        detail=getattr(failure, "detail", "") or "",
        resets_at=resets.isoformat() if isinstance(resets, datetime) else None,
        turns=getattr(result, "num_turns", None),
        wall_ms=getattr(result, "wall_ms", None),
        cost_usd=getattr(result, "cost_usd", None),
        exit_code=getattr(result, "exit_code", None),
        stop_reason=getattr(result, "stop_reason", None),
        error=getattr(result, "error", None),
        session_id=getattr(result, "session_id", None),
        branch=getattr(snap, "branch", None),
        head_before=getattr(snap, "head", None),
        head_subject=getattr(snap, "head_subject", None),
        wip_commit=wip_commit,
        files=getattr(snap, "files", 0),
        insertions=getattr(snap, "insertions", 0),
        deletions=getattr(snap, "deletions", 0),
        new=groups.get("new", []),
        modified=groups.get("modified", []),
        deleted=groups.get("deleted", []),
        last_words=_tail(getattr(result, "final_text", "") or ""),
    )


def _tail(text: str, limit: int = LAST_WORDS_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


# ----------------------------------------------------------------------
# sidecar rendering
# ----------------------------------------------------------------------

def render_sidecar(record: Record) -> str:
    """The human-readable sidecar, with the JSON source of truth appended."""
    latest = record.latest
    out: list[str] = [f"# Interrupted — {record.stem}\n\n"]

    if record.blocked:
        out.append(
            "**Blocked.** The last two attempts made no progress (no turns, no "
            "file changes), which does not look like a usage limit. Check the "
            "agent binary and auth, then use `--force` to recover anyway.\n\n"
        )
    else:
        out.append(
            f"Attempt {len(record.attempts) + 1} pending. `odin recover "
            f"{record.stem}` merges the brief below into the task body and "
            "requeues it.\n\n"
        )

    if latest is not None:
        out.append("## Why\n\n")
        out.append(f"{_reason_phrase(latest)} ({latest.confidence})")
        if latest.resets_at:
            out.append(f" · resets {latest.resets_at}")
        out.append("\n")
        if latest.detail:
            out.append(f"\n> {latest.detail}\n")
        out.append("\n")

    if record.attempts:
        out.append("## Attempts\n\n")
        out.append("| # | when | turns | wall | cost | left behind | wip commit |\n")
        out.append("|---|------|-------|------|------|-------------|------------|\n")
        for a in record.attempts:
            wall = f"{round((a.wall_ms or 0) / 1000)}s" if a.wall_ms else "—"
            cost = f"${a.cost_usd:.2f}" if isinstance(a.cost_usd, (int, float)) else "—"
            left = (
                f"{a.files} files +{a.insertions}/-{a.deletions}" if a.files else "nothing"
            )
            out.append(
                f"| {a.n} | {a.ts} | {a.turns if a.turns is not None else '—'} "
                f"| {wall} | {cost} | {left} | {a.wip_commit or '—'} |\n"
            )
        out.append("\n")

    out.append("## Resumption brief\n\n")
    out.append(build_brief(record) or "_(none)_")
    out.append("\n\n<details><summary>evidence</summary>\n\n```json\n")
    out.append(record.to_json())
    out.append("\n```\n\n</details>\n")
    return "".join(out)


def _reason_phrase(a: Attempt) -> str:
    return {
        "usage_limit": "provider usage limit",
        "process_died": "the odin process did not exit cleanly",
        "no_sentinel": "the agent emitted no sentinel block",
    }.get(a.reason, "interrupted for an unrecognised reason")


# ----------------------------------------------------------------------
# the resumption brief
# ----------------------------------------------------------------------

MINIMAL_BRIEF = (
    "## You are resuming this task\n\n"
    "A previous attempt at this task was interrupted and left work behind. "
    "Inspect the working tree and recent commits before making changes."
)

_STANDING_INSTRUCTIONS = """\
Before writing anything:
- Inventory what already exists against what this task requires.
- Finish partial work rather than rewriting it. If you do replace something,
  say why in your NEXT_CONTEXT.
- Prove each acceptance criterion with a test or command. A function existing
  is not evidence that it works.
- Compile errors in this area may be your predecessor's unfinished work, not an
  inherited defect — check before concluding the repository is broken."""


def build_brief(record: Record, *, verify_output: str = "") -> str:
    """Synthesize the text merged into the task body on recovery.

    Everything here is derived from the run and the repository — nothing is
    invented. When a fact is unavailable (no commit was needed, the process died
    before saying anything) the corresponding paragraph is simply omitted rather
    than guessed at.
    """
    a = record.latest
    if a is None:
        return ""

    parts: list[str] = ["## You are resuming this task\n"]
    parts.append(_opening(a, len(record.attempts)))

    if a.wip_commit and a.files:
        parts.append(
            f"\nYour predecessor's work is in commit {a.wip_commit}:\n"
            + _file_lines(a)
            + f"  {a.files} file{'s' if a.files != 1 else ''} changed, "
            f"+{a.insertions}/-{a.deletions}\n"
        )
    elif a.files:
        parts.append(
            f"\nYour predecessor left uncommitted work in the tree:\n"
            + _file_lines(a)
            + f"  {a.files} file{'s' if a.files != 1 else ''} changed, "
            f"+{a.insertions}/-{a.deletions}\n"
        )
    else:
        parts.append(
            "\nIt left nothing behind in the working tree — it was stopped "
            "before it changed anything.\n"
        )

    if a.head_before:
        subject = f' "{a.head_subject}"' if a.head_subject else ""
        parts.append(
            f"\nThe last commit before that attempt is {a.head_before}{subject}.\n"
            "Anything after it is your predecessor's unfinished work.\n"
        )

    if a.last_words:
        parts.append("\nIts final output before it stopped:\n")
        parts.append(_quote(a.last_words))

    if verify_output.strip():
        parts.append("\nCurrent state of the project's verification command:\n")
        parts.append(_quote(verify_output.strip()))

    parts.append("\n" + _STANDING_INSTRUCTIONS)
    return "".join(parts)


def _opening(a: Attempt, total: int) -> str:
    if a.reason == "process_died":
        base = (
            "A previous attempt at this task was cut short when Odin's own "
            "process stopped. How far it got is not recorded — only what it "
            "left behind, below."
        )
    else:
        bits = []
        if a.turns:
            bits.append(f"{a.turns} turn{'s' if a.turns != 1 else ''}")
        if a.wall_ms and a.wall_ms >= 1000:
            bits.append(_short_duration(a.wall_ms))
        after = f" after {' / '.join(bits)}" if bits else ""
        cause = (
            "a provider usage limit" if a.reason == "usage_limit"
            else "an interruption"
        )
        base = f"A previous attempt was interrupted{after} by {cause}."
    ordinal = f" This is attempt {total + 1}." if total > 1 else ""
    return f"\n{base} You are continuing it, not starting fresh.{ordinal}\n"


def _file_lines(a: Attempt) -> str:
    rows = [("new", a.new), ("modified", a.modified), ("deleted", a.deleted)]
    out = []
    for label, paths in rows:
        if not paths:
            continue
        shown = ", ".join(paths[:6])
        if len(paths) > 6:
            shown += f", … ({len(paths) - 6} more)"
        out.append(f"  {label:9} {shown}\n")
    return "".join(out)


def _quote(text: str) -> str:
    return "".join(f"  {line}\n" for line in text.strip().split("\n"))


def _short_duration(ms: int) -> str:
    secs = ms // 1000
    if secs >= 3600:
        return f"~{secs // 3600}h{(secs % 3600) // 60:02d}m"
    if secs >= 60:
        return f"~{secs // 60}m"
    return f"~{secs}s"
