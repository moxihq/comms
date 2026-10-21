"""comms' Claude adapter process as a turns caller (design §5.9, §5.2, §5.7's outcomes).

The adapter runs a `ClaudeShim` on the rig's daemon and reaches a stand-in endpoint that
speaks turns' socket protocol (`turns_endpoint.FakeEndpoint`) through the real
`moxi_turns` library. The endpoint plays the session: its turns, its SessionStarts, its
Stops, and its going away.

What is held here:
- before the endpoint is found the adapter holds mail and the daemon reports the bound
  name as having no endpoint, which a send reports as `waiting`;
- the first look holds the session, learns its id and `turned`, and is a release point;
- one digest at a time, at turn boundaries only, settled from turns' outcome: `answer`
  handled; `aborted`, `error`, `refusal`, `Refused` and `Lost` unhandled;
- `Lost` that is untied keeps the endpoint; any other `Lost`, or the endpoint's end,
  drops it, tells the daemon, and the adapter looks again;
- the holder's answer at a Stop is the shim's reply check.
"""

from __future__ import annotations

import os
import time

import pytest
from conftest import clock_reads_warm, cursor_of, frame_kinds, wait_until
from turns_endpoint import FakeEndpoint

import moxi_turns
from moxi_comms import api, owed
from moxi_comms.adapters.claude_adapter import ClaudeAdapter

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = "reviewer@proj-a"
SESSION = "5e55a1d0-0000-4000-8000-000000000001"


@pytest.fixture
def backend():
    """The local record: the library opens the box's record by the config, as a session's
    send does."""
    return "local"


@pytest.fixture
def warm(monkeypatch):
    clock_reads_warm(monkeypatch, "claude-shim")


@pytest.fixture
def endpoints():
    made: list[FakeEndpoint] = []

    def make(**kw) -> FakeEndpoint:
        kw.setdefault("session_id", SESSION)
        ep = FakeEndpoint(**kw).open()
        made.append(ep)
        return ep

    yield make
    for ep in made:
        ep.go_away("the test ended")


@pytest.fixture
def bare_adapter(rig):
    """An adapter whose shim states `stated` from its bind, as `moxi comms claude` hands
    it the id the launch named (RULE D6; None for an unnamed resume). The cache read is
    the real one: no transcript is written for it."""
    made: list[ClaudeAdapter] = []

    def make(*, stated=SESSION, **kw) -> ClaudeAdapter:
        shim = rig.shim(name="reviewer", scope="proj-a", session_id=None, start=False)
        shim.session_id = stated
        a = ClaudeAdapter(shim, os.getpid(), look_interval=0.05, idle_poll=0.02,
                          idle_settle=0.05, **kw)
        a.start(tolerate_offline=False)
        made.append(a)
        return a

    yield make
    for a in made:
        a.stop()


@pytest.fixture
def adapter(bare_adapter, warm):
    """An adapter whose cache reads warm, for the tests of its delivery mechanics."""
    return bare_adapter


def send(rig, text, *flags):
    return rig.cli(["send", "--posture", "none", REVIEWER, text, *flags], **PLANNER)


def found(a, ep) -> bool:
    """The adapter has found `ep`, holds it and watches it."""
    return wait_until(lambda: a.session is not None and ep.holder is not None
                      and bool(ep.watchers))


def awaiting(a) -> tuple:
    """The ids of the digest the adapter handed over, once its hand-over has returned."""
    assert wait_until(lambda: bool(a.shim.awaiting_ids))
    return a.shim.awaiting_ids


def binding(rig, qualified=REVIEWER):
    with rig.daemon._lock:
        return rig.daemon.bindings.get(qualified)


# --------------------------------------------------------------------------- waiting

def test_before_its_endpoint_the_adapter_holds_mail_and_a_send_reports_waiting(
        rig, bare_adapter, endpoints, frames):
    """RULE D6 on the real cache read: a fresh launch states its named id from its bind,
    with no transcript and no turn, so cache reads `fresh` and the send reads `waiting`."""
    a = bare_adapter()
    assert wait_until(lambda: binding(rig) is not None)
    assert binding(rig).endpoint is False, "registered as having no endpoint"
    assert binding(rig).session_id == SESSION and binding(rig).turned is False, \
        "the named id, stated from the bind"
    assert rig.daemon._read(binding(rig)).kind == "fresh"
    code, out = send(rig, "before the endpoint")
    assert code == 0, out
    assert "is bound, but its session has had no turns endpoint" in out, out
    assert wait_until(lambda: len(a.shim.queue) == 1), "the adapter holds it"
    reply = rig.daemon._session_query({"identity": REVIEWER})
    assert reply["endpoint"] is False and reply["endpoint_for"] is not None

    ep = endpoints()
    assert found(a, ep), "found within its look interval"
    assert wait_until(lambda: len(ep.texts) == 1), "the held mail goes in at the look"
    assert "before the endpoint" in ep.texts[0]
    assert binding(rig).endpoint is True
    assert binding(rig).session_id == SESSION, "the look learned the session id"
    assert ep.holder is not None, "the adapter is the session's holder"


def test_waiting_is_data_on_the_library_result(rig, bare_adapter):
    from conftest import _stamp_of
    rig.shim(slot=1, name="planner", scope="proj-a")
    bare_adapter()
    assert wait_until(lambda: binding(rig) is not None)
    planner = _stamp_of({"MOXI_COMMS_PROJECT": "proj-a", "MOXI_COMMS_NAME": "planner",
                         "MOXI_COMMS_SCOPE": "proj-a", "MOXI_COMMS_SLOT": "1"})
    with rig.session_holding(planner) as sock:
        result = api.send(api.Sender.socket(sock), [REVIEWER], "data", posture="none")
    assert result.outcome == "sent", result.lines()
    (r,) = result.recipients
    assert r.delivery == "waiting", r
    assert "no turns endpoint" in r.reason


def test_a_launch_resuming_an_expired_session_reads_its_clock_before_the_endpoint(
        rig, bare_adapter):
    """RULE D6: a resume names its id, so cache reads that transcript's clock at the
    bind, and a send to the expired session is held cold, not refused `cache-unread`."""
    from conftest import write_transcript
    write_transcript(SESSION, age=3 * 3600)
    bare_adapter()
    assert wait_until(lambda: binding(rig) is not None)
    assert rig.daemon._read(binding(rig)).kind == "cold"
    code, out = send(rig, "into a cold cache")
    assert code == 0 and "cache went cold" in out, out
    assert "no turns endpoint" not in out, "a cold hold reads before `waiting`"


def test_an_unnamed_resume_states_no_id_and_its_send_fails_closed(rig, bare_adapter):
    """RULE D6's exception: until the look the binding has no id, so the hold refuses
    `cache-unread` (exit 6), as before the rule."""
    bare_adapter(stated=None)
    assert wait_until(lambda: binding(rig) is not None)
    assert binding(rig).session_id is None
    assert rig.daemon._read(binding(rig)).kind == "unknown"
    code, out = send(rig, "to an unnamed resume")
    assert code == 6 and "no session id reported yet" in out, out


def test_a_bound_name_with_its_endpoint_reads_live(rig, adapter, endpoints):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    assert wait_until(lambda: binding(rig).endpoint is True)
    code, out = send(rig, "live")
    assert code == 0 and "no turns endpoint" not in out, out


# --------------------------------------------------------------------------- delivery

def test_an_answered_turn_settles_the_digest_handled_and_moves_the_cursor(
        rig, adapter, endpoints, frames):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    code, out = send(rig, "one")
    assert code == 0, out
    assert wait_until(lambda: len(ep.texts) == 1)
    mid = awaiting(a)
    assert mid and ep.turn is not None and ep.turn["delivery"] == "d1"
    ep.end_turn("answer")
    assert wait_until(lambda: a.delivery is None and not a.shim.awaiting_ids)
    assert wait_until(lambda: any(mid[0] in f.get("handled", ())
                                  for f in frame_kinds(frames, "turn")))
    assert cursor_of(REVIEWER) == mid[0]


@pytest.mark.parametrize("outcome", ["aborted", "error", "refusal"])
def test_a_turn_that_did_not_answer_settles_the_digest_unhandled(
        rig, adapter, endpoints, frames, outcome):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    send(rig, "interrupted")
    assert wait_until(lambda: len(ep.texts) == 1)
    (mid,) = awaiting(a)
    ep.end_turn(outcome)
    assert wait_until(lambda: any(mid in f.get("unhandled", ())
                                  for f in frame_kinds(frames, "turn")))
    assert cursor_of(REVIEWER) is None, "the cursor does not pass an unhandled digest"
    assert a.session is not None, "the endpoint is kept"


def test_mail_that_arrives_mid_turn_waits_for_the_turn_end(rig, adapter, endpoints):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    ep.start_turn()                       # the person's turn
    assert wait_until(lambda: a.turn_open)
    send(rig, "mid turn")
    assert wait_until(lambda: len(a.shim.queue) == 1)
    time.sleep(0.3)
    assert ep.texts == [], "nothing is handed while a turn runs"
    ep.end_turn("answer")
    assert wait_until(lambda: len(ep.texts) == 1), "the turn end is the release point"


def test_one_digest_at_a_time(rig, adapter, endpoints):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    send(rig, "first")
    assert wait_until(lambda: len(ep.texts) == 1)
    send(rig, "second")
    send(rig, "third")
    assert wait_until(lambda: len(a.shim.queue) == 2)
    time.sleep(0.3)
    assert len(ep.texts) == 1, "the next digest waits for the first one's turn"
    ep.end_turn("answer")
    assert wait_until(lambda: len(ep.texts) == 2)
    assert "second" in ep.texts[1] and "third" in ep.texts[1], "one digest, coalesced"


@pytest.mark.parametrize("reason", ["dropped", "gone", "unsupported"])
def test_a_refused_delivery_is_unhandled_and_the_endpoint_stays(
        rig, adapter, endpoints, frames, reason):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    ep.refuse_next(reason)
    send(rig, "refused")
    assert wait_until(lambda: len(ep.texts) == 1)
    assert wait_until(lambda: any(f.get("unhandled") for f in frame_kinds(frames, "turn")))
    assert a.session is not None, "a refusal the endpoint made keeps the endpoint"


def test_an_untied_loss_settles_one_digest_and_keeps_the_endpoint(
        rig, adapter, endpoints, frames):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    ep.lose_next("untied: the submit resolved with no turn started in its window")
    send(rig, "untied")
    assert wait_until(lambda: len(ep.texts) == 1)
    assert wait_until(lambda: any(f.get("unhandled") for f in frame_kinds(frames, "turn")))
    assert a.session is not None and binding(rig).endpoint is True
    assert a.looks == 1


def test_a_loss_that_is_not_untied_means_the_endpoint_is_gone(
        rig, adapter, endpoints, frames):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    ep.lose_next("the endpoint ended")
    send(rig, "gone")
    assert wait_until(lambda: a.looks >= 2 or a.session is None)
    assert wait_until(lambda: any(f.get("unhandled") for f in frame_kinds(frames, "turn")))
    assert wait_until(lambda: any(f.get("endpoint") is False for f in frames)), \
        "the daemon was told"


def test_the_endpoint_going_away_settles_the_open_digest_and_the_adapter_looks_again(
        rig, adapter, endpoints, frames):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    send(rig, "in flight")
    assert wait_until(lambda: len(ep.texts) == 1)
    (mid,) = awaiting(a)
    ep.go_away("a plugin reload")
    assert wait_until(lambda: a.session is None)
    assert wait_until(lambda: binding(rig).endpoint is False)
    assert wait_until(lambda: any(mid in f.get("unhandled", ())
                                  for f in frame_kinds(frames, "turn")))
    again = endpoints()
    assert wait_until(lambda: a.session is not None and a.looks == 2)
    assert again.holder is not None, "the new endpoint is held"
    assert wait_until(lambda: binding(rig).endpoint is True)


def test_a_look_after_a_reload_releases_no_held_mail(rig, adapter, endpoints, frames):
    """Only the first look stands in for a `ready`. A plugin reload raises no
    SessionStart, so the re-look after it is no release point: `--held` mail waits for
    the session's own next turn, a clear, a compaction or a restart."""
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    assert wait_until(lambda: len(frame_kinds(frames, "look")) == 1)
    time.sleep(0.3)
    code, out = send(rig, "held one", "--held")
    assert code == 0, out
    time.sleep(0.5)
    assert ep.texts == [], "held while idle"
    ep.go_away("a plugin reload")
    assert wait_until(lambda: a.session is None)
    again = endpoints()
    assert wait_until(lambda: a.session is not None and a.looks == 2)
    time.sleep(1.0)
    assert again.texts == [], "the re-look released the held message"
    assert len(frame_kinds(frames, "look")) == 1, "one look frame, at the first look"
    code, out = send(rig, "not held")
    assert code == 0, out
    assert wait_until(lambda: len(again.texts) == 1), "unheld mail still goes in"
    assert "not held" in again.texts[0] and "held one" not in again.texts[0]


def test_a_stop_while_the_endpoint_is_gone_is_not_held(rig, adapter, endpoints):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    ep.go_away("crash")
    assert wait_until(lambda: a.session is None)
    assert a._hold is None


# --------------------------------------------------------------------------- holding

def test_the_holder_answers_each_stop_with_the_shims_reply_check(
        rig, adapter, endpoints, monkeypatch):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    assert ep.stop() is None, "no debt: the turn ends"
    seen = []

    def on_stop(fields):
        seen.append(fields)
        return owed.Decision(block="reply to planner first", names=("planner@proj-a",))

    monkeypatch.setattr(a.shim, "on_stop", on_stop)
    ep.start_turn()
    assert ep.stop({"stop_hook_active": False, "task_types": ["shell"], "crons": 1}) \
        == "reply to planner first"
    assert seen[-1] == {"stop_hook_active": False, "task_types": ["shell"], "crons": 1}
    assert a.shim.blocked, "a held turn flushes nothing until it ends"
    ep.end_turn("answer")
    assert wait_until(lambda: not a.shim.blocked), "the turn end clears the block"


def test_a_missing_stop_fact_lets_the_turn_end(rig, adapter, endpoints):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    assert ep.stop({"task_types": None, "crons": None, "stop_hook_active": None}) is None


# --------------------------------------------------------------------------- evidence

def test_turned_follows_the_turn_events(rig, adapter, endpoints):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    assert a.shim.session_id == SESSION
    assert a.shim.turned is False, "a fresh session with no turn"
    ep.start_turn()
    assert wait_until(lambda: a.shim.turned)
    ep.end_turn()
    ep.ready("clear", "5e55a1d0-0000-4000-8000-000000000002")
    assert wait_until(lambda: a.shim.session_id.endswith("02"))
    assert a.shim.turned is False, "a clear lowers turned for the new id"
    ep.ready("resume", "5e55a1d0-0000-4000-8000-000000000003")
    assert wait_until(lambda: a.shim.session_id.endswith("03"))
    assert a.shim.turned is True, "another new id may hold a context"
    assert wait_until(lambda: binding(rig).session_id.endswith("03"))


def test_the_stated_id_at_the_look_keeps_turned_and_a_different_one_sets_it(
        rig, bare_adapter, endpoints):
    """At the look `facts()`'s id replaces the stated one. The same id is not a new
    session; a different one (a `/clear` or `/resume` no endpoint saw) has no source, so
    it sets `turned` and an empty transcript reads `unknown` until a turn."""
    ep = endpoints()
    same = bare_adapter()
    assert found(same, ep)
    assert same.shim.session_id == SESSION and same.shim.turned is False
    assert wait_until(lambda: binding(rig).endpoint is True)
    assert binding(rig).turned is False
    same.stop()
    assert wait_until(lambda: binding(rig) is None)

    other = bare_adapter(stated="5e55a1d0-0000-4000-8000-0000000000aa")
    assert wait_until(lambda: binding(rig) is not None)
    assert binding(rig).session_id.endswith("aa") and binding(rig).turned is False
    assert found(other, ep)
    assert other.shim.session_id == SESSION and other.shim.turned is True
    assert wait_until(lambda: binding(rig).session_id == SESSION
                      and binding(rig).turned is True)
    assert rig.daemon._read(binding(rig)).kind == "unknown"


def test_an_epoch_above_zero_at_the_look_sets_turned(rig, adapter, endpoints):
    ep = endpoints(epoch=3)
    a = adapter()
    assert found(a, ep)
    assert a.shim.turned is True
    assert wait_until(lambda: binding(rig).turned is True)


def test_the_first_look_is_a_release_point_when_idle_and_no_evidence_of_a_turn(
        rig, adapter, endpoints, frames):
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    assert wait_until(lambda: frame_kinds(frames, "look")), "a look frame at the look"
    assert not frame_kinds(frames, "turn"), "a look is not a turn"
    assert binding(rig).turned is False, "a fresh session stays fresh"


def test_no_release_at_the_look_while_a_turn_runs(rig, adapter, endpoints, frames):
    ep = endpoints()
    ep.start_turn()
    a = adapter()
    assert found(a, ep)
    time.sleep(0.2)
    assert not frame_kinds(frames, "look")
    assert a.turn_open


def test_a_session_another_caller_holds_is_not_taken(rig, adapter, endpoints):
    ep = endpoints()
    other = moxi_turns.find(pid=os.getpid()).hold(lambda facts: None)
    try:
        a = adapter()
        time.sleep(0.3)
        assert a.session is None, "a refused hold is no endpoint; it looks again"
    finally:
        other.close()
    assert found(a, ep)
    assert ep.holds >= 2


# --------------------------------------------------------------------------- stopping

def test_stop_deregisters_and_removes_the_claim_file(rig, adapter, endpoints):
    from moxi_comms import claimfile
    ep = endpoints()
    a = adapter()
    assert found(a, ep)
    key = a.shim.claims.key
    a.stop()
    assert wait_until(lambda: binding(rig) is None), "the name deregisters"
    assert claimfile.read(key) is None
