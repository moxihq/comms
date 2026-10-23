"""The reply posture, from the token on line zero to the refusal at the verb.

A sender says what kind of exchange it wants: open, none (`[no reply]`) or once
(`[reply-once]`). A reply to a single-reply ask goes out
as `[no reply]` unless the replier asks back with `--posture once`. While a verified single-reply ask is open,
the receiver's plain `moxi comms send` to the asker posts nothing and names the cure: `moxi comms
reply <id>`, or `--new` for a message on another subject. The daemon's ask index
only nominates; the verb decides on its own read of the record, and every fact that is
missing or fails lets the send through. The operator is never refused.

Every rig test here runs on the mock and on the local backend (`BOTH_BACKENDS` in
conftest), and no test targets the Discord implementation. Tests that take no
rig are pure and run once.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import (no_cold_at, queued, send_turn, wait_for_queue, wait_until,
                      write_transcript)

from moxi_comms import attribution, cli, constants, cursors, directory, glossary
from moxi_comms import machine_line
from moxi_comms.adapters.claude_shim import frame_line
from moxi_comms.attribution import Author
from moxi_comms.client import DaemonClient, DaemonOffline, DaemonTimeout
from moxi_comms.daemon import ASK_INDEX_SLACK, Daemon, Refusal, Resolution
from moxi_comms.gateway import GatewayError, Message
from moxi_comms.mock_gateway import NO_MENTIONS
from moxi_comms.paths import directory_db
from moxi_comms.sender import SenderIdentity, credential_for

from test_cold_sends import bind  # noqa: F401 -- a shared fixture

PLANNER = dict(slot=1, name="planner")
REVIEWER = dict(slot=2, name="reviewer")
CRITIC = dict(slot=3, name="critic")
ARCHIVIST = dict(slot=5, name="archivist", env={"MOXI_COMMS_GLOBAL": "1"})

P, R, C = "planner", "reviewer", "critic"


# --------------------------------------------------------------------------- helpers

def posted(rig):
    return rig.gateway.history(rig.names_channel)


def line_zero(msg):
    return machine_line.parse(msg.content)


def last(rig):
    return posted(rig)[-1]


def raw_post(rig, slot, display, body):
    """Post a body exactly as given through a slot's webhook, wearing `display`."""
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(slot, display, "global"))
    finally:
        db.close()
    return rig.gateway.execute_webhook(cred, body, username=display,
                                       allowed_mentions=NO_MENTIONS)


def indexed(rig, ask_id, daemon=None):
    daemon = daemon or rig.daemon
    assert wait_until(lambda: ask_id in daemon.asks), f"ask {ask_id} never indexed"
    return daemon.asks[ask_id]


def open_ask(rig, to="reviewer", text="which branch?", *, who=PLANNER):
    """A verified single-reply ask: its author is bound, so the record proves it. Some
    recipients are never bound in their test, so it passes `--new-name`; on a
    name bound now or before that changes nothing."""
    code, out = rig.cli(["send", "--new-name", to, text, "--posture", "once"], **who)
    assert code == 0, out
    ask = last(rig)
    assert line_zero(ask).posture == "once"
    indexed(rig, ask.id)
    return ask


@pytest.fixture
def pair(rig):
    """planner and reviewer, both bound: a verified ask from planner is possible."""
    return rig.shim(**PLANNER), rig.shim(**REVIEWER)


def force_nomination(monkeypatch, rig, *ids):
    """The daemon nominates exactly these ids, whatever its index holds, so the verb's
    own predicate is the only thing deciding."""
    monkeypatch.setattr(rig.daemon, "_asks_query", lambda frame: {"asks": list(ids)})


def refused(code, out):
    return code == 2 and "Nothing was sent" in out and "still open" in out


class ReadFails:
    """The real gateway, except that one record read raises."""

    def __init__(self, inner, error, *, what="get_message"):
        self._inner, self._error, self._what = inner, error, what

    def __getattr__(self, name):
        if name == self._what:
            def fail(*args, **kwargs):
                raise self._error
            return fail
        return getattr(self._inner, name)


@pytest.fixture
def reboot():
    started = []

    def _reboot(rig):
        rig.daemon.stop()
        rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
        started.append(rig.daemon)
        return rig.daemon

    yield _reboot
    for d in started:
        d.stop()


# ----------------------------------------------------------------- (a) the parser

def test_the_token_parses_as_the_once_posture():
    ml = machine_line.parse("::to planner [reply-once]\nwhich branch?")
    assert (ml.posture, ml.recipients, ml.text) == \
        ("once", ("planner",), "which branch?")


def test_the_token_is_matched_case_insensitively():
    assert machine_line.parse("::TO planner [Reply-ONCE]\nx").posture == "once"
    assert machine_line.parse("::to planner [No Reply]\nx").posture == "none"
    assert machine_line.parse("::to planner [OPEN]\nx").open_mark


def test_open_none_and_an_old_speaker_mark():
    plain = machine_line.parse("::to planner\nx")
    speaker = machine_line.parse("::to planner [speaker]\nx")    # an old record
    none = machine_line.parse("::to planner [no reply]\nx")
    assert plain.posture == "open"
    assert speaker.posture == "open"
    assert none.posture == "none"


def test_the_token_sits_in_the_terminal_slot_after_every_other_token():
    ml = machine_line.parse("::to a, b@x re:123 [audience:operator] [next-turn] "
                            "[reply-once]\nx")
    assert (ml.reply_to, ml.audience, ml.held, ml.posture) == \
        ("123", "operator", True, "once")
    for out_of_place in ("::to a [reply-once] re:123", "::to a [reply-once] [next-turn]",
                         "::to a [reply-once] [audience:operator]",
                         "::to [reply-once] a", "::to a [reply-once] trailing"):
        assert machine_line.parse(out_of_place + "\nx") is None, out_of_place


def test_the_token_follows_an_old_speaker_mark_and_is_never_doubled():
    assert machine_line.parse("::to a [speaker] [reply-once]\nx").posture == "once"
    for line in ("::to a [reply-once] [speaker]", "::to a [reply-once] [reply-once]",
                 "::to a [speaker] [speaker]", "::to a [reply once]",
                 "::to a [replyonce]"):
        assert machine_line.parse(line + "\nx") is None, line


def test_format_line_writes_each_posture_in_the_slot():
    assert machine_line.format_line(["a"], posture="once") == "::to a [reply-once]"
    assert machine_line.format_line(["a"], posture="none") == "::to a [no reply]"
    assert machine_line.format_line(["a"], posture="open") == "::to a"
    line = machine_line.format_line(["a"], reply_to="9", audience="operator",
                                    next_turn=True, posture="once")
    assert line == "::to a re:9 [audience:operator] [held] [reply-once]"
    assert machine_line.parse(line + "\nx").posture == "once"


def test_a_bad_posture_raises():
    with pytest.raises(ValueError):
        machine_line.format_line(["a"], posture="twice")


# ------------------------------------------------------------------ (b) existing callers

def test_keyword_calls_give_todays_bytes_and_a_positional_one_fails():
    assert machine_line.format_line(["a", "b"], reply_to="123") == "::to a, b re:123"
    assert machine_line.format_line(["a"]) == "::to a"
    assert machine_line.compose(["a"], "t", reply_to="5") == "::to a re:5\nt"
    assert machine_line.compose(["a"], "t") == "::to a\nt"
    # Every parameter after `recipients` (and `text`) is keyword-only: a positional
    # call fails instead of binding to the wrong parameter.
    with pytest.raises(TypeError):
        machine_line.format_line(["a", "b"], False, "123")
    with pytest.raises(TypeError):
        machine_line.compose(["a"], "t", False, "5")


def test_bodies_with_a_text_only_namespace_gives_one_plain_body():
    """A caller outside comms compares its own render with `cli._bodies(args, ...)`,
    with an `args` that carries the text: no posture on `args`."""
    ctx = SimpleNamespace(gateway=SimpleNamespace(max_content_length=16_000),
                          say=lambda _t="": None)
    fits = SimpleNamespace(text="hello")
    assert cli._bodies(fits, ctx, ["reviewer"], None) == ["::to reviewer\nhello"]
    assert cli._bodies(fits, ctx, ["reviewer"], "77") == ["::to reviewer re:77\nhello"]
    long = SimpleNamespace(text="word " * 1000)
    assert cli._bodies(long, ctx, ["reviewer"], "77") == \
        [f"::to reviewer re:77\n{long.text}"], "one body: comms does not split"


# ----------------------------------------------------------------- (c) the verbs

@pytest.mark.parametrize("flags, token", [
    (["--posture", "open"], "[open]"),
    (["--posture", "none"], "[no reply]"), (["--posture", "once"], "[reply-once]")])
def test_send_writes_the_posture_it_is_given(rig, flags, token):
    code, out = rig.cli(["send", "--new-name", "reviewer", "hi", *flags], **PLANNER)
    assert code == 0, out
    expected = "::to reviewer" + (f" {token}" if token else "")
    assert no_cold_at(last(rig).content).splitlines()[0] == expected


@pytest.mark.parametrize("verb", ["send", "reply"])
@pytest.mark.parametrize("flag", [["--speaker"], ["--operator-words"], ["--cold"]])
def test_a_removed_flag_is_argparses_error_and_nothing_is_posted(rig, verb, flag):
    ask = rig.post_from_slot(6, [R], "q", display=P)
    argv = (["send", "planner", "hi", "--posture", "open"] if verb == "send"
            else ["reply", ask.id, "hi"])
    with pytest.raises(SystemExit) as exc:
        rig.cli([*argv, *flag], **REVIEWER)
    assert exc.value.code == 2
    assert posted(rig) == [ask]


def _reply_token(rig, referent_line, flags=(), *, verb="reply"):
    referent = raw_post(rig, 6, P, f"{referent_line}\nthe ask")
    argv = (["reply", referent.id, "answer", *flags] if verb == "reply" else
            ["send", "--new-name", "planner", "answer", "--reply-to", referent.id, *flags])
    code, out = rig.cli(argv, **REVIEWER)
    assert code == 0, out
    reply = line_zero(last(rig))
    assert reply.reply_to == referent.id
    return reply.posture


@pytest.mark.parametrize("verb", ["reply", "send"])
def test_the_reply_default_table(rig, verb):
    """open -> open, an old [speaker] -> open, once -> none; none and once passed are
    written, and open on a once ask is refused (`test_posture_intent.py` has every cell)."""
    assert _reply_token(rig, "::to reviewer", verb=verb) == "open"
    assert _reply_token(rig, "::to reviewer [speaker]", verb=verb) == "open"
    assert _reply_token(rig, "::to reviewer [reply-once]", verb=verb) == "none"
    for flags, want in ((["--posture", "once"], "once"), (["--posture", "none"], "none")):
        assert _reply_token(rig, "::to reviewer [reply-once]", flags,
                            verb=verb) == want, flags
    assert _reply_token(rig, "::to reviewer", ["--posture", "once"],
                        verb=verb) == "once"


def test_a_reply_to_a_referent_not_in_the_record_is_open(rig):
    code, out = rig.cli(["send", "--new-name", "planner", "hi", "--reply-to", "1400000009999"],
                        **REVIEWER)
    assert code == 0, out
    assert line_zero(last(rig)).posture == "open"


def test_the_reply_default_rides_the_wire_as_no_reply(rig, pair):
    ask = open_ask(rig)
    code, out = rig.cli(["reply", ask.id, "on main."], **REVIEWER)
    assert code == 0, out
    assert no_cold_at(last(rig).content).splitlines()[0] == \
        f"::to planner re:{ask.id} [no reply]"


# ------------------------------------------- (e) the plain-send refusal

def test_a_plain_send_to_the_asker_is_refused_on_a_verified_open_ask(rig, pair):
    ask = open_ask(rig)
    before = posted(rig)
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "planner", "on main."], **REVIEWER)
    assert refused(code, out), out
    assert f"{P} asked you for a single reply in msg {ask.id}" in out
    assert "still open" in out
    assert f'  moxi comms reply {ask.id} "text"' in out
    assert "add --new" in out
    assert posted(rig) == before


def test_the_multi_recipient_cure_keeps_every_recipient(rig, pair):
    ask = open_ask(rig)
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "planner,critic", "fyi both"], **REVIEWER)
    assert refused(code, out), out
    assert f'  moxi comms send planner,critic --reply-to {ask.id} "text"' in out


def test_several_open_asks_are_listed_newest_first_at_most_three(rig, pair):
    asks = [open_ask(rig, text=f"q{n}") for n in range(4)]
    code, out = rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER)
    assert refused(code, out), out
    assert "4 single-reply asks to you are still open" in out
    listed = [ln for ln in out.splitlines() if ln.startswith("  msg ")]
    assert [ln.split()[1] for ln in listed] == [a.id for a in reversed(asks)][:3]
    assert "(and 1 more)" in out
    assert f'moxi comms reply {asks[-1].id} "text"' in out


def test_an_unverified_ask_is_delivered_stamped_and_binds_nobody(rig):
    reviewer = rig.shim(**REVIEWER)
    ask = raw_post(rig, 6, P, "::to reviewer [reply-once]\nwhich branch?")
    payload = next(p for p in wait_for_queue(reviewer, 1) if p["message_id"] == ask.id)
    frame = frame_line(payload)
    assert "(unverified)" in frame and frame.endswith("[reply-once]")
    assert payload["posture"] == "once"
    assert ask.id not in rig.daemon.asks
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "planner", "on main."], **REVIEWER)
    assert code == 0, out
    assert "single-reply" not in out
    assert line_zero(last(rig)).text == "on main."


def test_a_closed_ask_lets_the_plain_send_through(rig, pair):
    ask = open_ask(rig)
    assert rig.cli(["reply", ask.id, "on main."], **REVIEWER)[0] == 0
    assert wait_until(lambda: R not in rig.daemon.asks[ask.id]["open"])
    code, out = rig.cli(["send", "--posture", "none", "planner", "something else"], **REVIEWER)
    assert code == 0, out


def test_a_reply_followed_at_once_by_a_plain_send_is_not_refused(rig, pair, monkeypatch):
    """The index may not have seen the reply yet; the verb reads the record."""
    ask = open_ask(rig)
    force_nomination(monkeypatch, rig, ask.id)     # as if the reply were still unrouted
    assert rig.cli(["reply", ask.id, "on main."], **REVIEWER)[0] == 0
    code, out = rig.cli(["send", "--posture", "none", "planner", "and one more thing"], **REVIEWER)
    assert code == 0, out


def test_an_expired_ask_lets_the_plain_send_through(rig, pair):
    ask = open_ask(rig)
    rig.age_message(ask.id, time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - constants.ASK_TTL - 60)))
    code, out = rig.cli(["send", "--posture", "none", "planner", "late"], **REVIEWER)
    assert code == 0, out


def test_the_daemon_stops_nominating_an_ask_past_its_ttl(rig, pair):
    ask = open_ask(rig)
    frame = {"sender": R, "recipients": [P]}
    assert rig.daemon._asks_query(frame)["asks"] == [ask.id]
    rig.clock.advance(constants.ASK_TTL + 1)
    assert rig.daemon._asks_query(frame)["asks"] == []


def test_a_send_that_does_not_include_the_asker_is_not_checked(rig, pair):
    open_ask(rig)
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "critic", "unrelated"], **REVIEWER)
    assert code == 0, out


def test_the_verb_refuses_only_when_the_asker_is_a_recipient_of_this_send(rig, pair,
                                                                          monkeypatch):
    """A nomination that names an ask whose asker is not
    among this send's recipients lets the send through, whatever the daemon says."""
    ask = open_ask(rig)
    force_nomination(monkeypatch, rig, ask.id)
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "critic", "unrelated"], **REVIEWER)
    assert code == 0, out
    assert refused(*rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER)), "the control"


def test_a_held_plain_send_to_the_asker_is_refused(rig, pair):
    """`--held` never skips the single-reply check, and the cure carries it."""
    ask = open_ask(rig)
    before = posted(rig)
    code, out = rig.cli(["send", "--posture", "none", "planner", "later", "--held"], **REVIEWER)
    assert refused(code, out), out
    assert f'moxi comms reply {ask.id} "text" --held' in out
    assert posted(rig) == before


def test_the_asker_itself_and_other_recipients_are_not_bound_to_each_other(rig, pair):
    """Critic is bound, so the ask is handed to it and binds it: an ask still queued
    for a recipient binds that recipient only once a release point hands it over."""
    rig.shim(**CRITIC)
    ask = open_ask(rig, to="reviewer,critic")
    assert indexed(rig, ask.id)["open"] == {R, C}
    code, out = rig.cli(["send", "--posture", "none", "reviewer", "more context"], **PLANNER)
    assert code == 0, out
    code, out = rig.cli(["send", "--posture", "none", "planner", "answer?"], **CRITIC)
    assert refused(code, out), "critic is a recipient too"


# ---- failure branches: each lets the send through, with its NOTE

@pytest.mark.parametrize("error", [DaemonTimeout("timeout waiting for query"),
                                   DaemonOffline("comms offline: daemon closed the "
                                                 "connection")])
def test_a_daemon_that_does_not_answer_the_query_lets_the_send_through(rig, pair,
                                                                       monkeypatch, error):
    """A timeout, and a connection closed between the
    resolve and the query: both are a daemon that did not answer."""
    open_ask(rig)
    real = DaemonClient.query

    def query(self, kind, **kwargs):
        if kind == "asks":
            raise error
        return real(self, kind, **kwargs)

    monkeypatch.setattr(DaemonClient, "query", query)
    code, out = rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER)
    assert code == 0, out
    assert cli.ASK_CHECK_NO_ANSWER in out


def test_any_other_daemon_error_lets_the_send_through_quoting_it(rig, pair, monkeypatch):
    open_ask(rig)

    def broken(frame):
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(rig.daemon, "_asks_query", broken)
    code, out = rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER)
    assert code == 0, out
    assert "NOTE: the single-reply check did not run (the daemon said: " in out
    assert "the index is on fire" in out


@pytest.mark.parametrize("what", ["get_message", "history"])
@pytest.mark.parametrize("error", [GatewayError("500: the record is unreadable"),
                                   DaemonOffline("comms offline"),
                                   sqlite3.OperationalError("disk I/O error"),
                                   RuntimeError("an unforeseen defect")])
def test_a_failed_record_read_lets_the_send_through_naming_the_ask(rig, pair, what, error):
    ask = open_ask(rig)
    code, out = rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER,
                        gateway=ReadFails(rig.gateway, error, what=what))
    assert code == 0, out
    assert f"NOTE: the single-reply check could not read msg {ask.id}" in out
    assert str(error) in out
    assert line_zero(last(rig)).text == "hi"


@pytest.mark.parametrize("seam", ["db", "room"])
def test_a_failed_directory_read_lets_the_single_reply_check_through(rig, pair,
                                                                     monkeypatch, seam):
    ask = open_ask(rig)
    ctx = cli.Context(gateway=rig.gateway, db_path=directory_db(), clock=rig.clock)
    sender = SenderIdentity.for_slot(REVIEWER["slot"], R, "global")

    if seam == "db":
        class BrokenDb:
            def __enter__(self):
                raise sqlite3.OperationalError("directory unreadable")

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(ctx, "db", BrokenDb)
    else:
        def broken_room(*_args):
            raise sqlite3.OperationalError("room unreadable")

        monkeypatch.setattr(directory, "room_by_name", broken_room)

    found, notes = cli._open_asks(ctx, sender, [ask.id], [P])
    assert found == []
    failure = "directory unreadable" if seam == "db" else "room unreadable"
    assert notes == [f"NOTE: the single-reply check could not read msg {ask.id} from the "
                     f"record ({failure}), so it did not hold this send."]


# ---------------------------------- (d) open_for, each clause falsified alone

def test_control_the_predicate_holds_through_a_forced_nomination(rig, pair, monkeypatch):
    ask = open_ask(rig)
    force_nomination(monkeypatch, rig, ask.id)
    assert refused(*rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER))


def test_clause_1_a_message_that_is_not_once_binds_nobody(rig, pair, monkeypatch):
    rig.cli(["send", "--posture", "none", "reviewer", "open question"], **PLANNER)
    force_nomination(monkeypatch, rig, last(rig).id)
    assert rig.cli(["send", "--new-name", "--posture", "none", "planner", "hi"], **REVIEWER)[0] == 0


def test_clause_2_an_unverified_ask_binds_nobody(rig, monkeypatch):
    ask = raw_post(rig, 6, P, "::to reviewer [reply-once]\nq")
    force_nomination(monkeypatch, rig, ask.id)
    assert rig.cli(["send", "--new-name", "--posture", "none", "planner", "hi"], **REVIEWER)[0] == 0


def test_clause_2_an_ask_does_not_bind_its_own_author(rig, pair, monkeypatch):
    ask = open_ask(rig, to="planner,reviewer", who=REVIEWER)
    force_nomination(monkeypatch, rig, ask.id)
    assert rig.cli(["send", "--posture", "none", "reviewer", "note to self"], **REVIEWER)[0] == 0


def test_clause_3_an_ask_to_someone_else_binds_nobody_else(rig, pair, monkeypatch):
    ask = open_ask(rig, to="critic")
    force_nomination(monkeypatch, rig, ask.id)
    assert rig.cli(["send", "--new-name", "--posture", "none", "planner", "hi"], **REVIEWER)[0] == 0


def test_clause_4_an_expired_ask_binds_nobody(rig, pair, monkeypatch):
    ask = open_ask(rig)
    rig.age_message(ask.id, time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - constants.ASK_TTL - 60)))
    force_nomination(monkeypatch, rig, ask.id)
    assert rig.cli(["send", "--new-name", "--posture", "none", "planner", "hi"], **REVIEWER)[0] == 0


def test_clause_5_a_threaded_reply_to_the_asker_closes_it(rig, pair, monkeypatch):
    ask = open_ask(rig)
    rig.cli(["send", "planner", "on main.", "--reply-to", ask.id], **REVIEWER)
    force_nomination(monkeypatch, rig, ask.id)
    assert rig.cli(["send", "--new-name", "--posture", "none", "planner", "hi"], **REVIEWER)[0] == 0


def test_clause_5_a_reply_that_does_not_address_the_asker_does_not_close(rig, pair,
                                                                        monkeypatch):
    ask = open_ask(rig)
    code, out = rig.cli(["send", "--new-name", "critic", "about that ask", "--reply-to", ask.id],
                        **REVIEWER)
    assert code == 0, out
    force_nomination(monkeypatch, rig, ask.id)
    assert refused(*rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER))


def test_clause_5_someone_elses_reply_does_not_close_mine(rig, pair, monkeypatch):
    ask = open_ask(rig, to="reviewer,critic")
    assert rig.cli(["reply", ask.id, "critic's answer"], **CRITIC)[0] == 0
    force_nomination(monkeypatch, rig, ask.id)
    assert refused(*rig.cli(["send", "--posture", "none", "planner", "hi"], **REVIEWER))
    assert rig.cli(["send", "--posture", "none", "planner", "hi"], **CRITIC)[0] == 0


# The predicate on its own, over hand-built messages: the #names half of clause 1 and
# each other clause, against one base ask that holds.

NAMES_CHANNEL = "c-names"


def _msg(mid, content, *, channel=NAMES_CHANNEL, ts=None):
    stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return Message(id=str(mid), channel_id=channel, author_id="w", author_kind="webhook",
                   author_display="x", content=content, timestamp=stamp)


@pytest.fixture
def pure(monkeypatch):
    """`from_record` answers from the display this test gives each message."""
    authors = {}

    def from_record(db, msg):
        return authors[msg.id]

    monkeypatch.setattr(attribution, "from_record", from_record)
    sender = SenderIdentity(qualified=R, scope="global")
    planner = Author("planner", "global", P, "chosen", attribution.VERIFIED)
    reviewer = Author("reviewer", "global", R, "chosen", attribution.LABELED)
    return SimpleNamespace(authors=authors, sender=sender, planner=planner,
                           reviewer=reviewer)


def _open_for(pure, ask, replies=()):
    return cli.open_for(None, ask, pure.sender, now=time.time(),
                        names_channel=NAMES_CHANNEL, replies=lambda: list(replies))


def test_open_for_holds_on_the_base_ask(pure):
    ask = _msg(100, "::to reviewer [reply-once]\nq")
    pure.authors[ask.id] = pure.planner
    assert _open_for(pure, ask) is pure.planner


def test_open_for_each_clause_alone(pure):
    base = "::to reviewer [reply-once]\nq"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - constants.ASK_TTL - 1))
    cases = {
        "1 not #names": (_msg(101, base, channel="c-other"), pure.planner),
        "1 open": (_msg(102, "::to reviewer\nq"), pure.planner),
        "1 prose": (_msg(103, "reviewer [reply-once]\nq"), pure.planner),
        "2 unverified": (_msg(105, base), Author("planner", "global", P, "chosen",
                                                 attribution.LABELED)),
        "2 own": (_msg(106, "::to reviewer, planner [reply-once]\nq"),
                  Author("reviewer", "global", R, "chosen", attribution.VERIFIED)),
        "3 not to me": (_msg(107, "::to critic [reply-once]\nq"), pure.planner),
        "4 expired": (_msg(108, base, ts=old), pure.planner),
    }
    for label, (ask, author) in cases.items():
        pure.authors[ask.id] = author
        assert _open_for(pure, ask) is None, label


def test_open_for_clause_5_reads_only_the_senders_replies_to_the_asker(pure):
    ask = _msg(200, "::to reviewer [reply-once]\nq")
    pure.authors[ask.id] = pure.planner
    mine = _msg(201, "::to planner re:200 [speaker]\na")
    pure.authors[mine.id] = pure.reviewer
    assert _open_for(pure, ask, [mine]) is None
    elsewhere = _msg(202, "::to critic re:200\na")
    pure.authors[elsewhere.id] = pure.reviewer
    theirs = _msg(203, "::to planner re:200\na")
    pure.authors[theirs.id] = Author("critic", "global", C, "chosen", attribution.VERIFIED)
    other_ref = _msg(204, "::to planner re:199\na")
    pure.authors[other_ref.id] = pure.reviewer
    assert _open_for(pure, ask, [elsewhere, theirs, other_ref]) is pure.planner


def test_open_for_does_not_read_replies_unless_clauses_1_to_5_hold(pure):
    ask = _msg(300, "::to critic [reply-once]\nq")
    pure.authors[ask.id] = pure.planner

    def boom():
        raise AssertionError("read the record for an ask that binds nobody")

    assert cli.open_for(None, ask, pure.sender, now=time.time(),
                        names_channel=NAMES_CHANNEL, replies=boom) is None


# ------------------------------------------------------------ (f) the operator

def test_the_operators_terminal_is_never_refused(rig):
    if rig.backend != "local":
        pytest.skip("the credentialled operator lane exists on the local backend alone")
    rig.shim(**PLANNER)
    rig.shim(slot=4, name="operator", global_=True)
    ask = open_ask(rig, to="operator")
    assert indexed(rig, ask.id)["open"] == {"operator"}
    assert rig.daemon._asks_query({"sender": "operator", "recipients": [P]})["asks"] == \
        [ask.id], "the daemon nominates it; only the operator rule lets the send through"
    code, out = rig.cli(["send", "--posture", "none", "planner", "not that"],
                        env={"MOXI_COMMS_OPERATOR": "900001"})
    assert code == 0, out
    assert "single-reply" not in out


def test_an_agent_lane_sender_claiming_operator_is_never_refused(rig):
    rig.shim(**PLANNER)
    rig.shim(slot=4, name="operator", global_=True)
    ask = open_ask(rig, to="operator")
    assert rig.daemon._asks_query({"sender": "operator", "recipients": [P]})["asks"] == \
        [ask.id]
    code, out = rig.cli(["send", "--posture", "none", "planner", "spoken, not that"], slot=4,
                        name="operator", env={"MOXI_COMMS_GLOBAL": "1"})
    assert code == 0, out
    assert "single-reply" not in out


def test_the_operators_reply_to_an_agents_ask_still_carries_the_default(rig):
    rig.shim(**PLANNER)
    rig.shim(slot=4, name="operator", global_=True)
    ask = open_ask(rig, to="operator")
    code, out = rig.cli(["reply", ask.id, "yes"], slot=4, name="operator",
                        env={"MOXI_COMMS_GLOBAL": "1"})
    assert code == 0, out
    assert line_zero(last(rig)).posture == "none", "a stamp, not a refusal"


# ------------------------------------------------------------- (g) archivist is bound

def test_an_agents_ask_binds_archivist_as_an_agent(rig):
    rig.shim(**PLANNER)
    rig.shim(slot=5, name="archivist", global_=True)
    ask = open_ask(rig, to="archivist")
    code, out = rig.cli(["send", "--posture", "none", "planner", "unrelated"], **ARCHIVIST)
    assert refused(code, out), out
    assert f"msg {ask.id}" in out
    code, out = rig.cli(["reply", ask.id, "the answer"], **ARCHIVIST)
    assert code == 0, out


# ------------------------------- (h) a second reply, (i) a reply to an old [speaker]

def test_a_second_reply_to_an_answered_ask_posts(rig, pair):
    ask = open_ask(rig)
    assert rig.cli(["reply", ask.id, "on main."], **REVIEWER)[0] == 0
    code, out = rig.cli(["reply", ask.id, "correction: on reply-posture."], **REVIEWER)
    assert code == 0, out
    second = line_zero(last(rig))
    assert (second.reply_to, second.posture) == (ask.id, "none")
    code, out = rig.cli(["send", "planner", "third", "--reply-to", ask.id], **REVIEWER)
    assert code == 0, out
    entry = rig.daemon.asks[ask.id]
    assert R not in entry["open"]


def test_a_reply_to_an_old_speaker_message_is_not_refused(rig, pair):
    announced = raw_post(rig, 6, P, "::to reviewer [speaker]\nfyi")
    code, out = rig.cli(["reply", announced.id, "noted, and one thought"], **REVIEWER)
    assert code == 0, out
    assert line_zero(last(rig)).posture == "open"


# --------------------------------------------------------------- (k) the index

def test_a_rescan_reparse_changes_nothing_and_a_closed_name_never_reopens(rig, pair):
    ask = open_ask(rig, to="reviewer,critic")
    assert rig.cli(["reply", ask.id, "done"], **REVIEWER)[0] == 0
    assert wait_until(lambda: rig.daemon.asks[ask.id]["open"] == {C})
    rig.daemon.rescan()
    assert rig.daemon.asks[ask.id]["open"] == {C}, "the re-parse reopened nothing"


def test_the_boot_rescan_rebuilds_the_index(rig, pair, reboot):
    ask = open_ask(rig, to="reviewer,critic")
    assert rig.cli(["reply", ask.id, "done"], **REVIEWER)[0] == 0
    daemon = reboot(rig)
    assert daemon.asks[ask.id]["asker"] == P
    assert daemon.asks[ask.id]["open"] == {C}


def test_the_index_is_bounded_to_the_rescan_window(rig):
    daemon = rig.daemon
    author = Author("planner", "global", P, "chosen", attribution.VERIFIED)
    target = Resolution(R, "reviewer", "global", R, "chosen", None)
    ml = machine_line.parse("::to reviewer [reply-once]\nq")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    top = 1_400_000_000_000 + 3 * constants.RESCAN_LIMIT
    for n in range(1_400_000_000_000, top):
        msg = SimpleNamespace(id=str(n), timestamp=stamp)
        daemon._index_ask(msg, ml, author, target, opens=True, opening=None)
        assert len(daemon.asks) <= constants.RESCAN_LIMIT + ASK_INDEX_SLACK
    assert str(top - 1) in daemon.asks
    assert str(top - constants.RESCAN_LIMIT - ASK_INDEX_SLACK - 1) not in daemon.asks


def test_the_index_only_opens_what_it_has_not_held(rig):
    daemon = rig.daemon
    author = Author("planner", "global", P, "chosen", attribution.VERIFIED)
    ml = machine_line.parse("::to reviewer, critic [reply-once]\nq")
    msg = SimpleNamespace(id="1400000000001", timestamp="2026-09-29T00:00:00Z")
    rev = Resolution(R, "reviewer", "global", R, "chosen", None)
    cri = Resolution(C, "critic", "global", C, "chosen", None)
    opening = daemon._index_ask(msg, ml, author, rev, opens=True, opening=None)
    daemon._index_ask(msg, ml, author, cri, opens=True, opening=opening)
    assert daemon.asks[msg.id]["open"] == {R, C}
    daemon.asks[msg.id]["open"].discard(R)                  # closed
    again = daemon._index_ask(msg, ml, author, rev, opens=True, opening=None)
    daemon._index_ask(msg, ml, author, cri, opens=True, opening=again)
    assert again is False and daemon.asks[msg.id]["open"] == {C}


def test_a_send_during_the_boot_race_passes_and_is_refused_once_the_rescan_lands(
        rig, pair, monkeypatch):
    ask = open_ask(rig)
    # Handled before the stop: the cursors pass the ask while both are bound, so the
    # unbind at the stop leaves it no row (a cursor moved later deletes no row).
    for name in (P, R):
        cursors.advance(name, ask.id)
    rig.daemon.stop()
    gate = threading.Event()
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    rescan = daemon.rescan

    def held_rescan():
        gate.wait(10)
        rescan()

    monkeypatch.setattr(daemon, "rescan", held_rescan)
    rig.daemon = daemon
    booting = threading.Thread(target=daemon.start, daemon=True)
    booting.start()
    try:
        assert wait_until(lambda: daemon._sub is not None and daemon.bound)
        code, out = rig.cli(["send", "--posture", "none", "planner", "before the rescan"], **REVIEWER)
        assert code == 0, out
        # It passed because the index was empty, not because the check did
        # not run.
        assert cli.ASK_CHECK_NO_ANSWER not in out
        assert "NOTE: the single-reply" not in out
    finally:
        gate.set()
        booting.join(10)
    assert ask.id in daemon.asks
    assert refused(*rig.cli(["send", "--posture", "none", "planner", "after the rescan"], **REVIEWER))


def test_block_indexes_nothing(rig, pair):
    from conftest import write_config
    write_config(None, "block = true\n")
    first = raw_post(rig, 1, P, "::to reviewer [reply-once]\nq")
    time.sleep(0.3)
    assert rig.daemon.asks == {}
    # the positive control: the same body with `block` off is indexed.
    write_config(None, "block = false\n")
    second = raw_post(rig, 1, P, "::to reviewer [reply-once]\nq")

    assert indexed(rig, second.id)["open"] == {R}
    assert first.id not in rig.daemon.asks


# --------------------------------------------------------------------- (l) --new

def test_new_posts_on_an_open_ask_with_nothing_added(rig, pair):
    planner, _ = pair
    ask = open_ask(rig)
    code, out = rig.cli(["send", "--posture", "none", "planner", "a different subject", "--new"], **REVIEWER)
    assert code == 0, out
    msg = last(rig)
    assert no_cold_at(msg.content) == "::to planner [no reply]\na different subject"
    payload = next(p for p in wait_for_queue(planner, 1) if p["message_id"] == msg.id)
    assert (payload["posture"], payload["reply_to"]) == ("none", None)
    assert "speaker" not in payload
    assert "new" not in payload
    assert R in rig.daemon.asks[ask.id]["open"], "--new does not close the ask"


def test_new_with_no_open_ask_changes_nothing(rig):
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "planner", "hello", "--new"], **REVIEWER)
    assert code == 0, out
    assert no_cold_at(last(rig).content) == "::to planner [no reply]\nhello"


def test_new_with_reply_to_is_refused_and_posts_nothing(rig):
    ask = rig.post_from_slot(6, [R], "q", display=P)
    code, out = rig.cli(["send", "planner", "hi", "--new", "--reply-to", ask.id],
                        **REVIEWER)
    assert code == 2, out
    assert "--new" in out and "--reply-to" in out and "Nothing was sent" in out
    assert posted(rig) == [ask]


def test_reply_does_not_take_new(rig):
    ask = rig.post_from_slot(6, [R], "q", display=P)
    with pytest.raises(SystemExit) as e:
        rig.cli(["reply", ask.id, "hi", "--new"], **REVIEWER)
    assert e.value.code == 2
    assert posted(rig) == [ask]


# ------------------------------------------ (m) the payload, the frame, help, history

def test_the_payload_carries_the_posture_live(rig):
    reviewer = rig.shim(**REVIEWER)
    for flags, posture in ((["--posture", "open"], "open"),
                           (["--posture", "none"], "none"),
                           (["--posture", "once"], "once")):
        rig.cli(["send", "--new-name", "reviewer", "hi", *flags], **PLANNER)
        mid = last(rig).id
        wait_until(lambda: any(p["message_id"] == mid for p in reviewer.queue))
        p = next(p for p in reviewer.queue if p["message_id"] == mid)
        assert (p["posture"], p["backlog"]) == (posture, False)


def test_the_payload_carries_the_posture_on_release_at_bind(rig):
    """An ask to an unbound reviewer waits in its queue; the bind's release hands it,
    `held`, with its posture."""
    rig.shim(**PLANNER)
    ask = open_ask(rig)
    reviewer = rig.shim(**REVIEWER)
    p = next(p for p in wait_for_queue(reviewer, 1) if p["message_id"] == ask.id)
    assert (p["posture"], p.get("held")) == ("once", True)


def test_the_payload_carries_the_posture_on_rescan(rig, pair):
    """The rescan routes through `_deliver` like live routing and replay. The shim drops
    an id it already holds, so the frame is read off the daemon's side of the socket."""
    _, reviewer = pair
    ask = open_ask(rig)
    wait_until(lambda: any(p["message_id"] == ask.id for p in reviewer.queue))
    binding = rig.daemon.bindings[R]
    sent, real_send = [], binding.conn.send

    def record(frame):
        sent.append(frame)
        return real_send(frame)

    binding.conn.send = record
    with rig.daemon._lock:
        binding.delivered.clear()
    rig.daemon.rescan()
    (again,) = [f["payload"] for f in sent
                if f.get("op") == "deliver" and f["payload"]["message_id"] == ask.id]
    assert (again["posture"], again["backlog"]) == ("once", True)


def _payload(**extra):
    base = {"message_id": "101", "channel_id": "c1", "room": "#names",
            "author": {"name": P, "tier": "chosen", "attribution": "verified"},
            "recipients": [R], "posture": "open", "backlog": False,
            "text": "x", "reply_to": None, "audience": None, "audience_source": None,
            "timestamp": "2026-09-29T00:00:00Z"}
    base.update(extra)
    return base


def test_the_frame_line_renders_the_posture():
    assert frame_line(_payload()) == f"[comms] from {P} · msg 101"
    # a payload from an older daemon still carrying the old mark keys shows none
    assert frame_line(_payload(speaker=True, operator_words=True, via_phone=True)) == \
        f"[comms] from {P} · msg 101"
    assert frame_line(_payload(posture="none")).endswith(" · [no reply]")
    assert frame_line(_payload(posture="once")) == \
        f"[comms] from {P} · msg 101 · [reply-once]"
    unverified = frame_line(_payload(posture="once", author={
        "name": P, "tier": "chosen", "attribution": "labeled"}))
    assert "(unverified)" in unverified and unverified.endswith(" · [reply-once]")


def test_a_body_that_prints_the_token_is_not_a_posture():
    ml = machine_line.parse("::to reviewer\n::to planner [reply-once]\n[reply-once]")
    assert ml.posture == "open"


@pytest.mark.parametrize("term", ["posture", "[reply-once]", "reply once", "reply-once",
                                  "once", "open"])
def test_the_glossary_teaches_the_posture(term):
    entry = glossary.lookup(term)
    assert entry is not None and entry.term == "posture"
    for needle in ("`[reply-once]`", "`moxi comms reply`", "--new", "(unverified)",
                   "`--posture`", "`[no reply]`"):
        assert needle in entry.text


def test_the_speaker_entry_is_gone():
    assert glossary.lookup("speaker") is None


def test_history_renders_the_token_and_not_an_old_speaker_mark(rig):
    ask = raw_post(rig, 6, P, "::to reviewer [reply-once]\nq")
    note = raw_post(rig, 6, P, "::to reviewer [speaker]\nfyi")
    plain = raw_post(rig, 6, P, "::to reviewer\nhi")
    code, out = rig.cli(["history", R])
    assert code == 0, out
    rows = {ln.split()[0]: ln for ln in out.splitlines() if ln[:1].isdigit()}
    assert rows[ask.id].endswith("-> reviewer [reply-once]")
    assert rows[note.id].endswith("-> reviewer")
    assert rows[plain.id].endswith("-> reviewer")


# ------------------------------------------ ordering of the refusal, the hold and held asks

SESS_PLANNER = "44444444-4444-4444-8444-444444444444"


def test_a_refused_send_is_refused_before_the_cold_hold_and_the_cure_then_holds(rig):
    """Planner's cache is cold and planner holds a verified
    single-reply ask to reviewer. The plain send is refused, not held; the reply the
    refusal names is then posted and held for planner's own next turn (cold-hold: exit
    0, no re-run line); and the reply with
    `--held` posts, flagged."""
    planner = rig.shim(**PLANNER, session_id=SESS_PLANNER)
    rig.shim(**REVIEWER)
    ask = open_ask(rig)
    write_transcript(SESS_PLANNER, 2 * 3600)
    before = posted(rig)
    code, out = rig.cli(["send", "--new-name", "--posture", "none", "planner", "on main."], **REVIEWER)
    assert refused(code, out), out
    assert "held:" not in out
    assert posted(rig) == before
    code, out = rig.cli(["reply", ask.id, "on main."], **REVIEWER)
    assert code == 0 and "held: planner's cache went cold" in out, out
    assert "--held" not in out and out.splitlines()[-1].startswith("Sent and held: ")
    held_reply = last(rig)
    assert line_zero(held_reply).reply_to == ask.id
    assert wait_until(lambda: held_reply.id in queued(rig.daemon, "planner"))
    assert len(posted(rig)) == len(before) + 1
    code, out = rig.cli(["reply", ask.id, "on main.", "--held"], **REVIEWER)
    assert code == 0, out
    reply = line_zero(last(rig))
    assert (reply.reply_to, reply.posture, reply.held) == (ask.id, "none", True)


def test_a_held_ask_binds_only_once_it_is_handed_over(rig):
    """An ask sent `--posture once --held` sits in reviewer's queue until reviewer's
    turn. While it is held it binds nobody; once released and
    delivered it does."""
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    code, out = rig.cli(["send", "reviewer", "when you get to it: which branch?",
                         "--posture", "once", "--held"], **PLANNER)
    assert code == 0, out
    ask = last(rig)
    indexed(rig, ask.id)
    assert wait_until(lambda: ask.id in queued(rig.daemon, R))
    assert ask.id not in [p["message_id"] for p in reviewer.queue]
    code, out = rig.cli(["send", "--posture", "none", "planner", "unrelated status"], **REVIEWER)
    assert code == 0, out
    assert "single-reply" not in out
    send_turn(reviewer)
    assert wait_until(lambda: any(p["message_id"] == ask.id for p in reviewer.queue))
    assert ask.id in queued(rig.daemon, R), "the row stays until reviewer names it handled"
    code, out = rig.cli(["send", "--posture", "none", "planner", "more status"], **REVIEWER)
    assert refused(code, out), out
    assert f"msg {ask.id}" in out


def test_the_cure_carries_the_flags_a_reply_takes_and_leaves_out_audience(rig, pair,
                                                                          monkeypatch):
    """The cure line repeats the flags the reply also takes."""
    ask = open_ask(rig)
    code, out = rig.cli(["send", "planner", "x", "--posture", "once",
                         "--audience", "operator"], **REVIEWER)
    assert refused(code, out), out
    cure = next(ln for ln in out.splitlines() if ln.startswith("  moxi comms "))
    assert cure == f'  moxi comms reply {ask.id} "text" --posture once'
    code, out = rig.cli(["send", "--new-name", "planner,critic", "x", "--held",
                         "--posture", "open"], **REVIEWER)
    assert refused(code, out), out
    cure = next(ln for ln in out.splitlines() if ln.startswith("  moxi comms "))
    assert cure == (f'  moxi comms send planner,critic --reply-to {ask.id} "text" '
                    "--held"), "--posture open is not carried"
    code, out = rig.cli(["send", "--posture", "open", "planner", "x"], **REVIEWER)
    assert f'  moxi comms reply {ask.id} "text"\n' in out, "nothing carried, nothing added"


def test_the_cure_carries_held_to_every_adapter(rig, bind):
    """`--held` is accepted for every recipient, so the cure carries it to an
    asker on another adapter too."""
    bind(**PLANNER, adapter="codex")

    rig.shim(**REVIEWER)
    ask = open_ask(rig)

    code, out = rig.cli(["send", "planner", "answer", "--posture", "none",
                         "--held"], **REVIEWER)
    assert refused(code, out), out
    assert next(line for line in out.splitlines() if line.startswith("  moxi comms ")) == \
        f'  moxi comms reply {ask.id} "text" --held --posture none'
    assert f"msg {ask.id}" in out


def test_cure_flags_in_their_order():

    args = SimpleNamespace(held=True, standing=True, posture=None,
                           audience="operator")
    assert cli._cure_flags(args) == " --held --standing"
    args = SimpleNamespace(held=False, standing=False, posture="open",
                           audience=None)
    assert cli._cure_flags(args) == "", "a reply to a once ask refuses --posture open"
    args = SimpleNamespace(held=False, standing=False, posture="none",
                           audience=None)
    assert cli._cure_flags(args) == " --posture none"
    assert cli._cure_flags(SimpleNamespace(text="x")) == ""
