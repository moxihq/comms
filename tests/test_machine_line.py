"""The machine line: position zero, one line, sigil-marked, never prose."""

import pytest

from moxi_comms import machine_line as ml


def test_parses_recipients():
    body = "::to planner, reviewer@proj-b\nThe parser landed; the suite is green."
    parsed = ml.parse(body)
    assert parsed.recipients == ("planner", "reviewer@proj-b")
    assert parsed.text == "The parser landed; the suite is green."


def test_default_is_conversational():
    parsed = ml.parse("::to main\nwhat is the status?")
    assert parsed.posture == ml.POSTURE_OPEN
    assert parsed.recipients == ("main",)


def test_compose_round_trip():
    body = ml.compose(["a", "b@proj"], "hello\nworld")
    assert body.splitlines()[0] == "::to a, b@proj"
    assert ml.parse(body).text == "hello\nworld"


def test_tag_shaped_prose_is_not_routed():
    # a machine line anywhere but position zero is prose
    assert ml.parse("Here is what I would send:\n::to planner\nthanks") is None
    # naked `to:` prose never routes -- that is why the sigil exists
    assert ml.parse("to: planner, ship it") is None
    assert ml.parse("To: planner") is None


def test_quoted_text_does_not_collide():
    assert ml.parse('"to: planner, ship it" is what he said') is None
    assert ml.parse("> ::to planner\nquoted block") is None
    assert ml.parse("```\n::to planner\n```") is None


def test_line_zero_must_be_only_the_machine_line():
    assert ml.parse("::to planner and also tell reviewer\nbody") is None
    assert ml.parse("::to\nbody") is None
    assert ml.parse("::toplanner\nbody") is None


def test_operator_typeable_variants():
    # mobile autocapitalisation and a leading space are tolerated; the sigil is not
    assert ml.parse("::To planner\nhi").recipients == ("planner",)
    assert ml.parse("  ::to planner [NO REPLY]\nhi").posture == ml.POSTURE_NONE


def test_machine_line_only_body_has_empty_text():
    parsed = ml.parse("::to planner")
    assert parsed.recipients == ("planner",) and parsed.text == ""


# ------------------------------------------------------------- re:

def test_the_reply_token_carries_the_referent():
    parsed = ml.parse("::to planner@proj-a re:1535516000000000001\nanswer.")
    assert parsed.recipients == ("planner@proj-a",)
    assert parsed.reply_to == "1535516000000000001"
    assert parsed.text == "answer."


def test_the_reply_token_sits_between_the_tags_and_the_flag():
    parsed = ml.parse("::to a, b@proj re:1400000000001 [reply-once]\nhi")
    assert parsed.recipients == ("a", "b@proj")
    assert (parsed.reply_to, parsed.posture) == ("1400000000001", ml.POSTURE_ONCE)


def test_no_reply_token_means_none():
    assert ml.parse("::to planner [reply-once]\nhi").reply_to is None


def test_the_reply_token_is_case_insensitive_like_the_sigil():
    assert ml.parse("  ::TO planner RE:1400000000009 [Reply-Once]\nhi").reply_to == \
        "1400000000009"


def test_a_malformed_reply_token_makes_the_line_prose():
    # a non-digit id is not a snowflake -- and a half-parsed routing line is worse than
    # no routing line, so line zero fails whole (the ANY-other-content rule).
    assert ml.parse("::to planner re:not-a-snowflake\nhi") is None
    assert ml.parse("::to planner re:\nhi") is None
    # at most ONE re: per message
    assert ml.parse("::to planner re:1400000000001 re:1400000000002\nhi") is None
    # position is part of the grammar: after the tags, before the flag
    assert ml.parse("::to planner [reply-once] re:1400000000001\nhi") is None
    assert ml.parse("::to re:1400000000001 planner\nhi") is None
    # and a re: with no ::to is prose like any other line-zero text
    assert ml.parse("re:1400000000001\nhi") is None
    assert ml.parse("re: 1400000000001 planner") is None


def test_reply_token_round_trips_through_compose():
    body = ml.compose(["planner@proj-a"], "answer.", reply_to="1400000000123")
    assert body.splitlines()[0] == "::to planner@proj-a re:1400000000123"
    assert ml.parse(body).reply_to == "1400000000123"
    assert ml.parse(body).text == "answer."
    both = ml.compose(["a"], "x", posture=ml.POSTURE_ONCE, reply_to="1400000000124")
    assert both.splitlines()[0] == "::to a re:1400000000124 [reply-once]"
    assert ml.parse(both).reply_to == "1400000000124"
    assert ml.parse(both).posture == ml.POSTURE_ONCE


def test_a_spoofed_referent_is_routing_data_not_an_error():
    # spoofability is accepted for every tag: a hand-typed id that points nowhere parses,
    # exactly like a tag naming an identity that does not exist.
    assert ml.parse("::to planner re:999999999999999999\nhi").reply_to == \
        "999999999999999999"


# ------------------------------------------------------- audience

def test_the_audience_mark_parses_between_re_and_the_flag():
    parsed = ml.parse("::to lead re:1400000000001 [audience:operator] [reply-once]\nhi")
    assert parsed.recipients == ("lead",)
    assert parsed.reply_to == "1400000000001"
    assert parsed.audience == "operator"
    assert parsed.posture == ml.POSTURE_ONCE
    assert parsed.text == "hi"


def test_the_audience_mark_stands_alone_and_absence_is_none():
    assert ml.parse("::to lead [audience:operator]\nhi").audience == "operator"
    assert ml.parse("::to lead [reply-once]\nhi").audience is None
    assert ml.parse("::to lead\nhi").audience is None


def test_the_audience_value_is_a_tag_and_may_be_scoped():
    assert ml.parse("::to a [audience:planner@proj-a]\nhi").audience == "planner@proj-a"
    assert ml.parse("::to a [audience:x.y_z-1]\nhi").audience == "x.y_z-1"


def test_the_audience_keyword_is_case_insensitive_and_the_value_is_lowercased():
    # one audience, whatever the phone's keyboard did to it
    for line in ("::to a [audience:Operator]", "::to a [AUDIENCE:OPERATOR]",
                 "  ::TO a RE:1400000000009 [Audience:operator] [Reply-Once]"):
        assert ml.parse(f"{line}\nhi").audience == "operator"
    # a scoped spelling is a different string, not the same audience
    assert ml.parse("::to a [audience:operator@global]\nhi").audience == "operator@global"


def test_a_malformed_audience_makes_the_whole_line_prose():
    bad = (
        "::to a [audience:]",                                  # empty value
        "::to a [audience:op erator]",                         # outside the charset
        "::to a [audience:op/erator]",
        "::to a [audience: operator]",                         # space is not a tag char
        "::to a [audience:operator",                           # unclosed
        "::to a audience:operator",                            # the brackets are typed
        "::to a [audience:operator] [audience:thinker]",       # at most one
        "::to a [audience:operator] re:1400000000001",         # before re: is out of place
        "::to a [reply-once] [audience:operator]",             # after the posture too
        "::to [audience:operator] a",                          # never part of the tag list
        "::to a, [audience:operator]",
    )
    for line in bad:
        assert ml.parse(f"{line}\nhi") is None, line


def test_audience_round_trips_through_compose():
    body = ml.compose(["thinker"], "answer.", reply_to="1400000000300",
                      audience="operator", posture=ml.POSTURE_ONCE)
    assert body.splitlines()[0] == \
        "::to thinker re:1400000000300 [audience:operator] [reply-once]"
    parsed = ml.parse(body)
    assert (parsed.reply_to, parsed.audience, parsed.posture) == \
        ("1400000000300", "operator", ml.POSTURE_ONCE)
    assert ml.format_line(["a"], audience="operator") == "::to a [audience:operator]"


def test_normalize_audience_is_the_parsers_grammar():
    assert ml.normalize_audience("Operator") == "operator"
    assert ml.normalize_audience("planner@Proj-A") == "planner@proj-a"
    for bad in ("", "op erator", "op/erator", "a@", "@b", "operator\n"):
        assert ml.normalize_audience(bad) is None, bad
    assert ml.normalize_audience(None) is None


# ---- comms/refine §3.8: `[held]` and the send's `[cold-at:…]` ([R11], [R70], test 22)

def test_held_is_written_in_the_old_slot_and_both_tokens_parse_as_held():
    line = ml.format_line(["a"], reply_to="9", next_turn=True, posture="once")
    assert line == "::to a re:9 [held] [reply-once]"
    for token in ("[held]", "[next-turn]", "[HELD]"):
        parsed = ml.parse(f"::to a re:9 {token} [reply-once]\nhi")
        assert parsed is not None and parsed.held, token
    assert not ml.parse("::to a\nhi").held
    assert ml.parse("::to a [held] [next-turn]\nhi") is None, "one token in the slot"


def test_the_cold_at_round_trips_and_a_dash_is_not_a_time():
    line = ml.format_line(["reviewer", "planner"], cold_at=(1760216142, "-"),
                          posture="once")
    assert line == "::to reviewer, planner [cold-at:1760216142,-] [reply-once]"
    parsed = ml.parse(line + "\nhi")
    assert parsed.cold_at == (1760216142, "-") and parsed.posture == "once"
    assert isinstance(parsed.cold_at[0], int) and parsed.cold_at[1] == ml.COLD_AT_LIVE
    body = ml.compose(["a"], "x", reply_to="3", cold_at=("-",), cap=2, open_mark=True)
    assert ml.parse(body).cold_at == ("-",) and ml.parse(body).cap == 2


def test_a_line_with_no_cold_at_carries_no_reading():
    assert ml.parse("::to a, b [no reply]\nhi").cold_at is None
    assert ml.parse("::to a [next-turn]\nold").cold_at is None
    assert ml.format_line(["a"]) == "::to a", "cold_at=None writes today's bytes"


@pytest.mark.parametrize("line", [
    "::to a, b [cold-at:1]",                 # one value for two tags
    "::to a [cold-at:1,2]",                  # two values for one tag
    "::to a [cold-at:]",
    "::to a [cold-at:x]",
    "::to a [cold-at:1, 2]",
    "::to a [cold-at:1] [held]",             # out of its slot
    "::to a [cold-at:1] [cold-at:1]",
])
def test_a_malformed_cold_at_makes_the_line_prose(line):
    assert ml.parse(line + "\nhi") is None


def test_the_writer_refuses_a_cold_at_it_could_not_read_back():
    with pytest.raises(ValueError):
        ml.format_line(["a", "b"], cold_at=(1,))
    with pytest.raises(ValueError):
        ml.format_line(["a"], cold_at=(1,), next_turn=True)
    for bad in ((-1,), (1.5,), ("x",), (True,)):
        with pytest.raises(ValueError):
            ml.format_line(["a"], cold_at=bad)
