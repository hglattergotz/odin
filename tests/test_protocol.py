import json

from odin.protocol import Outcome, parse, parse_questions, unwrap_fence


def test_completed_simple():
    text = "Some preamble.\n<<<NEXT_CONTEXT>>>\nDo task 002 next.\n<<<END>>>\n"
    r = parse(text)
    assert r.outcome is Outcome.COMPLETED
    assert r.body == "Do task 002 next."


def test_held_simple():
    text = "<<<NEEDS_INPUT>>>\n1. Which DB?\n2. Which auth?\n<<<END>>>"
    r = parse(text)
    assert r.outcome is Outcome.HELD
    assert "Which DB?" in r.body
    assert "Which auth?" in r.body


def test_no_marker_is_unparseable():
    r = parse("I finished but forgot the protocol.")
    assert r.outcome is Outcome.UNPARSEABLE
    assert "no sentinel" in r.body.lower()


def test_both_markers_is_unparseable():
    text = "<<<NEXT_CONTEXT>>>\nx\n<<<END>>>\n<<<NEEDS_INPUT>>>\ny\n<<<END>>>"
    r = parse(text)
    assert r.outcome is Outcome.UNPARSEABLE
    assert "both" in r.body.lower()


def test_inline_marker_mention_in_prose_does_not_block():
    # Regression: an agent that emits a real NEXT_CONTEXT block but also quotes
    # the OTHER sentinel inline in its prose (e.g. "No `<<<NEEDS_INPUT>>>` was
    # needed") must NOT be treated as "both markers present". Only standalone
    # marker lines count as emitted blocks.
    text = (
        "Done. No `<<<NEEDS_INPUT>>>` was needed; the location was unambiguous.\n"
        "<<<NEXT_CONTEXT>>>\n"
        "Real carry-forward content.\n"
        "<<<END>>>\n"
    )
    r = parse(text)
    assert r.outcome is Outcome.COMPLETED
    assert r.body == "Real carry-forward content."


def test_open_with_no_end_is_unparseable():
    r = parse("<<<NEXT_CONTEXT>>>\nbody without end")
    assert r.outcome is Outcome.UNPARSEABLE


def test_end_before_open_is_unparseable():
    # END appears, but only before the open marker — not a valid pair.
    r = parse("<<<END>>>\n<<<NEXT_CONTEXT>>>\nbody")
    assert r.outcome is Outcome.UNPARSEABLE


def test_empty_body_is_unparseable():
    r = parse("<<<NEXT_CONTEXT>>>\n   \n<<<END>>>")
    assert r.outcome is Outcome.UNPARSEABLE


def test_last_open_wins_when_protocol_is_quoted():
    # Agents often quote the protocol earlier in their reasoning; the
    # terminal block is the authoritative one.
    text = (
        "First I'll explain the protocol: emit <<<NEXT_CONTEXT>>>...<<<END>>>.\n"
        "Now my actual block:\n"
        "<<<NEXT_CONTEXT>>>\n"
        "Real carry-forward content.\n"
        "<<<END>>>\n"
    )
    r = parse(text)
    assert r.outcome is Outcome.COMPLETED
    assert r.body == "Real carry-forward content."


def test_none_input():
    r = parse(None)  # type: ignore[arg-type]
    assert r.outcome is Outcome.UNPARSEABLE


def test_preserves_internal_whitespace():
    text = "<<<NEXT_CONTEXT>>>\nline 1\n\nline 3\n<<<END>>>"
    r = parse(text)
    assert r.body == "line 1\n\nline 3"


def test_unwrap_fence_strips_markdown_fence():
    body = "```markdown\nhello\nworld\n```"
    assert unwrap_fence(body) == "hello\nworld"


def test_unwrap_fence_passthrough_when_not_fenced():
    assert unwrap_fence("plain text") == "plain text"


def test_unwrap_fence_passthrough_when_partial_fence():
    # Only one side fenced — leave it alone.
    assert unwrap_fence("```\nhello") == "```\nhello"


# ----- parse_questions -----------------------------------------------

def _qjson(**extra) -> str:
    q = {
        "problem": "storage undecided",
        "question": "Which database?",
        "options": [
            {"key": "a", "label": "Postgres", "detail": "relational"},
            {"key": "b", "label": "SQLite", "detail": "zero-ops"},
        ],
        "recommended": "a",
        "why": "reuse infra",
    }
    q.update(extra)
    return json.dumps({"questions": [q]})


def test_parse_questions_full():
    qs = parse_questions(_qjson())
    assert qs is not None and len(qs) == 1
    q = qs[0]
    assert q.question == "Which database?"
    assert q.problem == "storage undecided"
    assert q.recommended == "a"
    assert q.why == "reuse infra"
    assert [o.key for o in q.options] == ["a", "b"]
    assert q.options[0].label == "Postgres"


def test_parse_questions_optional_fields_omitted():
    body = json.dumps({"questions": [{"question": "Proceed how?"}]})
    qs = parse_questions(body)
    assert qs is not None
    assert qs[0].recommended is None
    assert qs[0].why is None
    assert qs[0].options == ()


def test_parse_questions_returns_none_on_plain_text():
    assert parse_questions("1. Which DB?\n2. Which auth?") is None


def test_parse_questions_returns_none_on_bad_shape():
    assert parse_questions(json.dumps({"foo": "bar"})) is None
    assert parse_questions(json.dumps({"questions": []})) is None
    assert parse_questions(json.dumps({"questions": ["not an object"]})) is None
    assert parse_questions(json.dumps({"questions": [{"no_question": 1}]})) is None


def test_parse_questions_skips_malformed_options():
    body = json.dumps({"questions": [{
        "question": "Pick?",
        "options": [{"key": "a", "label": "ok"}, {"key": "", "label": "skip"}, "junk"],
    }]})
    qs = parse_questions(body)
    assert qs is not None
    assert [o.key for o in qs[0].options] == ["a"]


# ----- FOLLOW_UP -----------------------------------------------------

from odin.protocol import parse_follow_ups  # noqa: E402


def test_parse_next_context_with_follow_up():
    text = (
        "<<<NEXT_CONTEXT>>>\ncarry text\n<<<END>>>\n"
        "<<<FOLLOW_UP>>>\n[{\"title\": \"do x\"}]\n<<<END>>>"
    )
    r = parse(text)
    assert r.outcome is Outcome.COMPLETED
    assert r.body == "carry text"          # NEXT_CONTEXT body unaffected by trailing block
    assert r.follow_up == '[{"title": "do x"}]'


def test_parse_next_context_without_follow_up_has_none():
    r = parse("<<<NEXT_CONTEXT>>>\ncarry\n<<<END>>>")
    assert r.outcome is Outcome.COMPLETED
    assert r.follow_up is None


def test_follow_up_ignored_on_held():
    text = "<<<NEEDS_INPUT>>>\nq?\n<<<END>>>\n<<<FOLLOW_UP>>>\n[{\"title\":\"x\"}]\n<<<END>>>"
    r = parse(text)
    # NEEDS_INPUT + a non-conflicting FOLLOW_UP block is still HELD, no follow-up.
    assert r.outcome is Outcome.HELD
    assert r.follow_up is None


def test_parse_follow_ups_list():
    ups = parse_follow_ups('[{"title":"a","urgent":true,"body":"x"},{"title":"b"}]')
    assert ups is not None and len(ups) == 2
    assert ups[0].title == "a" and ups[0].urgent is True and ups[0].body == "x"
    assert ups[1].title == "b" and ups[1].urgent is False and ups[1].body == ""


def test_parse_follow_ups_dict_wrapper():
    ups = parse_follow_ups('{"tasks":[{"title":"a"}]}')
    assert ups is not None and ups[0].title == "a"


def test_parse_follow_ups_skips_titleless_and_rejects_garbage():
    assert parse_follow_ups('[{"body":"no title"}]') is None
    assert parse_follow_ups("not json") is None
    assert parse_follow_ups("[]") is None
