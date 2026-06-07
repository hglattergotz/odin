from pathlib import Path

import pytest

from odin.queue import SUBDIRS, Queue, Task


@pytest.fixture
def q(tmp_path: Path) -> Queue:
    return Queue(tmp_path / "queue")


def _write_task(q: Queue, name: str, body: str = "do the thing") -> Task:
    p = q.root / "pending" / name
    p.write_text(body, encoding="utf-8")
    return Task.from_path(p)


def test_init_creates_all_subdirs(tmp_path: Path):
    Queue(tmp_path / "q")
    for sub in SUBDIRS:
        assert (tmp_path / "q" / sub).is_dir()


def test_pending_lexicographic_order(q: Queue):
    _write_task(q, "002-b.md")
    _write_task(q, "001-a.md")
    _write_task(q, "010-j.md")
    names = [t.name for t in q.pending()]
    assert names == ["001-a.md", "002-b.md", "010-j.md"]


def test_next_pending_returns_first(q: Queue):
    _write_task(q, "002-b.md")
    t1 = _write_task(q, "001-a.md")
    assert q.next_pending().path == t1.path


def test_next_pending_empty(q: Queue):
    assert q.next_pending() is None


def test_claim_running_and_mark_done(q: Queue):
    t = _write_task(q, "001-a.md", "body")
    r = q.claim_running(t)
    assert r.path == q.root / "running" / "001-a.md"
    assert not t.path.exists()
    d = q.mark_done(r)
    assert d.path == q.root / "done" / "001-a.md"
    assert d.read() == "body"


def test_mark_failed(q: Queue):
    t = _write_task(q, "001-a.md")
    r = q.claim_running(t)
    f = q.mark_failed(r)
    assert f.path == q.root / "failed" / "001-a.md"


def test_mark_held_writes_questions_file(q: Queue):
    t = _write_task(q, "001-a.md", "original body")
    r = q.claim_running(t)
    h = q.mark_held(r, "1. Which db?\n2. Which framework?")
    assert h.path == q.root / "held" / "001-a.md"
    qpath = q.held_questions_path("001-a")
    text = qpath.read_text(encoding="utf-8")
    assert "Which db?" in text
    assert "## Answers" in text
    # The body file is preserved in held/ as the original task content.
    assert h.read() == "original body"


def test_held_excludes_questions_files(q: Queue):
    t = _write_task(q, "001-a.md")
    r = q.claim_running(t)
    q.mark_held(r, "q?")
    held = q.held()
    assert [t.name for t in held] == ["001-a.md"]


def test_write_and_read_carry(q: Queue):
    q.write_carry("001-a", "carry body from 001")
    assert q.carry_for("001-a") == "carry body from 001"
    assert q.carry_for("999-missing") is None


def test_latest_carry_body_returns_most_recent_prior(q: Queue):
    q.write_carry("001-a", "from 001")
    q.write_carry("002-b", "from 002")
    q.write_carry("003-c", "from 003")
    # For task 003-c we want the carry from 002-b (the prior producer).
    assert q.latest_carry_body("003-c") == "from 002"
    # For task 010-z we want the latest, which is 003-c.
    assert q.latest_carry_body("010-z") == "from 003"


def test_latest_carry_body_none_when_no_priors(q: Queue):
    q.write_carry("005-e", "from 005")
    # For task 001-a there is no carry that sorts before it.
    assert q.latest_carry_body("001-a") is None


def test_resume_held_merges_qa_and_repends(q: Queue):
    t = _write_task(q, "001-a.md", "original task body")
    r = q.claim_running(t)
    q.mark_held(r, "What library?")
    # User adds answers.
    qpath = q.held_questions_path("001-a")
    text = qpath.read_text(encoding="utf-8")
    qpath.write_text(text + "Use library X because Y.\n", encoding="utf-8")

    resumed = q.resume_held("001-a")
    assert resumed.path == q.root / "pending" / "001-a.md"
    body = resumed.read()
    # Both the question and the answer must be in the merged body so the
    # fresh-session agent can interpret terse answers like "1 c".
    assert "Prior questions and the user's answers" in body
    assert "What library?" in body
    assert "Use library X" in body
    assert "original task body" in body
    # Q+A block sits before the original task body.
    assert body.index("What library?") < body.index("original task body")
    assert body.index("Use library X") < body.index("original task body")
    # The held body file is consumed, but the questions file stays for audit.
    assert not (q.root / "held" / "001-a.md").exists()
    assert qpath.exists()


def test_resume_held_rejects_empty_answers(q: Queue):
    t = _write_task(q, "001-a.md")
    r = q.claim_running(t)
    q.mark_held(r, "What?")
    with pytest.raises(ValueError, match="empty"):
        q.resume_held("001-a")


def test_resume_held_missing_task(q: Queue):
    with pytest.raises(FileNotFoundError):
        q.resume_held("999-nope")


# ----- follow-up tasks: backlog + urgent insert ----------------------

def test_add_backlog_writes_numbered_file(q: Queue):
    _write_task(q, "001-a.md")
    _write_task(q, "002-b.md")
    t = q.add_backlog("Harden input validation", "Validate the X field.")
    assert t.path.parent == q.root / "backlog"
    # Numbered past the highest existing prefix, slugified title.
    assert t.name == "003-harden-input-validation.md"
    body = t.read()
    assert body.startswith("# Harden input validation")
    assert "Validate the X field." in body
    assert [b.name for b in q.backlog()] == ["003-harden-input-validation.md"]


def test_insert_pending_after_sorts_before_next(q: Queue):
    _write_task(q, "003-done-ish.md")
    _write_task(q, "004-next.md")
    inserted = q.insert_pending_after("003-done-ish", "fix the race", "details")
    assert inserted.path.parent == q.root / "pending"
    assert inserted.name.startswith("003-done-ish-followup-")
    # It must sort after 003 and strictly before 004, so it runs next.
    order = [t.name for t in q.pending()]
    assert order.index(inserted.name) < order.index("004-next.md")
    assert q.next_pending().name == inserted.name  # would run next, but 003 itself
    # (003-done-ish is still pending in this unit test; the real flow has it in done/)


def test_unique_names_avoid_collision(q: Queue):
    a = q.add_backlog("same title", "")
    b = q.add_backlog("same title", "")
    assert a.name != b.name


# ----- archive (whole finished sub-queues) ---------------------------

def _subqueue(container: Path, name: str) -> Queue:
    return Queue(container / name)


def test_archive_state_finished_vs_blocked(tmp_path: Path):
    finished = Queue(tmp_path / "finished")
    (finished.root / "done" / "001-a.md").write_text("done")
    assert finished.archive_state() == (True, "done")

    blocked = Queue(tmp_path / "blocked")
    (blocked.root / "done" / "001-a.md").write_text("done")
    (blocked.root / "pending" / "002-b.md").write_text("todo")
    (blocked.root / "failed" / "003-c.md").write_text("oops")
    ok, reason = blocked.archive_state()
    assert ok is False
    assert "1 pending" in reason and "1 failed" in reason

    empty = Queue(tmp_path / "empty")
    assert empty.archive_state() == (False, "empty")


def test_archive_finished_subqueues_moves_whole_dirs(tmp_path: Path):
    from odin.queue import archive_finished_subqueues

    container = tmp_path / "queue"
    done = _subqueue(container, "alpha")
    (done.root / "done" / "001-a.md").write_text("done a")
    (done.root / "carry" / "001-a.next-context.md").write_text("carry")
    active = _subqueue(container, "beta")
    (active.root / "pending" / "001-x.md").write_text("todo")

    archived, skipped = archive_finished_subqueues(container)
    assert archived == [("alpha", "alpha")]
    assert skipped == [("beta", "1 pending")]

    # Whole dir moved as-is; beta untouched and still a visible sub-queue.
    assert not (container / "alpha").exists()
    assert (container / "archive" / "alpha" / "done" / "001-a.md").read_text() == "done a"
    assert (container / "archive" / "alpha" / "carry" / "001-a.next-context.md").exists()
    assert (container / "beta" / "pending" / "001-x.md").exists()
    assert Queue(container, create=False).subqueues() == ["beta"]


def test_archive_finished_subqueues_dedups_name(tmp_path: Path):
    from odin.queue import archive_finished_subqueues

    container = tmp_path / "queue"
    # Pre-existing archive/alpha forces the next alpha to become alpha-2.
    (container / "archive" / "alpha" / "done").mkdir(parents=True)
    again = _subqueue(container, "alpha")
    (again.root / "done" / "001-a.md").write_text("second")

    archived, _ = archive_finished_subqueues(container)
    assert archived == [("alpha", "alpha-2")]
    assert (container / "archive" / "alpha-2" / "done" / "001-a.md").read_text() == "second"


def test_archived_subqueues_newest_first(tmp_path: Path):
    import os

    from odin.queue import archived_subqueues

    container = tmp_path / "queue"
    a = container / "archive" / "old"
    b = container / "archive" / "new"
    (a / "done").mkdir(parents=True)
    (b / "done").mkdir(parents=True)
    os.utime(a, (1_000_000, 1_000_000))
    os.utime(b, (2_000_000, 2_000_000))
    assert [p.name for p in archived_subqueues(container)] == ["new", "old"]


def test_archive_dir_not_treated_as_subqueue(tmp_path: Path):
    container = tmp_path / "queue"
    (container / "archive" / "alpha" / "done").mkdir(parents=True)
    (container / "beta" / "pending").mkdir(parents=True)
    assert Queue(container, create=False).subqueues() == ["beta"]


def test_last_activity_reflects_newest_file(tmp_path: Path):
    import os

    q = Queue(tmp_path / "queue")
    p = q.root / "done" / "001-a.md"
    p.write_text("x")
    os.utime(p, (1_500_000, 1_500_000))
    assert q.last_activity() == 1_500_000


# ----- container detection / no-create -------------------------------

def test_create_false_does_not_make_dirs(tmp_path: Path):
    root = tmp_path / "q"
    Queue(root, create=False)
    assert not root.exists()  # read-only construction must not litter


def test_subqueues_detects_child_queues(tmp_path: Path):
    container = tmp_path / "queue"
    # Two real sub-queues + an unrelated dir.
    Queue(container / "waitlist")
    Queue(container / "auth")
    (container / "notes").mkdir(parents=True)
    q = Queue(container, create=False)
    assert q.subqueues() == ["auth", "waitlist"]


def test_is_empty(tmp_path: Path):
    q = Queue(tmp_path / "q")
    assert q.is_empty() is True
    (q.root / "done" / "001-a.md").write_text("x")
    assert q.is_empty() is False
