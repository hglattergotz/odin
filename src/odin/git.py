"""Git helpers: startup branch management, plus the recovery WIP checkpoint.

Odin's git footprint is deliberately tiny. At *startup* it verifies the working
tree is clean and puts the repo on the one branch the whole queue will land on.
Per-task milestone commits stay the target project's CLAUDE.md's job.

There is exactly **one** write operation, `commit_wip`, and it exists for one
purpose: when a run is interrupted mid-task, the partial work in the tree is
checkpointed into a single commit so the queue can restart against a clean tree
(see `docs/interruption-recovery-proposal.md` §6.1). Odin still never pushes,
merges, rebases, amends, or opens PRs, and never rewrites history.

Every function shells out to `git` (must be on PATH) with `cwd` set to the
target project. Non-zero exits raise GitError so the CLI can print a clean
message instead of a traceback.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    """A git invocation failed."""


class SecretGuardError(GitError):
    """A WIP commit was refused because it would have included a secret-ish file.

    "Never commit secrets" is a durable supply-chain rule, and an automated
    `git add` over whatever an interrupted agent left behind is exactly how that
    rule gets broken by accident. Raised *before* anything is staged.
    """


#: Filenames that must never be swept into an automated WIP commit. Matched
#: case-insensitively against each dirty path's basename. Deliberately short:
#: a false positive costs one manual commit, a false negative leaks a secret.
SECRET_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "credentials.json", "service-account*.json",
)


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(project),
        capture_output=True,
        text=True,
    )


def _checked(project: Path, *args: str) -> str:
    proc = _git(project, *args)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def is_repo(project: Path) -> bool:
    proc = _git(project, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def current_branch(project: Path) -> str:
    """Short name of the current branch, or "" when HEAD is detached."""
    proc = _git(project, "symbolic-ref", "--quiet", "--short", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_clean(project: Path, *, ignore_within: Path | None = None) -> tuple[bool, str]:
    """Return (clean, porcelain_text).

    `ignore_within`, when set and located inside the project, drops status
    entries beneath it — this is how the queue directory (untracked
    pending/done/… files) is excluded so it doesn't make the tree look dirty.
    """
    out = _checked(project, "status", "--porcelain")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if ignore_within is not None:
        rel = _rel_or_none(project, ignore_within)
        if rel is not None:
            lines = [ln for ln in lines if not _entry_within(ln, rel)]
    return (not lines, "\n".join(lines))


def head_sha(project: Path, *, short: bool = True) -> str:
    """Current HEAD sha, or "" on an unborn branch (no commits yet)."""
    proc = _git(project, "rev-parse", "--short" if short else "--verify", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def last_commit_subject(project: Path) -> str:
    """Subject line of HEAD, or "" when there are no commits."""
    proc = _git(project, "log", "-1", "--pretty=%s")
    return proc.stdout.strip() if proc.returncode == 0 else ""


@dataclass(frozen=True)
class Snapshot:
    """What an interrupted run left in the working tree.

    Captured at the moment of interruption (or at recovery time for a task
    stranded in `running/`), recorded in the recovery sidecar, and used to build
    the resumption brief. Purely descriptive — taking a snapshot writes nothing.
    """

    branch: str
    head: str                       # short sha, "" on an unborn branch
    head_subject: str
    entries: tuple[tuple[str, str], ...] = ()  # (XY status, path)
    files: int = 0
    insertions: int = 0
    deletions: int = 0
    #: Pre-rename paths. Git only reports `R`/`C` once the rename is *staged*
    #: (an unstaged on-disk rename shows up as a separate delete + untracked
    #: pair), so these never need staging — they are kept for the secret guard
    #: and for describing the change in the brief.
    rename_origs: tuple[str, ...] = field(default=())

    @property
    def dirty(self) -> bool:
        return bool(self.entries)

    def paths(self) -> list[str]:
        return [p for _, p in self.entries]

    def by_kind(self) -> dict[str, list[str]]:
        """Paths grouped as new / modified / deleted, for the brief."""
        groups: dict[str, list[str]] = {"new": [], "modified": [], "deleted": []}
        for xy, path in self.entries:
            if xy == "??" or "A" in xy:
                groups["new"].append(path)
            elif "D" in xy:
                groups["deleted"].append(path)
            else:
                groups["modified"].append(path)
        return groups

    def diffstat(self) -> str:
        """`8 files changed, +1187/-43` — empty string when nothing is dirty."""
        if not self.entries:
            return ""
        return (
            f"{self.files} file{'s' if self.files != 1 else ''} changed, "
            f"+{self.insertions}/-{self.deletions}"
        )


def snapshot(project: Path, *, ignore_within: Path | None = None) -> Snapshot:
    """Describe the working tree without touching it.

    `ignore_within` drops entries beneath a directory — the queue dir, using the
    same exclusion `is_clean` applies, so Odin's own bookkeeping is never
    mistaken for the agent's work.
    """
    entries, origs = _dirty_entries(project, ignore_within=ignore_within)
    files, insertions, deletions = _count_changes(project, entries)
    return Snapshot(
        branch=current_branch(project),
        head=head_sha(project),
        head_subject=last_commit_subject(project),
        entries=tuple(entries),
        files=files,
        insertions=insertions,
        deletions=deletions,
        rename_origs=tuple(origs),
    )


def secret_paths(paths: list[str]) -> list[str]:
    """Subset of `paths` whose basename looks like a credential (SECRET_GLOBS)."""
    hits = []
    for p in paths:
        base = p.rstrip("/").rsplit("/", 1)[-1].lower()
        if any(fnmatch.fnmatch(base, g) for g in SECRET_GLOBS):
            hits.append(p)
    return hits


def commit_wip(
    project: Path,
    *,
    stem: str,
    run_id: str | None = None,
    reason: str | None = None,
    ignore_within: Path | None = None,
    snap: Snapshot | None = None,
) -> tuple[str, Snapshot] | None:
    """Checkpoint an interrupted run's partial work into one commit.

    Returns `(short_sha, snapshot)`, or **None when there was nothing to commit**
    — a limit hit before the agent did any work leaves a clean tree, and an
    empty commit would be noise.

    Deliberate choices, each one load-bearing (proposal §6.1):

    - **Scoped** to the dirty paths outside `ignore_within` (the queue dir), so
      Odin's own bookkeeping is never committed into the user's project.
    - **Hooks bypassed** (`--no-verify`): a pre-commit hook that rejects
      non-building code would block the user at exactly the moment they are
      trying to get unblocked, and this is a checkpoint, not a contribution.
    - **Secret-guarded**: raises `SecretGuardError` *before staging anything*
      if a dirty path looks like a credential.
    - **Safe on failure**: any git error unstages (`git reset`, which leaves the
      working tree untouched) before raising, so a failed checkpoint never
      loses work and never leaves a half-staged index.

    Never amends, rebases, pushes, or rewrites history.
    """
    snap = snap if snap is not None else snapshot(project, ignore_within=ignore_within)
    if not snap.dirty:
        return None

    offenders = secret_paths(list(snap.paths()) + list(snap.rename_origs))
    if offenders:
        raise SecretGuardError(
            "refusing to auto-commit — these look like secrets: "
            + ", ".join(offenders)
        )

    # Only current paths are staged. A deleted-but-tracked file still matches
    # via the index, but a rename's *original* path matches neither index nor
    # worktree once git has recorded the rename — passing it makes `git add`
    # fail with "pathspec did not match any files".
    to_stage = list(snap.paths())
    subject = f"wip(odin): interrupted attempt at {stem}"
    body = (
        f"{snap.diffstat()}. Partial work from an interrupted attempt — "
        "may not build or pass tests."
    )
    trailers = f"Odin-WIP: {stem}"
    if run_id:
        trailers += f"\nOdin-Run: {run_id}"
    if reason:
        trailers += f"\nOdin-Reason: {reason}"

    try:
        _checked(project, "add", "-A", "--", *to_stage)
        _checked(
            project, "commit", "--no-verify",
            "-m", subject, "-m", body, "-m", trailers,
        )
    except GitError:
        _git(project, "reset")  # unstage; working tree is left exactly as found
        raise
    return head_sha(project), snap


def branch_exists(project: Path, name: str) -> bool:
    proc = _git(project, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    return proc.returncode == 0


def checkout(project: Path, name: str) -> None:
    _checked(project, "switch", name)


def create_and_checkout(project: Path, name: str, base: str | None = None) -> None:
    args = ["switch", "-c", name]
    if base:
        args.append(base)
    _checked(project, *args)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _dirty_entries(
    project: Path, *, ignore_within: Path | None = None
) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse `git status --porcelain -z` into ([(XY, path)], [rename_origs]).

    The `-z` form is used rather than the plain one because it emits raw,
    unquoted, NUL-separated paths — the plain form C-quotes anything with a
    space or non-ASCII byte, which would then be handed to `git add` verbatim
    and miss the file. Rename/copy entries are `XY <new>\\0<orig>\\0`.
    """
    out = _checked(project, "status", "--porcelain", "-z")
    fields = out.split("\0")
    entries: list[tuple[str, str]] = []
    origs: list[str] = []
    rel = _rel_or_none(project, ignore_within) if ignore_within is not None else None

    i = 0
    while i < len(fields):
        chunk = fields[i]
        i += 1
        if len(chunk) < 4:
            continue
        xy, path = chunk[:2], chunk[3:]
        orig: str | None = None
        if "R" in xy or "C" in xy:
            if i < len(fields):
                orig = fields[i]
                i += 1
        if rel is not None and _path_within(path, rel):
            continue
        entries.append((xy, path))
        if orig:
            origs.append(orig)
    return entries, origs


def _count_changes(
    project: Path, entries: list[tuple[str, str]]
) -> tuple[int, int, int]:
    """(files, insertions, deletions) across `entries`, without touching the index.

    Tracked changes come from `git diff --shortstat HEAD` limited to those
    paths. Untracked files are not in any diff, so their lines are counted
    directly; unreadable or binary files contribute nothing rather than failing.

    The file count is not simply `len(entries)`: git collapses a wholly
    untracked directory into a single `?? build/` entry, so a task that created
    twelve files under one new package would otherwise be reported as "1 file
    changed".
    """
    tracked = [p for xy, p in entries if xy != "??"]
    untracked = [p for xy, p in entries if xy == "??"]

    files = len(tracked)
    insertions = deletions = 0
    if tracked and head_sha(project):
        proc = _git(project, "diff", "--shortstat", "HEAD", "--", *tracked)
        if proc.returncode == 0:
            insertions, deletions = _parse_shortstat(proc.stdout)

    for rel in untracked:
        expanded = _expand(project / rel)
        files += len(expanded) or 1  # a vanished path still counts as one entry
        for f in expanded:
            insertions += _line_count(f)
    return files, insertions, deletions


def _expand(target: Path) -> list[Path]:
    """Files under `target` — porcelain reports a wholly-untracked dir as one
    entry (`?? build/`), so it has to be walked to be counted."""
    if target.is_dir():
        return [p for p in target.rglob("*") if p.is_file()]
    return [target] if target.is_file() else []


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            data = fh.read()
    except OSError:
        return 0
    if b"\0" in data[:8192]:
        return 0  # binary — git wouldn't count lines for it either
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _parse_shortstat(text: str) -> tuple[int, int]:
    """` 3 files changed, 12 insertions(+), 4 deletions(-)` -> (12, 4)."""
    ins = dele = 0
    for part in text.strip().split(","):
        part = part.strip()
        num = part.split(" ", 1)[0]
        if not num.isdigit():
            continue
        if "insertion" in part:
            ins = int(num)
        elif "deletion" in part:
            dele = int(num)
    return ins, dele


def _path_within(path: str, rel_prefix: str) -> bool:
    prefix = rel_prefix.rstrip("/")
    p = path.rstrip("/")
    return p == prefix or p.startswith(prefix + "/")


def _rel_or_none(project: Path, target: Path) -> str | None:
    try:
        return str(target.resolve().relative_to(project.resolve()))
    except ValueError:
        return None  # queue lives outside the project; nothing to filter


def _entry_within(porcelain_line: str, rel_prefix: str) -> bool:
    """True if a `git status --porcelain` entry points inside `rel_prefix`.

    Porcelain v1 lines are "XY <path>" or "XY <orig> -> <path>" for renames.
    """
    payload = porcelain_line[3:] if len(porcelain_line) > 3 else porcelain_line
    path = payload.split(" -> ")[-1].strip().strip('"')
    prefix = rel_prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")
