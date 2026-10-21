"""Cold sends: the session id, the cache read, the hold,
`moxi comms cache`, and `--next-turn`.

Every rig test here runs on both backends (BOTH_BACKENDS). No test targets Discord. The
cache clock is comms' own read of a session's transcript (cold-sends v0.6, RULE 1): the
transcripts live under the rig's tmp projects root, written by `write_transcript`.

v0.6 changed these tests where THE RULE changed what they assert ([R23]): no sender is
exempt, a small context and a failed read are held, and a young binding with no session id
is held. The v0.6 cases themselves are in `test_cold_every_sender.py`. cold-hold ([B1],
2026-10-10) changed them again: a held claude-shim recipient is posted and held, exit 0,
ending on the `Sent and held:` line; and only a
recipient the daemon will not hold refuses the post, exit 6. Its own cases are in
`test_cold_hold.py`. comms/refine §6.4 removed `--cold` (argparse's exit 2 now).

comms/refine §3 (the one queue) changed them again: a cold recipient of any adapter is
posted and held; a read comms cannot make refuses the post, exit 6, `--held` included;
`--next-turn` is `--held`, and held mail is a row in the queue, released only at a
release point (a `turn` carrying `handled`, a `ready` with a source, a register for
unflagged rows) and deleted only when the adapter names it handled.

Sections follow: (a) the session id, (b) a forged activity, (c) `cache_state`, (d) the
hold, (e) `moxi comms cache`, (f) `--held`.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import socket
import tempfile
import threading
import time

import pytest
from conftest import (projects_root, send_ready, send_turn, wait_for_queue, wait_until,
                      write_config, write_transcript)

from moxi_comms import cache, cli, constants, cursors, machine_line
from moxi_comms.adapters.claude_shim import frame_line
from moxi_comms.claimhold import ClaimServer
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import Daemon, Refusal
from moxi_comms.paths import daemon_socket
from moxi_comms.protocol import LineConn

import shim_drive

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
REVIEWER_Q = "reviewer@proj-a"

SESS_REVIEWER = "11111111-1111-4111-8111-111111111111"
SESS_THINKER = "22222222-2222-4222-8222-222222222222"
SESS_OTHER = "33333333-3333-4333-8333-333333333333"
SESS_BLANK = "44444444-4444-4444-8444-444444444444"
SESS_OPERATOR = "55555555-5555-4555-8555-555555555555"

HOUR = 3600


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def bind(rig):
    """Bindings held by bare DaemonClients: thinker, the operator, a codex name, a
    console. This process holds each the way a module's own process does: its own
    connection, reported to a `ClaimServer` the rig sends through. Closed at the end of
    the test."""
    made, servers = [], []

    def _bind(**kwargs):
        c = DaemonClient(daemon_socket()).connect()
        answer = c.register(**kwargs)
        made.append(c)
        ident = answer.get("identity") or {}
        scope = "global" if kwargs.get("global_") else kwargs.get("scope") or "global"
        server = ClaimServer(kwargs["name"], scope,
                             kind=kwargs.get("adapter", "claude-shim"),
                             slot=kwargs.get("slot", "next-free")).start()
        server.set_bound(answer.get("slot"), ident.get("tier"))
        servers.append(server)
        rig.holders[server.claim["qualified"]] = server
        return c

    yield _bind
    for server in servers:
        server.stop()
    for c in made:
        c.close()


def bind_thinker(bind, session_id=SESS_THINKER):
    """thinker, a global name held by a bare claude-shim binding on `session_id`, which
    reads its transcript; `""` is a session that has made no request yet (`fresh`)."""
    return bind(slot=3, name="thinker", global_=True, adapter="claude-shim",
                session_id=session_id or SESS_BLANK, projects_root=str(projects_root()),
                turned=bool(session_id))


def bind_operator(bind, slot=4):
    """The operator, held by a bare claude-shim binding of its own on a session that has
    made no request yet (`fresh`)."""
    return bind(slot=slot, name="operator", global_=True, adapter="claude-shim",
                session_id=SESS_OPERATOR, projects_root=str(projects_root()), turned=False)


def reviewer_with_session(rig, session_id=SESS_REVIEWER, **kwargs):
    """reviewer, on `session_id`. With no transcript written for it, it is a session
    that has not been prompted yet: `fresh` on its own shim's word (§4 item 3)."""
    return rig.shim(**REVIEWER, session_id=session_id, **kwargs)


def posted(rig):
    return rig.gateway.history(rig.names_channel)


def sent_id(out):
    return re.search(r"^sent (\d+) ", out, re.M).group(1)


def send(rig, to, text="hello", *flags, sender=PLANNER, env=None):
    return rig.cli(["send", "--posture", "none", to, text, *flags], **sender, env=env)


def held_of(rig, qualified=REVIEWER_Q):
    """The ids waiting in `qualified`'s queue (design §3.2: one queue)."""
    with rig.daemon._lock:
        return set(rig.daemon.queue.get(qualified, {}))


def queued_ids(shim_or_queue):
    queue = getattr(shim_or_queue, "queue", shim_or_queue)
    return [p["message_id"] for p in queue]


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


def fast_backoff():
    return itertools.repeat(0.05)


def post_many(rig, recipients, n, *, slot=6, display="planner@proj-a", next_turn=False):
    """`n` messages through one credential, resolved once."""
    from moxi_comms import directory
    from moxi_comms.mock_gateway import NO_MENTIONS
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(slot, display, "global"))
    finally:
        db.close()
    return [rig.gateway.execute_webhook(cred, machine_line.compose(recipients, f"m{i}",
                                                                   next_turn=next_turn),
                                        username=display, allowed_mentions=NO_MENTIONS)
            for i in range(n)]


def next_turn(rig, to=REVIEWER_Q, text="when you are next about"):
    """A `--held` send through the verb; returns its id once it waits in the queue.
    `--new-name`: some callers send before the name has ever bound (§7.6); on a name
    bound now or before it changes nothing."""
    code, out = send(rig, to, text, "--held", "--new-name")
    assert code == 0, out
    mid = sent_id(out)
    wait_until(lambda: mid in held_of(rig, to))
    return mid


# ============================================================ (a) the session id

def test_a_session_start_carries_the_id_to_the_shim_and_the_daemon(rig):
    reviewer = rig.shim(**REVIEWER, session_id=None)
    assert reviewer.session_id is None and rig.daemon.bindings[REVIEWER_Q].session_id is None
    shim_drive.ready(reviewer, "startup", SESS_REVIEWER)
    assert reviewer.session_id == SESS_REVIEWER
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].session_id == SESS_REVIEWER)
    assert rig.daemon.bindings[REVIEWER_Q].session_seen_at is not None


def test_a_clear_changes_the_id_everywhere(rig):
    """`SessionStart` fires on `/clear` with the NEW id; the path follows it."""
    reviewer = reviewer_with_session(rig)
    assert rig.daemon.bindings[REVIEWER_Q].session_id == SESS_REVIEWER, "from register"
    shim_drive.ready(reviewer, "clear", SESS_OTHER)
    assert reviewer.session_id == SESS_OTHER
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].session_id == SESS_OTHER)
    # and a turn end in the same session keeps it there
    shim_drive.drive(reviewer, shim_drive.TURN_BOUNDARY, SESS_OTHER)
    assert rig.daemon.bindings[REVIEWER_Q].session_id == SESS_OTHER


def test_the_shim_starts_from_its_spawn_environment(rig, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESS_OTHER)
    reviewer = rig.shim(**REVIEWER, session_id=None)
    assert reviewer.session_id == SESS_OTHER
    assert rig.daemon.bindings[REVIEWER_Q].session_id == SESS_OTHER


def test_the_session_query_answers_bound_and_unbound(rig):
    reviewer_with_session(rig)
    with _client() as c:
        bound = c.query("session", identity=REVIEWER_Q)
        unbound = c.query("session", identity="nobody@proj-a")
    assert bound["bound"] is True and bound["adapter"] == "claude-shim"
    assert bound["session_id"] == SESS_REVIEWER and bound["bound_at"] is not None
    assert bound["bound_for"] >= 0
    assert bound["cache_kind"] == "fresh", "never prompted, on its own shim's word"
    assert unbound["bound"] is False and unbound["session_id"] is None
    assert unbound["cache_kind"] is None


class _client:
    def __enter__(self):
        self.c = DaemonClient(daemon_socket()).connect()
        return self.c

    def __exit__(self, *exc):
        self.c.close()


# ===================================================== (b) a forged activity

def test_a_forged_activity_from_another_connection_changes_nothing(rig):
    reviewer = reviewer_with_session(rig)
    held = next_turn(rig)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(daemon_socket()))
    conn = LineConn(s)
    try:
        conn.send({"op": "activity", "identity": REVIEWER_Q, "session_id": SESS_OTHER,
                   "kind": "turn", "handled": [held], "unhandled": [], "seq": 1})
        conn.send({"id": 1, "op": "ping"})
        assert conn.recv()["ok"] is True
    finally:
        conn.close()
    assert rig.daemon.bindings[REVIEWER_Q].session_id == SESS_REVIEWER
    assert held_of(rig) == {held} and reviewer.queue == [], "a forged turn releases nothing"


# ========================================================= (c) cache_state

def test_warm_cold_and_a_raw_id_with_no_transcript():
    write_transcript(SESS_REVIEWER, 600)
    write_transcript(SESS_OTHER, 2 * HOUR)
    warm, cold, none = cache.cache_states([cache.target_for_session_id(i) for i in
                                           (SESS_REVIEWER, SESS_OTHER, SESS_THINKER)])
    assert warm.kind == "warm"
    assert 595 <= warm.age_seconds <= 660
    assert cold.kind == "cold"
    # v0.6: no transcript, and a raw id carries no evidence of no request (§4)
    assert none.kind == "unknown" and "no transcript for session" in none.reason


def test_a_transcript_with_no_assistant_line_yet_is_fresh():
    write_transcript(SESS_OTHER, None)
    assert cache.cache_state(SESS_OTHER).kind == "fresh"


def test_a_raw_id_needs_no_daemon():
    write_transcript(SESS_OTHER, 120)
    state = cache.cache_state(SESS_OTHER)
    assert state.kind == "warm" and state.session_id == SESS_OTHER


def test_a_subagents_newer_work_does_not_warm_the_main_clock(tmp_path, monkeypatch):
    """The clock is the main conversation's last assistant line. A sidechain line in
    the main file, and a subagent's own file, are newer and must not move it. Read by
    comms' own reader (v0.6), against a transcript under a temporary Claude home."""
    def line(ts, *, sidechain=False, req="r1"):
        return json.dumps({
            "type": "assistant", "isSidechain": sidechain, "sessionId": SESS_OTHER,
            "requestId": req, "uuid": req, "timestamp": ts,
            "message": {"model": "claude-opus-5-5",
                        "usage": {"input_tokens": 1000, "cache_read_input_tokens": 30000,
                                  "cache_creation_input_tokens": 0, "output_tokens": 10}}})

    def iso(age):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - age))

    project = tmp_path / "projects" / "-tmp-x"
    project.mkdir(parents=True)
    (project / f"{SESS_OTHER}.jsonl").write_text(
        line(iso(2 * HOUR)) + "\n" + line(iso(30), sidechain=True, req="r2") + "\n")
    sub = project / SESS_OTHER / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(line(iso(10), sidechain=True, req="r3") + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    state = cache.cache_state(SESS_OTHER)
    assert state.kind == "cold", state
    assert state.age_seconds > HOUR


# ============================================================== (d) the hold

#: A held send's last line for reviewer, a chosen name (design §3.8.2).
HELD_LAST = ("Sent and held: reviewer@proj-a reads this after its own next turn, or when "
             "its session is cleared, compacted or restarted. Nothing wakes it for this "
             "message alone.")


def held_line(out):
    """The first `held:` line of a send's output (the line after the `sent` line)."""
    return next(x for x in out.splitlines() if x.startswith("held: "))


def test_a_send_to_a_cold_recipient_is_posted_and_held(rig):
    reviewer = reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 2 * HOUR + 600)
    code, out = send(rig, REVIEWER_Q)
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == f"sent {posted(rig)[0].id} to reviewer@proj-a in #names"
    assert re.fullmatch(r"held: reviewer@proj-a's cache went cold 1h10m ago \(last turn "
                        r"[A-Za-z0-9: ]+\)\.", lines[1]), lines[1]
    assert lines[2:] == [HELD_LAST]
    (msg,) = posted(rig)
    wait_until(lambda: msg.id in held_of(rig))
    assert reviewer.queue == []


def test_a_reply_is_held(rig):
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 2 * HOUR)
    ask = rig.post_from_slot(6, ["planner@proj-a"], "q?", display="reviewer@proj-a")
    code, out = rig.cli(["reply", ask.id, "a."], **PLANNER)
    assert code == 0 and "held: reviewer@proj-a's cache went cold" in out, out
    assert out.splitlines()[-1] == HELD_LAST, out


def test_a_small_context_is_held_like_any_other(rig):
    """v0.6: card W3's small-context exception is superseded ([C14]). The hold line ends
    after its cold sentence."""
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, HOUR + 12 * 60)
    code, out = send(rig, REVIEWER_Q)
    assert code == 0 and len(posted(rig)) == 1
    assert re.fullmatch(r"held: reviewer@proj-a's cache went cold 12m ago \(last turn "
                        r"[A-Za-z0-9: ]+\)\.", held_line(out)), out


def test_warm_and_fresh_go_through_silently(rig):
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, HOUR - 60)
    code, out = send(rig, REVIEWER_Q)
    assert code == 0 and "NOTE" not in out
    write_transcript(SESS_REVIEWER, None)
    code, out = send(rig, REVIEWER_Q)
    assert code == 0 and "NOTE" not in out


def test_a_failed_read_refuses_the_post(rig):
    """A clock comms cannot read refuses the send, nothing posted, exit 6 (design
    §3.8.3, [F7]): the sender sends it again in a moment."""
    reviewer_with_session(rig)
    path = write_transcript(SESS_REVIEWER, 60)
    path.chmod(0)
    try:
        code, out = send(rig, REVIEWER_Q)
    finally:
        path.chmod(0o600)
    assert code == cli.EXIT_COLD and posted(rig) == [], out
    lines = out.splitlines()
    assert lines[0].startswith("Nothing was sent: comms could not read reviewer@proj-a's "
                               "cache (the transcript could not be read")
    assert lines[1:] == ["Send it again in a moment; a read that succeeds holds or "
                         "delivers it.", "Nothing was sent."]
    assert "held:" not in out


def test_no_session_id_refuses_the_post_young_or_old(rig):
    """A binding with no session id reported reads unknown: the send is refused, young
    or old (design §3.8.3)."""
    rig.shim(**REVIEWER, session_id=None)
    for _ in range(2):
        code, out = send(rig, REVIEWER_Q)
        assert code == cli.EXIT_COLD and posted(rig) == [], out
        assert out.splitlines()[0] == ("Nothing was sent: comms could not read "
                                       "reviewer@proj-a's cache (no session id reported "
                                       "yet).")
        rig.clock.advance(constants.SESSION_ID_GRACE + 1)


def test_a_failed_session_query_refuses_the_post(rig, monkeypatch):
    """A `session` query that fails refuses the post, exit 6 (design §3.8.3); the old
    daemon's own reason is gone with the version check (§3.10)."""
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 5 * HOUR)

    def refuse(frame):
        raise Refusal("unknown query kind 'session'")

    monkeypatch.setattr(rig.daemon, "_session_query", refuse)
    code, out = send(rig, REVIEWER_Q)
    assert code == cli.EXIT_COLD and posted(rig) == []
    assert out.splitlines()[0] == ("Nothing was sent: comms could not ask the daemon about "
                                   "reviewer@proj-a (the session query failed (unknown "
                                   "query kind 'session')).")
    assert out.splitlines()[-1] == "Nothing was sent."


def test_no_rerun_line_beside_thinker_the_operator_or_codex(rig, bind):
    """No re-run line on any path. Beside a passing thinker or the operator the cold
    reviewer is held and the post goes; beside a codex agent comms cannot read, the
    whole post is refused (design §3.8.3)."""
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 2 * HOUR)
    bind_thinker(bind, session_id="")                    # blank anchor: fresh
    bind_operator(bind)
    bind(slot=5, name="coder", scope="proj-a", adapter="codex")
    for other in ("thinker", "operator"):
        code, out = send(rig, f"reviewer@proj-a,{other}")
        assert code == 0, (other, out)
        assert "--held" not in out, other
        assert out.splitlines()[-1] == HELD_LAST, other
    code, out = send(rig, "reviewer@proj-a,coder@proj-a")
    assert code == cli.EXIT_COLD and "--held" not in out
    lines = out.splitlines()
    assert lines[0].startswith("Nothing was sent: comms could not read coder@proj-a's "
                               "cache (")
    assert lines[1] == "To reach the other recipients now, send it again without coder@proj-a."
    assert lines[-1] == "Nothing was sent."
    assert len(posted(rig)) == 2


def test_no_sender_is_exempt(rig, bind):
    """v0.6 ([C14], [C15]): card W2 (a)'s never-held senders are superseded. thinker,
    the console and the operator's lane are held like any agent."""
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 9 * HOUR)
    # The operator's terminal first: once this process holds bindings it is inside
    # their sessions, where the operator's lane is refused.
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "from the operator"],
                        env={"MOXI_COMMS_OPERATOR": rig.prov.operator_user_id})
    if rig.backend == "local":
        assert code == 0 and out.splitlines()[-1].startswith(HELD_LAST), out
    else:   # the mock keeps no operator credential: the post fails after the check
        assert code == cli.EXIT_FAILED and "posting credential" in out, out
    bind_thinker(bind, session_id="")
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "from thinker"], name="thinker",
                        env={"MOXI_COMMS_GLOBAL": "1"})
    assert code == 0 and out.splitlines()[-1] == HELD_LAST, out
    bind(slot=4, name="console", scope="acme")
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "from the console"], name="console",
                        scope="acme")
    assert code == 0 and out.splitlines()[-1] == HELD_LAST, out
    assert len(posted(rig)) == (3 if rig.backend == "local" else 2)
    wait_until(lambda: held_of(rig) == {m.id for m in posted(rig)})


def test_thinker_is_checked_as_a_recipient_and_held_like_any(rig, bind):
    """A cold global name is held like every recipient ([F5], [F8]): posted, a row."""
    bind_thinker(bind)
    write_transcript(SESS_THINKER, 3 * HOUR)
    code, out = send(rig, "thinker")
    assert code == 0 and len(posted(rig)) == 1, out
    assert held_line(out).startswith("held: thinker's cache went cold 2h ago")
    assert out.splitlines()[-1] == (
        "Sent and held: thinker reads this after its own next turn, or when its session "
        "is cleared, compacted or restarted. Nothing wakes it for this message alone.")
    (msg,) = posted(rig)
    assert wait_until(lambda: msg.id in held_of(rig, "thinker"))


def test_unbound_and_operator_recipients_are_not_read(rig, bind):
    bind_operator(bind)
    code, out = send(rig, "nobody-yet", "hello", "--new-name")
    assert code == 0 and "not bound right now" in out
    code, out = send(rig, "operator")
    assert code == 0 and "NOTE" not in out


def test_several_recipients_are_held_per_recipient(rig, bind):
    """One post; warm thinker gets it now, the two cold shims are held."""
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 2 * HOUR)
    bind_thinker(bind)
    write_transcript(SESS_THINKER, 300)          # warm
    rig.shim(slot=4, name="main", scope="proj-a", session_id=SESS_OTHER)
    write_transcript(SESS_OTHER, 3 * HOUR)       # cold too
    code, out = send(rig, "reviewer@proj-a,thinker,main@proj-a")
    assert code == 0 and len(posted(rig)) == 1, out
    held_lines = [x for x in out.splitlines() if x.startswith("held:")]
    assert len(held_lines) == 2
    assert "reviewer@proj-a" in held_lines[0] and "main@proj-a" in held_lines[1]
    (msg,) = posted(rig)
    assert wait_until(lambda: msg.id in held_of(rig) and msg.id in held_of(rig, "main@proj-a"))
    assert msg.id not in held_of(rig, "thinker")


def test_two_held_recipients_each_get_their_own_line(rig, bind):
    reviewer_with_session(rig)
    rig.shim(slot=4, name="main", scope="proj-a", session_id=SESS_OTHER)
    write_transcript(SESS_REVIEWER, 2 * HOUR)
    write_transcript(SESS_OTHER, 4 * HOUR)
    code, out = send(rig, "reviewer@proj-a,main@proj-a")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("sent ")
    assert lines[1].startswith("held: reviewer@proj-a's")
    assert lines[2].startswith("held: main@proj-a's")
    assert lines[3:] == [
        "Sent and held: reviewer@proj-a and main@proj-a read this after their own next "
        "turns, or when their sessions are cleared, compacted or restarted. Nothing wakes "
        "them for this message alone."]


# ===================================================== (e) moxi comms cache

def cache_cli(rig, *targets, **kw):
    return rig.cli(["cache", *targets], **kw)


AS_THINKER = dict(name="thinker", env={"MOXI_COMMS_GLOBAL": "1"})
AS_OPERATOR = dict(env={"MOXI_COMMS_OPERATOR": "900001"})


def test_comms_cache_speaks_one_line_per_kind(rig, bind):
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 4 * 60)
    rig.shim(slot=4, name="main", scope="proj-a", session_id=SESS_OTHER)
    write_transcript(SESS_OTHER, HOUR + 18 * HOUR)
    bind_thinker(bind)
    write_transcript(SESS_THINKER, HOUR + 70 * 60)
    code, out = cache_cli(rig, "reviewer@proj-a", "main@proj-a", "thinker",
                          SESS_THINKER[:8] + SESS_REVIEWER[8:], **AS_OPERATOR)
    assert code == 0, out
    lines = out.splitlines()
    # The transcript's stamp is to the second, so the read is 240 to 241 s old.
    assert lines[0] == ("reviewer@proj-a: cache warm, last turn 4 minutes ago, expires in "
                        "55 minutes.")
    assert lines[1] == ("main@proj-a: cache expired 18 hours ago. No agent can reach it "
                        "now; only the operator, in its session.")
    assert lines[2] == ("thinker: cache expired 1 hour 10 minutes ago. No agent can reach "
                        "it now; only the operator, in its session.")
    # v0.6: a raw id with no transcript carries no evidence that it made no request
    assert lines[3].endswith("Agents' messages to it are held.")
    assert ": cache state not known (no transcript for session" in lines[3]


def test_comms_cache_with_no_argument_reads_every_bound_name(rig, bind):
    reviewer_with_session(rig)
    bind_thinker(bind, session_id="")
    code, out = cache_cli(rig, **AS_OPERATOR)
    assert code == 0, out
    assert "thinker: new session, no turn yet, nothing cached." in out
    assert "reviewer@proj-a: new session, no turn yet, nothing cached." in out


def test_comms_cache_says_why_it_does_not_know(rig, bind):
    rig.shim(**REVIEWER, session_id=None)
    bind(slot=5, name="coder", scope="proj-a", adapter="codex")
    code, out = cache_cli(rig, "reviewer@proj-a", "coder@proj-a", "nobody", **AS_OPERATOR)
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == ("reviewer@proj-a: cache state not known (no session id reported "
                        "yet). Agents' messages to it are held.")
    assert lines[1] == ("coder@proj-a: cache state not known (no thread id reported "
                        "yet). Agents' messages to it are held.")
    assert "not bound right now" in lines[2]


@pytest.mark.parametrize("who", [
    dict(**PLANNER),                                       # any other name
    dict(name="thinker", env={"MOXI_COMMS_GLOBAL": "1"}),  # a global name, not listed
    dict(),                                                # no identity at all
    dict(name="operator", env={"MOXI_COMMS_GLOBAL": "1", "MOXI_COMMS_SLOT": "x"}),  # malformed
    dict(name="planner", env={"MOXI_COMMS_OPERATOR": "900001"}),   # both stamps at once
])
def test_comms_cache_refuses_any_other_caller_and_reads_nothing(rig, who):
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 60)
    code, out = cache_cli(rig, "reviewer@proj-a", SESS_OTHER, **who)
    assert code == 2
    assert out.strip().endswith("nothing was read."), out


# ========================================================== (f) --next-turn

def test_the_mark_rides_line_zero_in_its_place():
    # An old record's `[next-turn]` and `[speaker]` still parse (comms/refine §3.8.1, §6.0).
    ml = machine_line.parse("::to a, b re:12 [audience:x] [Next-Turn] [speaker]\nhi")
    assert ml.held and ml.recipients == ("a", "b") and ml.reply_to == "12"
    assert machine_line.parse("::to a [next-turn]\nhi").held
    assert machine_line.parse("::to a [held]\nhi").held
    assert not machine_line.parse("::to a\nhi").held
    for prose in ("::to a [held] [held]\nx", "::to a [held] re:12\nx",
                  "::to a [speaker] [held]\nx"):
        assert machine_line.parse(prose) is None, prose
    assert machine_line.format_line(["a"], reply_to="12", audience="op", next_turn=True) == \
        "::to a re:12 [audience:op] [held]"


def test_a_held_send_waits_with_no_hand_over_and_nothing_delivered(rig):
    """`--held` reads the cache like any send (§3.8.1); the message is a row, flagged,
    and nothing is handed."""
    reviewer = reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 9 * HOUR)
    code, out = send(rig, REVIEWER_Q, "later", "--held")
    assert code == 0, out
    mid = sent_id(out)
    assert posted(rig)[0].content.splitlines()[0] == "::to reviewer@proj-a [held] [no reply]"
    assert ("NOTE: reviewer@proj-a reads this after its own next turn, or when its session "
            "is cleared, compacted or restarted. Nothing wakes it for this message alone."
            ) in out.splitlines()
    wait_until(lambda: mid in held_of(rig))
    time.sleep(0.2)
    assert reviewer.queue == []
    assert not any(k[1] == mid for k in rig.daemon.handed_at)


def test_the_recipients_own_turn_hands_it_over_and_handled_settles_it(rig):
    reviewer = rig.shim(**REVIEWER)
    mid = next_turn(rig)
    send_turn(reviewer)                          # a turn on a warm cache, nothing handled
    (payload,) = wait_for_queue(reviewer, 1)
    assert payload["message_id"] == mid and payload["held"] is True
    assert held_of(rig) == {mid}, "the row stays until the adapter names it handled"
    line = frame_line(payload)
    assert re.search(r"\[held\] sent (\w{3} \d{2} )?\d{2}:\d{2}", line), line
    reviewer.on_turn_boundary()                 # the idle flush injects it
    assert "when you are next" in reviewer.injected[-1]
    send_turn(reviewer, handled=[mid])
    assert wait_until(lambda: held_of(rig) == set())


def test_a_newer_message_goes_alone_and_the_row_waits(rig):
    """No piggyback ([F3]): a live message to the name hands only itself."""
    reviewer = rig.shim(**REVIEWER)
    held = next_turn(rig)
    code, out = send(rig, REVIEWER_Q, "now please")
    now = sent_id(out)
    wait_for_queue(reviewer, 1)
    time.sleep(0.2)
    assert queued_ids(reviewer) == [now]
    assert held_of(rig) == {held}


def test_a_cursor_past_a_held_id_leaves_its_row(rig):
    """Rows no longer need the cursor ([I1], [R2]): live mail past a queued id moves the
    cursor past it, and the row stays until a `turn` names it handled."""
    reviewer = rig.shim(**REVIEWER)
    held = next_turn(rig)
    for i in range(3):
        send(rig, REVIEWER_Q, f"plain {i}")
    wait_for_queue(reviewer, 3)
    reviewer.on_turn_boundary(idle=True)     # the idle tick injects the live mail
    reviewer.on_turn_boundary()              # its Stop: cursor written, then `turn`
    assert int(cursors.read(REVIEWER_Q)) > int(held)
    assert held_of(rig) == {held}
    # First digest only: the Stop's warm release (`handled=[]`) may land before its flush.
    assert held not in reviewer.injected[0]


def test_a_ready_with_no_source_releases_nothing_and_a_clear_releases(rig):
    """A `ready` with no `source` is a pre-round adapter's; it releases nothing. One with
    `clear` releases every row with no read (§3.5)."""
    reviewer = reviewer_with_session(rig)
    mid = next_turn(rig)
    send_ready(reviewer, None)
    time.sleep(0.2)
    assert reviewer.queue == [] and held_of(rig) == {mid}
    send_ready(reviewer, "clear")
    assert queued_ids(wait_for_queue(reviewer, 1)) == [mid]


def test_an_activity_with_no_kind_releases_nothing(rig):
    reviewer = rig.shim(**REVIEWER)
    mid = next_turn(rig)
    reviewer._client.notify("activity", identity=REVIEWER_Q)      # an older shim's frame
    time.sleep(0.2)
    assert reviewer.queue == [] and held_of(rig) == {mid}


def test_a_new_sessions_held_mail_goes_at_its_start(rig):
    """Flagged mail to an unbound name: the register releases only unflagged rows; the
    session's `ready source=startup` releases it ([F2], [R7])."""
    mid = next_turn(rig)                             # unbound: a row
    reviewer = rig.shim(**REVIEWER, session_id=SESS_REVIEWER)
    wait_until(lambda: not reviewer.replaying)
    time.sleep(0.2)
    assert reviewer.queue == [], "a register is not a real boundary for flagged mail"
    send_ready(reviewer, "startup")
    assert queued_ids(wait_for_queue(reviewer, 1)) == [mid]


def test_a_row_already_handed_is_not_handed_again(rig):
    """Re-hand ([R58]): a row in the binding's `delivered` is not handed by a release;
    the row stays."""
    reviewer = rig.shim(**REVIEWER)
    mid = next_turn(rig)
    with rig.daemon._lock:
        rig.daemon.bindings[REVIEWER_Q].delivered.add(mid)     # handed already
    send_turn(reviewer)
    time.sleep(0.2)
    assert reviewer.queue == [] and held_of(rig) == {mid}


def test_queued_mail_is_never_bounded_and_the_flagged_row_waits(rig):
    """Bounded replay is for mail with no row (§3.3.4): offline rows past REPLAY_N and
    older than REPLAY_T all go at the bind's release; the flagged one waits for a real
    boundary."""
    mid = next_turn(rig)
    rig.age_message(mid, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(rig.clock() - 2 * 86400)))
    offline = post_many(rig, [REVIEWER_Q], constants.REPLAY_N + 10)
    assert wait_until(lambda: len(held_of(rig)) == constants.REPLAY_N + 11)
    reviewer = rig.shim(**REVIEWER)
    wait_until(lambda: not reviewer.replaying)
    wait_for_queue(reviewer, constants.REPLAY_N + 10)
    assert queued_ids(reviewer) == [m.id for m in offline]
    assert reviewer.replay_notice is None, "nothing was bounded"
    assert mid not in queued_ids(reviewer)


def test_the_same_session_re_registering_with_only_held_mail_gets_nothing(rig):
    first = rig.shim(**REVIEWER)
    mid = next_turn(rig)
    first.stop()
    wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
    again = rig.shim(**REVIEWER)
    wait_until(lambda: not again.replaying)
    time.sleep(0.2)
    assert again.queue == [] and held_of(rig) == {mid}


def test_a_daemon_restart_rebuilds_held_and_the_reconnect_releases_nothing(rig, reboot):
    reviewer = rig.shim(**REVIEWER, autoreconnect=True, backoff=fast_backoff())
    mid = next_turn(rig)
    daemon = reboot(rig)
    assert mid in held_of(rig), "the boot read rebuilt it"
    wait_until(lambda: REVIEWER_Q in daemon.bindings and not reviewer.offline)
    time.sleep(0.3)
    assert reviewer.queue == [] and held_of(rig) == {mid}


def test_a_reconnect_whose_replay_finds_only_holding_duplicates_releases_nothing(
        rig, reboot):
    reviewer = rig.shim(**REVIEWER, autoreconnect=True, backoff=fast_backoff())
    code, out = send(rig, REVIEWER_Q, "queued, not yet shown")
    plain = sent_id(out)
    wait_for_queue(reviewer, 1)
    held = next_turn(rig)
    daemon = reboot(rig)
    wait_until(lambda: REVIEWER_Q in daemon.bindings and not reviewer.offline)
    time.sleep(0.3)
    assert queued_ids(reviewer) == [plain]
    # The boot rescan found plain to an unbound name: a row, which the reconnect's
    # `holding` claims, so no release hands it again; it waits for `handled`.
    assert held_of(rig) == {held, plain}
    assert plain in daemon.bindings[REVIEWER_Q].delivered, "claimed from `holding`"


def test_the_boot_read_finds_held_mail_past_the_rescan_limit(rig, reboot):
    mid = next_turn(rig)
    post_many(rig, ["main@proj-b"], constants.RESCAN_LIMIT + 10)
    reboot(rig)
    assert held_of(rig) == {mid}


def test_unbind_keeps_a_handed_rows_row(rig):
    """A row handed and never named handled stays through the unbind ([R2])."""
    reviewer = rig.shim(**REVIEWER)
    mid = next_turn(rig)
    send_turn(reviewer)
    wait_for_queue(reviewer, 1)                 # handed, never shown
    assert held_of(rig) == {mid}
    reviewer.stop()
    wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
    assert held_of(rig) == {mid}


def test_an_ended_agent_n_loses_its_held_mail(rig):
    auto = rig.shim(slot=5)
    q = auto.identity
    mid = next_turn(rig, to=q)
    auto.stop()
    wait_until(lambda: q not in rig.daemon.bindings)
    assert mid not in held_of(rig, q)


def test_the_trim_keeps_held_mail_past_retention(rig):
    if rig.backend != "local":
        pytest.skip("only the local record trims")
    mid = next_turn(rig)
    old = rig.post_from_slot(6, ["main@proj-b"], "ancient")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rig.clock() - 200 * 86400))
    rig.age_message(mid, stamp)
    rig.age_message(old.id, stamp)
    rig.daemon.trim_record()
    assert rig.gateway.get_message(mid) is not None
    assert rig.gateway.get_message(old.id) is None


def test_m2_the_queue_load_runs_before_the_socket_listens(rig, monkeypatch):
    """Nothing can register, and so nothing can be handed, until every row is loaded
    (§3.3.1). Live mail posted inside the boot window waits as a row and goes at the
    bind; the flagged row waits for a real boundary."""
    from moxi_comms.client import DaemonOffline
    held = next_turn(rig)
    rig.daemon.stop()
    entered, go = threading.Event(), threading.Event()
    real = Daemon._load_queue

    def gated(self):
        entered.set()
        go.wait(10)
        return real(self)

    monkeypatch.setattr(Daemon, "_load_queue", gated)
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    boot = threading.Thread(target=daemon.start)
    boot.start()
    try:
        assert entered.wait(5)
        with pytest.raises(DaemonOffline):
            DaemonClient(daemon_socket()).connect()      # not listening yet
        live = rig.post_from_slot(6, [REVIEWER_Q], "live, inside the boot window",
                                  display="planner@proj-a")
    finally:
        go.set()
        boot.join(10)
    rig.daemon = daemon
    assert wait_until(lambda: held_of(rig) == {held, live.id})
    reviewer = rig.shim(**REVIEWER)
    assert queued_ids(wait_for_queue(reviewer, 1)) == [live.id]
    time.sleep(0.2)
    assert queued_ids(reviewer) == [live.id], "the flagged row waits"


def test_m1_the_trim_waits_for_the_queue_load(rig, monkeypatch, reboot):
    """The trim keeps every queued id, so it never runs before the queue is loaded
    (§3.11); a load that raises stops the start."""
    if rig.backend != "local":
        pytest.skip("only the local record trims")
    mid = next_turn(rig)
    ancient = rig.post_from_slot(6, ["main@proj-b"], "ancient")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rig.clock() - 200 * 86400))
    rig.age_message(mid, stamp)
    rig.age_message(ancient.id, stamp)
    cursors.advance("main@proj-b", ancient.id)     # handled: no row for it at the boot
    rig.daemon.stop()
    unloaded = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    assert unloaded._queue_loaded is False
    unloaded.trim_record()
    assert rig.gateway.get_message(mid) is not None
    assert rig.gateway.get_message(ancient.id) is not None, "skipped, not partial"

    def broken(self):
        raise RuntimeError("the boot read failed")

    with monkeypatch.context() as patched:
        patched.setattr(rig.gateway, "queue_rows", lambda: broken(None))
        with pytest.raises(RuntimeError):
            Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    assert os.environ["CLAUDE_CONFIG_DIR"].startswith(tempfile.gettempdir())
    daemon = reboot(rig)
    assert daemon._queue_loaded is True
    daemon.trim_record()
    assert rig.gateway.get_message(mid) is not None
    assert rig.gateway.get_message(ancient.id) is None


def test_m4_the_release_order_is_kept_per_name_not_across_names(rig):
    planner = rig.shim(**PLANNER)
    rig.shim(**REVIEWER)
    assert rig.daemon._name_lock(REVIEWER_Q) is rig.daemon._name_lock(REVIEWER_Q)
    stuck, done = threading.Event(), threading.Event()

    def hold_reviewers_lock():
        with rig.daemon._name_lock(REVIEWER_Q):
            stuck.set()
            done.wait(10)

    t = threading.Thread(target=hold_reviewers_lock)
    t.start()
    try:
        assert stuck.wait(5)
        code, out = send(rig, "planner@proj-a", "not stuck behind reviewer", sender=REVIEWER)
        assert code == 0, out
        wait_for_queue(planner, 1)
        assert queued_ids(planner) == [sent_id(out)]
    finally:
        done.set()
        t.join(5)


def test_m6_held_to_a_shim_with_no_session_id_is_refused(rig):
    """A shim that reports no session id reads unknown, and `--held` is no exception
    (§3.8.1, §3.8.3): exit 6, nothing posted."""
    rig.shim(**REVIEWER, session_id=None)      # like a shim built before this comms
    rig.clock.advance(constants.SESSION_ID_GRACE + 1)
    code, out = send(rig, REVIEWER_Q, "later", "--held")
    assert code == cli.EXIT_COLD and posted(rig) == [], out
    assert out.splitlines()[0] == ("Nothing was sent: comms could not read "
                                   "reviewer@proj-a's cache (no session id reported yet).")


class Pushes:
    """A bare adapter binding that records what the daemon pushes to it."""

    def __init__(self):
        self.frames = []

    def __call__(self, frame):
        self.frames.append(frame)

    def delivered(self):
        return [f["payload"] for f in self.frames if f.get("op") == "deliver"]


@pytest.mark.parametrize("kw,at_register", [
    (dict(slot=5, name="coder", scope="proj-a", adapter="codex"), False),
    (dict(slot=3, name="thinker", global_=True, adapter="claude-shim",
          session_id=SESS_BLANK, turned=False), False),
    (dict(slot=4, name="operator", global_=True, adapter="claude-shim",
          session_id=SESS_OPERATOR, turned=False), True),
], ids=["codex", "global-name", "operator"])
def test_m7_flagged_mail_waits_at_every_adapters_bind_but_the_operators(
        rig, kw, at_register):
    """Every adapter takes held mail ([F5], [F8]): a flagged row waits through the
    register and goes at a real boundary (a session start). The flag never holds the
    operator ([R12]): his is offline mail, released at his register on a passing read."""
    q = "coder@proj-a" if kw["name"] == "coder" else kw["name"]
    tag = q if "@" in q else f"{q}@global"
    if kw["adapter"] == "claude-shim":                 # no request yet: fresh
        kw = {**kw, "projects_root": str(projects_root())}
    (held,) = post_many(rig, [tag], 1, next_turn=True)     # hand-typed
    assert wait_until(lambda: held.id in held_of(rig, q))
    pushes = Pushes()
    c = DaemonClient(daemon_socket(), on_push=pushes).connect()
    try:
        c.register(**kw)
        if at_register:
            assert wait_until(lambda: pushes.delivered())
        else:
            time.sleep(0.2)
            assert pushes.delivered() == []
            c.notify("activity", identity=q, kind="ready", source="startup")
            assert wait_until(lambda: pushes.delivered())
        (payload,) = pushes.delivered()
        assert payload["message_id"] == held.id and payload["held"] is True
        assert held_of(rig, q) == {held.id}, "the row stays until handled"
    finally:
        c.close()


def test_comms_agents_and_comms_explain_name_queued_mail(rig):
    """`agents` answers `waiting` per name, with the oldest row's time, and `pending`
    always empty ([R63]); `explain` names the case `waiting` (§3.11). The roster's
    rendering of `waiting` is §7.3's."""
    rig.shim(**REVIEWER)
    mid = next_turn(rig)
    sent_at = rig.clock()
    rig.clock.advance(2 * 86400 + 3 * HOUR)
    c = DaemonClient(daemon_socket()).connect()
    try:
        view = c.query("agents")
    finally:
        c.close()
    assert view["pending"] == {} and "held" not in view
    assert view["waiting"][REVIEWER_Q]["count"] == 1
    assert abs(view["waiting"][REVIEWER_Q]["oldest"] - sent_at) <= 2
    code, out = rig.cli(["agents", "--seats"], **PLANNER, clock=rig.clock)
    assert f"  {'reviewer@proj-a':<24} 1 message(s), oldest waiting 2d 3h" in out
    assert "pending" not in out
    code, out = rig.cli(["explain", REVIEWER_Q, mid], **PLANNER)
    assert code == 0 and out.splitlines()[0].startswith(f"{REVIEWER_Q}   {cursors.WAITING}")
    assert "This message waits in the queue for the name's next release point" in \
        out.replace("\n  ", " ")
    assert cursors.BEHIND not in out


def test_held_is_accepted_for_thinker_and_ignored_for_the_operator(rig, bind):
    """`--held` is accepted for every recipient ([F5], [F8]); for the operator it is
    ignored and said so ([F21])."""
    rig.shim(**REVIEWER)
    bind_thinker(bind, session_id="")                    # blank anchor: fresh
    bind_operator(bind)
    code, out = send(rig, "reviewer@proj-a,thinker", "later", "--held")
    assert code == 0, out
    mid = sent_id(out)
    assert wait_until(lambda: mid in held_of(rig) and mid in held_of(rig, "thinker"))
    code, out = send(rig, "reviewer@proj-a,operator", "later", "--held")
    assert code == 0, out
    assert ("NOTE: --held was ignored for operator: the operator has no session to hold "
            "it for, so it went to him as an ordinary message.") in out.splitlines()
    mid = sent_id(out)
    assert wait_until(lambda: mid in held_of(rig))
    assert mid not in held_of(rig, "operator")


def test_held_to_an_unbound_thinker_or_operator_by_name_posts(rig):
    code, out = send(rig, "thinker@global", "later", "--held", "--new-name")
    assert code == 0, out
    assert posted(rig)[-1].content.splitlines()[0].endswith("[held] [no reply]")
    code, out = send(rig, "operator@global", "later", "--held", "--new-name")
    assert code == 0, out
    assert "[held]" not in posted(rig)[-1].content.splitlines()[0], \
        "the flag alone to the operator writes no token"


def test_reply_takes_held(rig):
    reviewer = rig.shim(**REVIEWER)
    ask = rig.post_from_slot(6, ["planner@proj-a"], "q?", display="reviewer@proj-a")
    code, out = rig.cli(["reply", ask.id, "later.", "--held"], **PLANNER)
    assert code == 0, out
    assert posted(rig)[-1].content.splitlines()[0] == \
        f"::to reviewer@proj-a re:{ask.id} [held]"
    wait_until(lambda: held_of(rig))
    assert reviewer.queue == []


# ================================================= the senders and the cache callers

AS_CONSOLE = dict(name="console", scope="acme")


def test_comms_cache_answers_the_operator_alone_by_default(rig):
    """`[policy] cache_callers` is `["operator"]` unless the box's config says more."""
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 60)
    code, out = cache_cli(rig, "reviewer@proj-a", **AS_THINKER)
    assert code == 2 and out.strip().endswith("nothing was read."), out
    code, out = cache_cli(rig, "reviewer@proj-a", **AS_OPERATOR)
    assert code == 0 and out.startswith("reviewer@proj-a: cache warm"), out


def test_a_retired_policy_key_fails_the_load_with_a_fix():
    """[R62]: `never_held` and `next_turn_refused` are retired keys; a config that still
    names one fails the load and names the key, rather than being read."""
    from moxi_comms import config

    for key in ("never_held", "next_turn_refused"):
        write_config(None, f'[policy]\n{key} = ["console@tools"]\n')
        with pytest.raises(config.ConfigError) as err:
            config.load()
        assert key in str(err.value), err.value


def test_cache_callers_names_who_may_read_the_cache(rig, bind):
    write_config(None, '[policy]\ncache_callers = ["operator", "planner@proj-a"]\n')
    reviewer_with_session(rig)
    write_transcript(SESS_REVIEWER, 9 * HOUR)
    bind(slot=4, name="console", scope="tools")
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "from tools"],
                        name="console", scope="tools")
    assert code == 0, out
    assert "NOTE" not in out and out.splitlines()[-1] == HELD_LAST
    code, out = cache_cli(rig, "reviewer@proj-a", **PLANNER)
    assert code == 0 and out.startswith("reviewer@proj-a: cache expired"), out
