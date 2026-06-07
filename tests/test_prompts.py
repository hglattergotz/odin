"""Tests for the interactive terminal prompts (driven via StringIO)."""

from __future__ import annotations

import io

from odin.prompts import (
    BranchPlan, ask_branch_choice, ask_continue, ask_questions, render_questions,
)
from odin.protocol import Option, Question


def _q(**kw) -> Question:
    base = dict(question="Which database?", options=(
        Option("a", "Postgres", "relational, already deployed"),
        Option("b", "SQLite", "zero-ops, single file"),
    ), problem="Storage backend undecided", recommended="a", why="reuse existing infra")
    base.update(kw)
    return Question(**base)


# ----- ask_questions -------------------------------------------------

def test_pick_option_by_key():
    out = io.StringIO()
    answers = ask_questions([_q()], in_=io.StringIO("b\n"), out=out)
    assert "b) SQLite" in answers
    assert "Which database?" in answers
    # The prompt shows options and the recommendation reason.
    rendered = out.getvalue()
    assert "[a] Postgres" in rendered and "(recommended)" in rendered
    assert "Why a:" in rendered


def test_empty_input_takes_recommendation():
    answers = ask_questions([_q()], in_=io.StringIO("\n"), out=io.StringIO())
    assert "a) Postgres" in answers


def test_free_form_answer_recorded_verbatim():
    answers = ask_questions([_q()], in_=io.StringIO("use MySQL actually\n"), out=io.StringIO())
    assert "use MySQL actually" in answers


def test_eof_falls_back_to_recommendation():
    # Empty stream = immediate EOF.
    answers = ask_questions([_q()], in_=io.StringIO(""), out=io.StringIO())
    assert "a) Postgres" in answers


def test_no_recommendation_reprompts_until_answer():
    q = _q(recommended=None, why=None)
    # First line blank -> reprompt; second line chooses a.
    answers = ask_questions([q], in_=io.StringIO("\na\n"), out=io.StringIO())
    assert "a) Postgres" in answers


class _TTYOut(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_question_is_yellow_on_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = _TTYOut()
    ask_questions([_q()], in_=io.StringIO("a\n"), out=out)
    rendered = out.getvalue()
    assert "\033[93m" in rendered          # yellow applied to the question
    assert "Which database?" in rendered


def test_no_color_on_stringio_or_when_no_color_set(monkeypatch):
    # Plain StringIO is not a TTY → no escape codes.
    out = io.StringIO()
    ask_questions([_q()], in_=io.StringIO("a\n"), out=out)
    assert "\033[" not in out.getvalue()
    # NO_COLOR disables color even on a TTY.
    monkeypatch.setenv("NO_COLOR", "1")
    tty = _TTYOut()
    ask_questions([_q()], in_=io.StringIO("a\n"), out=tty)
    assert "\033[" not in tty.getvalue()


def test_multiple_questions_numbered():
    qs = [_q(), _q(question="Which web framework?")]
    answers = ask_questions(qs, in_=io.StringIO("a\nb\n"), out=io.StringIO())
    assert "Q1:" in answers and "Q2:" in answers
    assert "Which web framework?" in answers


# ----- render_questions ----------------------------------------------

def test_render_questions_plain_text():
    text = render_questions([_q()])
    assert "Which database?" in text
    assert "**a**" in text and "Postgres" in text
    assert "(recommended)" in text


# ----- ask_branch_choice ---------------------------------------------

def test_branch_use_current_default():
    plan = ask_branch_choice("main", in_=io.StringIO("\n"), out=io.StringIO())
    assert plan == BranchPlan(name="main", base=None, create=False)


def test_branch_create_new():
    plan = ask_branch_choice(
        "main", in_=io.StringIO("2\nodin/batch\nmain\n"), out=io.StringIO()
    )
    assert plan == BranchPlan(name="odin/batch", base="main", create=True)


def test_branch_create_new_default_base():
    plan = ask_branch_choice(
        "develop", in_=io.StringIO("2\nodin/batch\n\n"), out=io.StringIO()
    )
    assert plan == BranchPlan(name="odin/batch", base="develop", create=True)


def test_branch_switch_existing():
    plan = ask_branch_choice("main", in_=io.StringIO("3\nrelease\n"), out=io.StringIO())
    assert plan == BranchPlan(name="release", base=None, create=False)


# ----- ask_continue --------------------------------------------------

def test_ask_continue_default_and_explicit_continue():
    assert ask_continue(in_=io.StringIO("\n"), out=io.StringIO()) is True
    assert ask_continue(in_=io.StringIO("c\n"), out=io.StringIO()) is True
    assert ask_continue(in_=io.StringIO("continue\n"), out=io.StringIO()) is True


def test_ask_continue_stop():
    assert ask_continue(in_=io.StringIO("s\n"), out=io.StringIO()) is False
    assert ask_continue(in_=io.StringIO("stop\n"), out=io.StringIO()) is False


def test_ask_continue_eof_defaults_to_stop():
    assert ask_continue(in_=io.StringIO(""), out=io.StringIO()) is False


def test_ask_continue_reprompts_on_garbage():
    out = io.StringIO()
    assert ask_continue(in_=io.StringIO("huh\ns\n"), out=out) is False
    assert "Please type c or s" in out.getvalue()
