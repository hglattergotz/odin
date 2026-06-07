"""Filesystem-backed task queue.

Layout (relative to a queue root):

    pending/   NNN-slug.md   — waiting, picked in lexicographic order
    running/   NNN-slug.md   — at most one in flight
    done/      NNN-slug.md   — completed successfully
    failed/    NNN-slug.md   — non-zero exit, unparseable output, etc.
    held/      NNN-slug.md           — blocked on questions (original task body)
               NNN-slug.questions.md — questions + Answers heading
    carry/     NNN-slug.next-context.md — carry-forward from prior task

We never delete queue files; we only move them between subdirs. The audit
trail of where each task ended up matters more than tidy directories.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SUBDIRS = ("pending", "running", "done", "failed", "held", "carry", "backlog")

# `odin archive` moves whole finished sub-queues under <container>/archive/<name>/
# so the container overview only shows work still in flight. The archive dir is
# never itself treated as a sub-queue.
ARCHIVE_DIRNAME = "archive"

# A sub-queue is archivable only when nothing actionable remains — everything
# ran and succeeded. These states each block archiving (with a reason).
_ARCHIVE_BLOCKERS = ("pending", "running", "held", "failed", "backlog")


@dataclass(frozen=True)
class Task:
    path: Path           # absolute path to the current location of the .md file
    name: str            # e.g. "001-add-readme.md"
    stem: str            # e.g. "001-add-readme"

    @classmethod
    def from_path(cls, path: Path) -> "Task":
        return cls(path=path.resolve(), name=path.name, stem=path.stem)

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


class Queue:
    """Thin wrapper around the queue directory tree.

    The constructor ensures every subdir exists. All move operations are
    atomic (os.replace) within the same filesystem.
    """

    def __init__(self, root: Path, *, create: bool = True):
        self.root = root.resolve()
        if create:
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """Create the standard subdirs. Only commands that move files in (run)
        need this; read-only commands construct with create=False so they never
        litter a non-queue directory."""
        for sub in SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # --- queries -----------------------------------------------------

    def pending(self) -> list[Task]:
        """Pending tasks in lexicographic order."""
        return self._list("pending")

    def running(self) -> list[Task]:
        return self._list("running")

    def done(self) -> list[Task]:
        return self._list("done")

    def failed(self) -> list[Task]:
        return self._list("failed")

    def backlog(self) -> list[Task]:
        return self._list("backlog")

    def counts(self) -> dict[str, int]:
        """Per-state task counts — used for the container overview."""
        return {
            "pending": len(self.pending()),
            "running": len(self.running()),
            "held": len(self.held()),
            "done": len(self.done()),
            "failed": len(self.failed()),
            "backlog": len(self.backlog()),
        }

    def is_empty(self) -> bool:
        """True if no task files live in any working subdir of this queue."""
        return not any(
            self._list(s)
            for s in ("pending", "running", "held", "done", "failed", "backlog")
        )

    def subqueues(self) -> list[str]:
        """Names of immediate child dirs that are themselves queues.

        Lets a command tell when a path is a *container* of named queues
        (e.g. queue/ holding queue/waitlist, queue/auth) rather than a queue.
        """
        if not self.root.is_dir():
            return []
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir()
            and child.name != ARCHIVE_DIRNAME
            and any((child / s).is_dir() for s in SUBDIRS)
        )

    def last_activity(self) -> float:
        """Most recent mtime of any task file across the working subdirs.

        Used to order sub-queues in the container overview newest-first, so the
        queue you most recently worked sorts to the top. Falls back to the
        queue dir's own mtime when it holds no task files.
        """
        latest = 0.0
        for sub in SUBDIRS:
            d = self.root / sub
            if not d.is_dir():
                continue
            for p in d.glob("*"):
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    pass
        if latest == 0.0:
            try:
                latest = self.root.stat().st_mtime
            except OSError:
                pass
        return latest

    def archive_state(self) -> tuple[bool, str]:
        """Return (archivable, reason).

        Archivable only when every task ran and succeeded — i.e. nothing remains
        in pending/running/held/failed/backlog and there is at least one done
        task. Otherwise `reason` summarises why (e.g. '2 pending, 1 failed', or
        'empty').
        """
        c = self.counts()
        blockers = [f"{c[s]} {s}" for s in _ARCHIVE_BLOCKERS if c[s]]
        if blockers:
            return False, ", ".join(blockers)
        if c["done"] == 0:
            return False, "empty"
        return True, "done"

    def held(self) -> list[Task]:
        """Held tasks — only the bodies (NNN-slug.md), not the *.questions.md."""
        return [
            t for t in self._list("held")
            if not t.name.endswith(".questions.md")
        ]

    def next_pending(self) -> Task | None:
        items = self.pending()
        return items[0] if items else None

    def carry_for(self, stem: str) -> str | None:
        """Return the carry-context body for `stem`, or None if absent."""
        p = self.root / "carry" / f"{stem}.next-context.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def held_questions_path(self, stem: str) -> Path:
        return self.root / "held" / f"{stem}.questions.md"

    # --- mutations ---------------------------------------------------

    def claim_running(self, task: Task) -> Task:
        return self._move(task, "running")

    def mark_done(self, task: Task) -> Task:
        return self._move(task, "done")

    def mark_failed(self, task: Task) -> Task:
        return self._move(task, "failed")

    def mark_held(self, task: Task, questions_body: str, *, raw: str | None = None) -> Task:
        """Move the task body to held/, and write the questions file alongside.

        `questions_body` is the human-readable rendering shown in the file and
        carried into the resumed prompt. `raw`, when given and different, is the
        agent's original block (e.g. the JSON) preserved for audit.
        """
        moved = self._move(task, "held")
        qpath = self.held_questions_path(moved.stem)
        qpath.write_text(
            _questions_template(questions_body, raw=raw),
            encoding="utf-8",
        )
        return moved

    def record_answers(self, stem: str, answers_text: str) -> None:
        """Append answers under the existing '## Answers' heading of a held file.

        Used by the interactive path: it collects answers in-terminal and writes
        them here so the existing resume_held() merge logic can run unchanged.
        """
        qpath = self.held_questions_path(stem)
        if not qpath.exists():
            raise FileNotFoundError(f"no questions file at {qpath}")
        existing = qpath.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n") else "\n"
        qpath.write_text(existing + sep + answers_text.strip() + "\n", encoding="utf-8")

    def add_backlog(self, title: str, body: str) -> Task:
        """Record a discovered, non-urgent follow-up task in backlog/.

        Named with the next global NNN prefix so it is ready to promote into
        pending/ later just by moving the file.
        """
        base = f"{self._next_index():03d}-{_slugify(title)}"
        p = self.root / "backlog" / self._unique_name("backlog", base)
        p.write_text(_task_file(title, body), encoding="utf-8")
        return Task.from_path(p)

    def insert_pending_after(self, after_stem: str, title: str, body: str) -> Task:
        """Insert an urgent follow-up so it sorts right after `after_stem` and
        before the next pending task — i.e. it runs next.

        `<after_stem>-followup-<slug>` starts with the just-finished task's stem,
        so it lexicographically follows it but precedes the higher-numbered next
        task.
        """
        base = f"{after_stem}-followup-{_slugify(title)}"
        p = self.root / "pending" / self._unique_name("pending", base)
        p.write_text(_task_file(title, body), encoding="utf-8")
        return Task.from_path(p)

    def write_carry(self, for_next_after_stem: str, body: str) -> Path:
        """Persist a carry-context body keyed off the *producing* task's stem.

        We store under the producer's stem (e.g. carry/001-foo.next-context.md)
        rather than the consumer's, so multiple runs are deterministic without
        needing to know what task N+1 will be.
        """
        p = self.root / "carry" / f"{for_next_after_stem}.next-context.md"
        p.write_text(body, encoding="utf-8")
        return p

    def latest_carry_body(self, before_stem: str) -> str | None:
        """Return the most recent carry body produced *before* `before_stem`.

        Carry files are looked up lexicographically; we return the body of the
        last one whose stem sorts strictly less than `before_stem`. Returns
        None if there is no eligible producer (e.g. the very first task).
        """
        carry_dir = self.root / "carry"
        candidates = sorted(carry_dir.glob("*.next-context.md"))
        # Strip the ".next-context.md" suffix to compare against `before_stem`.
        prior = [
            p for p in candidates
            if p.name.removesuffix(".next-context.md") < before_stem
        ]
        if not prior:
            return None
        return prior[-1].read_text(encoding="utf-8")

    def resume_held(self, stem: str) -> Task:
        """Move a held task back to pending/ and merge Q+A into its body.

        Reads both the questions and answers sections from
        <stem>.questions.md and prepends them, paired, to the original task
        body. Pairing matters: each fresh Claude session has no memory of the
        prior session, so bare answer letters ("1 c") would be meaningless
        without the question text they refer to. Raises if no answers found.
        """
        body_path = self.root / "held" / f"{stem}.md"
        q_path = self.held_questions_path(stem)
        if not body_path.exists():
            raise FileNotFoundError(f"no held task body at {body_path}")
        if not q_path.exists():
            raise FileNotFoundError(f"no questions file at {q_path}")

        qa = _extract_qa(q_path.read_text(encoding="utf-8"))
        if not qa.answers.strip():
            raise ValueError(
                f"{q_path} has an empty '## Answers' section; "
                "fill it in before resuming."
            )

        original = body_path.read_text(encoding="utf-8")
        merged = (
            "## Prior questions and the user's answers\n\n"
            "In a previous run you asked the questions below and the user "
            "answered them. Treat these answers as resolved decisions and "
            "proceed with the task — do not re-ask.\n\n"
            "### Questions you asked\n\n"
            f"{qa.questions.strip()}\n\n"
            "### User's answers\n\n"
            f"{qa.answers.strip()}\n\n"
            "---\n\n"
            f"{original}"
        )

        pending_path = self.root / "pending" / f"{stem}.md"
        pending_path.write_text(merged, encoding="utf-8")
        # Keep the questions file in held/ as an audit record; move only the body.
        body_path.unlink()
        return Task.from_path(pending_path)

    # --- internals ---------------------------------------------------

    def _list(self, subdir: str) -> list[Task]:
        d = self.root / subdir
        return sorted(
            (Task.from_path(p) for p in d.glob("*.md")),
            key=lambda t: t.name,
        )

    def _move(self, task: Task, to_subdir: str) -> Task:
        dest = self.root / to_subdir / task.name
        os.replace(task.path, dest)
        return Task.from_path(dest)

    def _next_index(self) -> int:
        """One past the highest NNN- prefix seen anywhere in the queue."""
        mx = 0
        for sub in SUBDIRS:
            for t in self._list(sub):
                m = re.match(r"(\d+)", t.stem)
                if m:
                    mx = max(mx, int(m.group(1)))
        return mx + 1

    def _unique_name(self, subdir: str, base: str) -> str:
        d = self.root / subdir
        if not (d / f"{base}.md").exists():
            return f"{base}.md"
        i = 2
        while (d / f"{base}-{i}.md").exists():
            i += 1
        return f"{base}-{i}.md"


# ----------------------------------------------------------------------
# container-level archive (whole finished sub-queues)
# ----------------------------------------------------------------------

def archive_finished_subqueues(
    container: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Move every *finished* sub-queue of `container` into
    container/archive/<name>/ so the overview only shows live work.

    Returns (archived, skipped):
      archived = [(name, archived_as)]  — archived_as differs from name only on
                                          a clash (suffixed -2, -3, …).
      skipped  = [(name, reason)]       — reason from Queue.archive_state().

    Pure move (os.rename of the whole dir) — nothing is deleted, so an archived
    sub-queue is restored by moving it back out of archive/.
    """
    container = container.resolve()
    archive_root = container / ARCHIVE_DIRNAME
    archived: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for name in Queue(container, create=False).subqueues():
        sq = Queue(container / name, create=False)
        ok, reason = sq.archive_state()
        if not ok:
            skipped.append((name, reason))
            continue
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = _unique_dir(archive_root, name)
        os.rename(sq.root, dest)
        archived.append((name, dest.name))
    return archived, skipped


def archived_subqueues(container: Path) -> list[Path]:
    """Archived sub-queue dirs under container/archive/, newest-first by mtime."""
    adir = container.resolve() / ARCHIVE_DIRNAME
    if not adir.is_dir():
        return []
    return sorted(
        (c for c in adir.iterdir() if c.is_dir()),
        key=_safe_mtime,
        reverse=True,
    )


def _unique_dir(parent: Path, name: str) -> Path:
    """A non-existent path under `parent`: name, then name-2, name-3, …"""
    if not (parent / name).exists():
        return parent / name
    i = 2
    while (parent / f"{name}-{i}").exists():
        i += 1
    return parent / f"{name}-{i}"


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

_QUESTIONS_HEADING_RE = re.compile(r"^## Questions\s*$", re.MULTILINE)
_ANSWERS_HEADING_RE = re.compile(r"^## Answers\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "task"


def _task_file(title: str, body: str) -> str:
    """Render a new task file body from a follow-up's title + body."""
    heading = title if title.lstrip().startswith("#") else f"# {title}"
    out = heading.rstrip() + "\n"
    if body.strip():
        out += "\n" + body.strip() + "\n"
    return out


@dataclass(frozen=True)
class _QA:
    questions: str
    answers: str


def _questions_template(questions_body: str, raw: str | None = None) -> str:
    parts = [
        "# Held — agent requested input\n\n",
        "The agent emitted these questions and did not commit anything. ",
        "Add answers under the Answers heading below, then run ",
        "`odin resume <task-stem>`.\n\n",
        "## Questions\n\n",
        f"{questions_body.strip()}\n\n",
    ]
    if raw is not None and raw.strip() and raw.strip() != questions_body.strip():
        parts.append("<details><summary>raw agent block</summary>\n\n")
        parts.append("```\n")
        parts.append(f"{raw.strip()}\n")
        parts.append("```\n\n</details>\n\n")
    parts.append("## Answers\n\n")
    return "".join(parts)


def _extract_qa(questions_file_text: str) -> _QA:
    """Split a held questions file into its Questions and Answers sections."""
    am = _ANSWERS_HEADING_RE.search(questions_file_text)
    if am is None:
        return _QA(questions="", answers="")
    answers = questions_file_text[am.end():]
    qm = _QUESTIONS_HEADING_RE.search(questions_file_text)
    if qm is None or qm.end() >= am.start():
        return _QA(questions="", answers=answers)
    return _QA(
        questions=questions_file_text[qm.end():am.start()],
        answers=answers,
    )
