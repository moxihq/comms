"""Cold hold, under the one queue (comms/refine §3).

Mail to an agent whose cache is cold is still accepted and kept for it. It arrives at the
agent's next turn, whether that turn comes on a woken cache, after a clear, or in a new
session after an exit.

[B1] comms@cold-hold held a cold claude-shim recipient's mail and refused every other
adapter's. comms/refine §3 replaced that: a cold recipient of ANY adapter is posted and
its message waits as a row in the one queue ([F1], [F5]); the row goes out at the name's
next release point (§3.5): a `turn` carrying `handled` on a warm read, a `ready` whose
source is `startup`, `clear` or `compact`, a `resume` on a warm read, and a register on a
passing read for unflagged rows. The row stays until the adapter names it handled
(§3.3.3). A read comms cannot make (no session id, an unreadable transcript, a name that
unbinds mid-check, a failed `session` query) refuses the post at the verb, exit 6, nothing
posted ([F7], §3.8.3). An `agent-N`'s rows end with its binding (§3.3.2).

Release points are driven by raw `activity` frames on the adapter's own connection
(`send_turn`, `send_ready`): the in-branch shim still sends `turn` with no `handled` and
`ready` with no `source`, which release nothing (§3.14).

Every rig test runs on both backends (BOTH_BACKENDS), never Discord (comms axiom,
2026-09-23). The cache clock reads fixture transcripts under the rig's tmp projects root.
"""

from __future__ import annotations

import time

import pytest
from conftest import (record_rows, send_ready, send_turn, wait_for_queue, wait_until,
                      write_transcript)

from moxi_comms import cache, cli, constants, cursors, machine_line, sending
from moxi_comms.client import DaemonClient, DaemonError
from moxi_comms.daemon import TIER_EPHEMERAL
from moxi_comms.paths import daemon_socket

# Shared fixtures, imported so pytest finds them here too (the autouse tmp-path guard
# among them).
from test_cold_every_sender import (adapters, bare_shim, cold_harness_report,  # noqa: F401
                                    only_tmp_paths)
from test_cold_sends import reboot  # noqa: F401

import shim_drive

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
REVIEWER_Q = "reviewer@proj-a"
MAIN_Q = "main@proj-a"
HELPER_Q = "helper@proj-a"
CODER_Q = "coder@proj-a"

SESS_A = "aaaaaaaa-1111-4111-8111-111111111111"
SESS_B = "bbbbbbbb-2222-4222-8222-222222222222"
SESS_C = "cccccccc-3333-4333-8333-333333333333"
SESS_D = "dddddddd-4444-4444-8444-444444444444"

HOUR = 3600

LAST_CHOSEN = ("Sent and held: reviewer@proj-a reads this after its own next turn, or when "
               "its session is cleared, compacted or restarted. Nothing wakes it for this "
               "message alone.")


def last_agent_n(q):
    return (f"Sent and held: {q} reads this after its own next turn in this session (when "
            f"the operator types into it, or its first turn after a /clear). {q} is a "
            "session-only name: if the session ends, its comms connection drops or the "
            "comms daemon restarts first, the message is dropped. Nothing wakes it for "
            "this message alone.")


# --------------------------------------------------------------------------- helpers

def posted(rig):
    return rig.gateway.history(rig.names_channel)


def held_of(rig, q=REVIEWER_Q):
    """The ids waiting in `q`'s queue (design §3.2: one queue)."""
    with rig.daemon._lock:
        return set(rig.daemon.queue.get(q, {}))


def queued_ids(shim_or_queue):
    queue = getattr(shim_or_queue, "queue", shim_or_queue)
    return [p["message_id"] for p in queue]


def sent_ids(out):
    return [line.split()[1] for line in out.splitlines() if line.startswith("sent ")]


def send(rig, to, text="hello", *flags, sender=PLANNER, env=None):
    return rig.cli(["send", "--posture", "none", to, text, *flags], **sender, env=env)


def cold_shim(rig, sid=SESS_A, age=2 * HOUR, **kw):
    """reviewer (or `kw`'s name) on session `sid`, its last request `age` seconds ago."""
    write_transcript(sid, age)
    return rig.shim(**{**REVIEWER, **kw}, session_id=sid)


def cold_send(rig, to=REVIEWER_Q, text="for when you are back", *flags, **kw):
    """A plain send that is held; returns (out, id) once the daemon has its row."""
    code, out = send(rig, to, text, *flags, **kw)
    assert code == 0, out
    (mid,) = sent_ids(out)
    q = to if "@" in to or to.startswith("agent-") else f"{to}@proj-a"
    wait_until(lambda: mid in held_of(rig, q))
    return out, mid


def hook(monkeypatch, s, op, sid, event="Stop", **extra):
    """One turn event of session `sid`, handed to the shim as comms' Claude adapter
    hands it turns' event."""
    shim_drive.drive(s, op, sid, **extra)


def stop_shim(rig, s, q=REVIEWER_Q):
    s.stop()
    wait_until(lambda: q not in rig.daemon.bindings)


def agents_view():
    c = DaemonClient(daemon_socket()).connect()
    try:
        return c.query("agents")
    finally:
        c.close()


def bare_turn(pushes, q, handled=()):
    """`activity kind=turn` with `handled` from a bare binding's own connection."""
    pushes.client.notify("activity", identity=q, kind="turn", turned=True,
                         handled=[str(i) for i in handled], unhandled=[])


# ================================================================ the held send

def test_a_plain_send_to_a_cold_shim_is_posted_and_held(rig):
    s = cold_shim(rig, age=2 * HOUR + 600)
    write_transcript(SESS_A, 2 * HOUR + 600)
    t_send = rig.clock()
    code, out = send(rig, REVIEWER_Q, "for when you are back")
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0].startswith("sent ") and lines[0].endswith("to reviewer@proj-a in #names")
    assert lines[1].startswith("held: reviewer@proj-a's cache went cold 1h10m ago (last "
                               "turn ")
    assert lines[1].endswith(")."), lines[1]       # the line ends after its cold sentence
    assert lines[2:] == [LAST_CHOSEN]
    (msg,) = posted(rig)
    ml = machine_line.parse(msg.content)
    assert not ml.held, "no [held] mark: the cold read, not the flag, makes the row"
    (cold_at,) = ml.cold_at
    assert isinstance(cold_at, int) and cold_at <= t_send, "a cold read's cold-at is past"
    wait_until(lambda: msg.id in held_of(rig))
    assert record_rows(rig.gateway, REVIEWER_Q) == {msg.id}
    time.sleep(0.1)
    assert s.queue == []
    view = agents_view()
    assert view["waiting"][REVIEWER_Q]["count"] == 1
    code, roster = rig.cli(["agents", "--seats"], **PLANNER)
    assert f"  {REVIEWER_Q:<24} 1 message(s), oldest waiting " in roster
    code, why = rig.cli(["explain", REVIEWER_Q, msg.id], **PLANNER)
    assert code == 0 and why.splitlines()[0].startswith(f"{REVIEWER_Q}   {cursors.WAITING}")
    assert f"1 in the queue for its next release point: {msg.id}" in " ".join(why.split())
    assert "ACCOUNT-LIMITS-SECRET" not in out


def test_wake_the_operators_turn_releases_it(rig):
    """Cold-held (§3.5): the turn that warms the cache sends `turn`; the read is warm;
    the row goes, marked held, and stays until the adapter names it handled."""
    s = cold_shim(rig)
    _out, mid = cold_send(rig)
    send_turn(s)                                 # still cold: nothing
    time.sleep(0.2)
    assert s.queue == [] and held_of(rig) == {mid}
    write_transcript(SESS_A, 3)                  # the operator typed: a warm request
    send_turn(s)
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == mid and payload["held"] is True
    assert held_of(rig) == {mid}
    send_turn(s, handled=[mid])
    wait_until(lambda: held_of(rig) == set())
    assert record_rows(rig.gateway, REVIEWER_Q) == set()


def test_the_shims_stop_on_a_warm_read_releases_the_row(rig, monkeypatch):
    """§3.5: the shim's Stop sends `turn` with `handled` (empty here, nothing was
    handed), and on a warm read that releases the row. A `turn` with no `handled` (a
    pre-round adapter, §3.14) releases nothing: `test_queue.py` tests that with raw
    frames."""
    s = cold_shim(rig)
    _out, mid = cold_send(rig)
    write_transcript(SESS_A, 3)
    hook(monkeypatch, s, "turn-boundary", SESS_A)
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == mid and payload["held"] is True


def test_clear_releases_it_with_no_read(rig, monkeypatch):
    """[F2]: a /clear is a release point that reads nothing. The shim's SessionStart
    sends `ready source=clear`, which releases the row though the cleared session has
    no transcript yet."""
    s = cold_shim(rig)
    _out, mid = cold_send(rig)
    hook(monkeypatch, s, "ready", SESS_B, "SessionStart", source="clear")
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == mid and payload["held"] is True
    assert rig.daemon.bindings[REVIEWER_Q].session_id == SESS_B


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_relaunch_the_new_sessions_register_releases_it(rig, reboot, restart):
    """A new session binds on a fresh read: the register is a release point for an
    unflagged row ([R7]); its `ready source=startup` does not hand it twice ([R58])."""
    s = cold_shim(rig)
    _out, mid = cold_send(rig)
    stop_shim(rig, s)
    assert held_of(rig) == {mid}, "a row survives the unbind for a chosen name"
    if restart:
        reboot(rig)
        assert held_of(rig) == {mid}, "the boot load brought it back from the record"
    write_transcript(SESS_B, None)               # a new session: no request yet, fresh
    again = rig.shim(**REVIEWER, session_id=SESS_B)
    (payload,) = wait_for_queue(again, 1)
    assert payload["message_id"] == mid and payload["held"] is True
    send_ready(again, "startup")
    time.sleep(0.2)
    assert queued_ids(again) == [mid], "a row handed to this binding is not handed again"
    assert held_of(rig) == {mid}


def test_relaunch_with_other_mail_waiting_the_register_hands_both_oldest_first(rig):
    s = cold_shim(rig)
    _out, first = cold_send(rig)
    stop_shim(rig, s)
    code, out = send(rig, REVIEWER_Q, "while you were away")
    assert code == 0 and "not bound right now" in out
    (second,) = sent_ids(out)
    wait_until(lambda: second in held_of(rig))
    write_transcript(SESS_B, None)               # fresh: the read passes
    again = rig.shim(**REVIEWER, session_id=SESS_B)
    got = wait_for_queue(again, 2)
    assert queued_ids(got) == [first, second]
    assert all(p["held"] is True for p in got)
    assert held_of(rig) == {first, second}, "rows stay until handled"


def test_a_resumed_cold_session_waits_for_its_first_warm_turn(rig):
    """[F10]: `--resume` on a cold transcript releases nothing at the register or at
    `ready source=resume`; the resumed session's first `turn` on a warm read does."""
    s = cold_shim(rig)
    _out, mid = cold_send(rig)
    stop_shim(rig, s)
    again = rig.shim(**REVIEWER, session_id=SESS_A)      # `--resume`: still cold
    wait_until(lambda: not again.replaying)
    send_ready(again, "resume")
    time.sleep(0.2)
    assert again.queue == [] and held_of(rig) == {mid}
    write_transcript(SESS_A, 4)
    send_turn(again)
    assert queued_ids(wait_for_queue(again, 1)) == [mid]


def test_a_resumed_warm_session_gets_it_at_its_resume(rig):
    """[F10]: a resume whose transcript reads warm releases every row at its
    `ready source=resume`, flagged ones included."""
    write_transcript(SESS_A, 2 * HOUR)
    first = rig.shim(**REVIEWER, session_id=SESS_A)
    code, out = send(rig, REVIEWER_Q, "later", "--held")
    assert code == 0, out
    (flagged,) = sent_ids(out)
    wait_until(lambda: flagged in held_of(rig))
    stop_shim(rig, first)
    write_transcript(SESS_A, 5)                  # warm again before the resume binds
    again = rig.shim(**REVIEWER, session_id=SESS_A)
    wait_until(lambda: not again.replaying)
    time.sleep(0.2)
    assert again.queue == [], "a register holds a flagged row"
    send_ready(again, "resume")
    assert queued_ids(wait_for_queue(again, 1)) == [flagged]


def test_an_unreadable_shim_read_refuses_the_post(rig):
    """[F7]: a read comms cannot make is no longer held; the post is refused, exit 6."""
    write_transcript(SESS_A, lines=["this will not parse", "{ nor this"])
    s = rig.shim(**REVIEWER, session_id=SESS_A)
    code, out = send(rig, REVIEWER_Q)
    assert_refused(rig, code, out, f"comms could not read {REVIEWER_Q}'s cache (")
    assert s.queue == [] and held_of(rig) == set()


def test_a_warm_and_a_cold_recipient_the_warm_one_gets_it_now(rig):
    """Per recipient: one post; the warm one has it now, the cold one waits."""
    cold = cold_shim(rig)
    warm = rig.shim(slot=4, name="main", scope="proj-a")          # a warm transcript
    code, out = send(rig, "reviewer@proj-a,main@proj-a")
    assert code == 0, out
    (mid,) = sent_ids(out)
    assert [x for x in out.splitlines() if x.startswith("held:")] == [
        x for x in out.splitlines() if x.startswith("held: reviewer@proj-a's")]
    assert out.splitlines()[-1] == LAST_CHOSEN
    (payload,) = wait_for_queue(warm, 1)
    assert payload["message_id"] == mid and "held" not in payload, "live, not released"
    wait_until(lambda: mid in held_of(rig))
    assert cold.queue == [] and held_of(rig, MAIN_Q) == set()


def test_reply_to_a_cold_asker_is_held_and_pays_in_the_record(rig):
    cold_shim(rig)
    ask = rig.post_from_slot(6, ["planner@proj-a"], "q?", display=REVIEWER_Q)
    code, out = rig.cli(["reply", ask.id, "a."], **PLANNER)
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0].startswith("replying to reviewer@proj-a")
    assert lines[1].startswith("sent ") and lines[2].startswith("held: reviewer@proj-a's")
    assert lines[-1] == LAST_CHOSEN
    reply = posted(rig)[-1]
    assert machine_line.parse(reply.content).reply_to == ask.id
    wait_until(lambda: reply.id in held_of(rig))


def test_the_operators_lane_is_held_and_ends_on_the_last_line(rig):
    if rig.backend != "local":
        pytest.skip("the credentialled operator lane is local's")
    cold_shim(rig)
    code, out = rig.cli(["send", "--posture", "none", REVIEWER_Q, "from the operator"],
                        env={"MOXI_COMMS_OPERATOR": rig.prov.operator_user_id})
    assert code == 0, out
    assert out.splitlines()[-1] == (LAST_CHOSEN + " To reach it now, type into its "
                                    "session yourself.")
    (msg,) = posted(rig)
    wait_until(lambda: msg.id in held_of(rig))


def test_the_verb_reads_cold_and_the_cache_warms_before_routing(rig, monkeypatch):
    """§3.6: a known read at the daemon decides alone. The sender was told held, its
    cold-at is past, and the gate, reading warm, delivers live."""
    s = cold_shim(rig)
    real = sending.post

    def warmed(*a, **kw):
        write_transcript(SESS_A, 5)               # its own turn lands in between
        return real(*a, **kw)

    monkeypatch.setattr(sending, "post", warmed)
    code, out = send(rig, REVIEWER_Q, "just in time")
    assert code == 0 and out.splitlines()[-1] == LAST_CHOSEN
    (mid,) = sent_ids(out)
    assert queued_ids(wait_for_queue(s, 1)) == [mid]
    assert held_of(rig) == set()


def test_a_shim_that_reports_no_session_id_refuses_the_post(rig):
    """§3.8.2/§3.8.3: the silent-shim clause is gone. A binding that has reported no
    session id past the grace, as a shim built before cold-sends does, is a read comms
    cannot make: the post is refused."""
    rig.shim(**REVIEWER, session_id=None)
    rig.clock.advance(constants.SESSION_ID_GRACE + 1)
    code, out = send(rig, REVIEWER_Q)
    assert_refused(rig, code, out, f"comms could not read {REVIEWER_Q}'s cache (no session "
                                   "id reported yet)")
    assert "reported no turns" not in out


# ===================================================================== agent-N

def agent_n(rig, sid=SESS_C, age=2 * HOUR, **kw):
    write_transcript(sid, age)
    s = rig.shim(slot=5, session_id=sid, **kw)
    assert rig.daemon.bindings[s.identity].tier == TIER_EPHEMERAL
    return s


def test_an_agent_n_is_held_with_the_session_only_line_and_its_warm_turn_releases(rig):
    s = agent_n(rig)
    q = s.identity
    out, mid = cold_send(rig, q)
    assert out.splitlines()[-1] == last_agent_n(q)
    write_transcript(SESS_C, 3)
    send_turn(s)
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == mid and payload["held"] is True


def test_an_agent_n_keeps_its_name_across_a_clear_and_the_clear_releases(
        rig, monkeypatch):
    s = agent_n(rig)
    q = s.identity
    _out, mid = cold_send(rig, q)
    hook(monkeypatch, s, "ready", SESS_D, "SessionStart", source="clear")
    assert queued_ids(wait_for_queue(s, 1)) == [mid]
    assert rig.daemon.bindings[q].session_id == SESS_D
    assert s.identity == q


def test_an_ended_agent_n_drops_its_rows_from_memory_and_the_record(rig):
    """§3.3.2: an `agent-N` loses its rows at unbind, in memory and in the record."""
    s = agent_n(rig)
    q = s.identity
    _out, mid = cold_send(rig, q)
    wait_until(lambda: record_rows(rig.gateway, q) == {mid})
    stop_shim(rig, s, q)
    assert held_of(rig, q) == set()
    wait_until(lambda: record_rows(rig.gateway, q) == set())


def test_boot_drops_an_agent_ns_rows_and_the_same_number_gets_nothing(rig, reboot):
    """§3.3.2: rows in the record that still name an `agent-N` are dropped at boot, and
    an unrelated session numbered the same gets nothing at its first warm turn. A
    chosen name's rows in the same record load."""
    s = agent_n(rig)
    q = s.identity
    _out, mid = cold_send(rig, q)
    keeper = cold_shim(rig, SESS_A)
    _out, kept = cold_send(rig)
    stop_shim(rig, s, q)
    stop_shim(rig, keeper)
    rig.daemon.stop()
    rig.gateway.queue_add(q, [mid])              # as a daemon that died mid-unbind left it
    assert record_rows(rig.gateway, q) == {mid}
    reboot(rig)
    assert held_of(rig, q) == set()
    assert record_rows(rig.gateway, q) == set()
    assert held_of(rig) == {kept}, "a chosen name's rows are untouched"
    later = rig.shim(slot=5, session_id=SESS_B)          # unrelated, numbered the same
    assert later.identity == q
    write_transcript(SESS_B, 2)
    send_turn(later)
    time.sleep(0.3)
    assert later.queue == []


def test_the_queue_takes_no_row_for_an_unbound_agent_n(rig):
    """A reader that races the unbind puts nothing back for an `agent-N`; a chosen name
    with no binding gets its row, as before."""
    msg = rig.post_from_slot(6, ["nobody"], "x", display="planner@proj-a")
    with rig.daemon._lock:
        rig.daemon._queue_add("agent-41", msg.id, None)
        rig.daemon._queue_add(REVIEWER_Q, msg.id, None)
    assert held_of(rig, "agent-41") == set()
    assert held_of(rig) == {msg.id}


def test_the_last_line_names_each_tier_present(rig):
    """A chosen and an `agent-N` recipient, one clause each."""
    cold_shim(rig)
    s = agent_n(rig)
    q = s.identity
    code, out = send(rig, f"{REVIEWER_Q},{q}")
    assert code == 0, out
    assert out.splitlines()[-1] == (
        "Sent and held: reviewer@proj-a reads this after its own next turn, or when its "
        "session is cleared, compacted or restarted. "
        f"{q} reads this after its own next turn in this session (when the operator types "
        f"into it, or its first turn after a /clear). {q} is a session-only name: if the "
        "session ends, its comms connection drops or the comms daemon restarts first, the "
        "message is dropped. Nothing wakes them for this message alone.")


def test_a_held_send_to_an_agent_n_says_it_is_session_only(rig):
    """§3.8.2: no "with the next other message to it" ([F3])."""
    s = rig.shim(slot=5)
    q = s.identity
    code, out = send(rig, q, "later", "--held")
    assert code == 0, out
    assert out.splitlines()[1] == (
        f"NOTE: {q} reads this after its own next turn in this session (when the operator "
        "types into it, or its first turn after a /clear). Nothing wakes it for this "
        f"message alone. {q} is a session-only name: if the session ends, its comms "
        "connection drops or the comms daemon restarts first, the message is dropped.")
    assert "next other message" not in out
    assert "waits with no end" not in out


# ============================================== every adapter is held; refusals

def assert_refused(rig, code, out, reason):
    lines = out.splitlines()
    assert code == cli.EXIT_COLD == 6, out
    assert lines[0].startswith("Nothing was sent: ") and reason in lines[0], lines[0]
    assert lines[-1] == "Nothing was sent.", out
    assert posted(rig) == []
    assert "--held" not in out, "no re-run line"


def test_a_cold_shim_beside_a_cold_bare_binding_both_are_held(rig, adapters):
    """[F5], [F8]: a bare binding takes held mail like any adapter; its warm `turn`
    releases."""
    cold_shim(rig)
    helper = bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    code, out = send(rig, "reviewer@proj-a,helper@proj-a")
    assert code == 0, out
    lines = out.splitlines()
    assert [x.split("'")[0] for x in lines if x.startswith("held:")] == [
        f"held: {REVIEWER_Q}", f"held: {HELPER_Q}"]
    assert lines[-1] == ("Sent and held: reviewer@proj-a and helper@proj-a read this after "
                         "their own next turns, or when their sessions are cleared, "
                         "compacted or restarted. Nothing wakes them for this message "
                         "alone.")
    (mid,) = sent_ids(out)
    wait_until(lambda: mid in held_of(rig) and mid in held_of(rig, HELPER_Q))
    assert helper.ids() == []
    write_transcript(SESS_D, 3)                  # its session ran: warm
    bare_turn(helper, HELPER_Q)
    wait_until(lambda: helper.ids() == [mid])
    assert helper.delivered()[0]["held"] is True


def test_a_cold_shim_beside_a_cold_codex_agent_both_are_held(rig, adapters, tmp_path):
    cold_shim(rig)
    coder = adapters(slot=5, name="coder", scope="proj-a", adapter="codex",
                     report=cold_harness_report("codex", tmp_path))
    code, out = send(rig, "reviewer@proj-a,coder@proj-a")
    assert code == 0, out
    assert out.splitlines()[-1] == (
        "Sent and held: reviewer@proj-a and coder@proj-a read this after their own next "
        "turns, or when their sessions are cleared, compacted or restarted. Nothing wakes "
        "them for this message alone.")
    (mid,) = sent_ids(out)
    wait_until(lambda: mid in held_of(rig) and mid in held_of(rig, CODER_Q))
    time.sleep(0.1)
    assert coder.ids() == []


def test_an_unreadable_codex_agent_refuses_the_post(rig, adapters):
    adapters(slot=5, name="coder", scope="proj-a", adapter="codex")   # no session id
    code, out = send(rig, CODER_Q)
    assert_refused(rig, code, out, f"comms could not read {CODER_Q}'s cache (no thread id "
                                   "reported yet)")
    assert "To reach the other recipients" not in out, "there are none"


def test_an_unreadable_codex_agent_refuses_a_held_send_too(rig, adapters):
    """§3.8.1: `--held` reads the caches like any send; an unknown read refuses it."""
    adapters(slot=5, name="coder", scope="proj-a", adapter="codex")   # no session id
    code, out = send(rig, "reviewer@proj-a,coder@proj-a", "later", "--held", "--new-name")
    assert_refused(rig, code, out, f"comms could not read {CODER_Q}'s cache")
    assert held_of(rig, CODER_Q) == set()


def test_a_session_answer_with_no_verdict_refuses(rig, monkeypatch):
    """§3.8.3: no verdict is refused like an unknown one (the old-daemon reason is the
    version check now, §3.10)."""
    cold_shim(rig)
    real = rig.daemon._session_query

    def bare(frame):
        answer = real(frame)
        for key in ("cache_kind", "age_seconds", "cache_reason"):
            answer.pop(key)
        return answer

    monkeypatch.setattr(rig.daemon, "_session_query", bare)
    code, out = send(rig, REVIEWER_Q)
    assert_refused(rig, code, out, f"comms could not read {REVIEWER_Q}'s cache")
    assert "older than this comms" not in out and "Only the operator" not in out


def test_a_failed_session_query_refuses(rig, monkeypatch):
    cold_shim(rig)
    real = DaemonClient.query

    def broken(self, kind, **kw):
        if kind == "session":
            raise DaemonError("the daemon fell over")
        return real(self, kind, **kw)

    monkeypatch.setattr(DaemonClient, "query", broken)
    code, out = send(rig, REVIEWER_Q)
    assert_refused(rig, code, out, f"comms could not ask the daemon about {REVIEWER_Q} "
                                   "(the session query failed (the daemon fell over))")


def test_an_adapter_comms_has_no_clock_for_is_delivered_live(rig, monkeypatch):
    """§3.6, [F6]: an adapter with no clock reads `no-cache`; the send writes `-` for it
    and the mail goes live, where the verb used to refuse it."""
    s = cold_shim(rig)
    # The daemon's registry with no clock for claude-shim: its read is the no-clock one.
    from moxi_comms import kinds
    monkeypatch.setattr(rig.daemon, "kinds", kinds.Registry(tuple(
        k for k in rig.daemon.kinds.kinds if k.name != "claude-shim")))
    code, out = send(rig, REVIEWER_Q, "no clock to count by")
    assert code == 0, out
    assert not any(x.startswith("held:") for x in out.splitlines())
    (mid,) = sent_ids(out)
    assert machine_line.parse(posted(rig)[0].content).cold_at == (machine_line.COLD_AT_LIVE,)
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == mid and "held" not in payload
    assert held_of(rig) == set()


def test_a_name_that_unbinds_mid_check_refuses(rig, monkeypatch):
    cold_shim(rig)
    real = rig.daemon._session_query
    monkeypatch.setattr(rig.daemon, "_session_query",
                        lambda frame: {**real(frame), "bound": False})
    code, out = send(rig, REVIEWER_Q)
    assert_refused(rig, code, out, f"{REVIEWER_Q} unbound while comms was checking it")


# ============================================================ NOTEs, last lines


def test_a_held_send_prints_no_note_and_the_outcome_line_is_last(rig, adapters):
    """[R62]: no sender is exempt and nothing says so; the held send's outcome line
    stays last ([R4]). (`[policy] never_held` is a retired key that now fails the load,
    `test_cold_sends.py`.)"""
    bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    code, out = send(rig, HELPER_Q, "anyway")
    assert code == 0, out
    assert "NOTE" not in out and "never_held" not in out
    assert out.splitlines()[-1] == (
        "Sent and held: helper@proj-a reads this after its own next turn, or when its "
        "session is cleared, compacted or restarted. Nothing wakes it for this message "
        "alone.")


def test_a_refusal_keeps_its_own_line_last(rig, adapters):
    """records m4, under [R62]: a refusal (here `--held` to a codex agent with no session
    id) prints no NOTE, so "Nothing was sent." stays last."""
    adapters(slot=5, name="coder", scope="proj-a", adapter="codex")
    code, out = send(rig, CODER_Q, "later", "--held")
    assert_refused(rig, code, out, f"comms could not read {CODER_Q}'s cache")
    assert "NOTE" not in out
