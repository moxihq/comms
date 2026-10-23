"""The claim socket's two ops and who may call each, the membership rule, the claim file,
the cursor's hold guard, and the adapter states' transitions."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from claim_kit import FakeProc, World
from moxi_comms import claimfile, claimhold
from moxi_comms.claimhold import NOT_INSIDE, ClaimHolder
from moxi_comms.membership import SELF, TREE, ProcView, Root, inside, root_of, sessions_of

SEAT, OTHER = "seat@proj-a", "other@proj-a"


@pytest.fixture
def world(rig):
    w = World()
    yield w
    w.stop()


def pair(world):
    """Two sessions of the same scope, each bound under its own name."""
    return world.session("seat", slot=2), world.session("other", slot=3)


# --------------------------------------------------------------------------- membership

def test_the_nearest_root_decides_and_sessions_nest():
    proc = FakeProc()
    outer = proc.spawn()
    mid = proc.child(outer)
    inner = proc.child(mid)
    leaf = proc.child(inner)
    table = [("outer", [Root(outer, f"s{outer}", TREE)]),
             ("inner", [Root(inner, f"s{inner}", TREE)])]
    assert sessions_of(mid, table, proc) == ["outer"]
    assert sessions_of(leaf, table, proc) == ["inner"]      # never the outer one
    assert sessions_of(proc.spawn(), table, proc) == []      # inside no session


def test_a_self_root_admits_only_itself_and_may_hold_several_bindings():
    proc = FakeProc()
    loop = proc.spawn()
    turn = proc.child(loop)
    stray = proc.child(loop)
    table = [("operator", [Root(loop, f"s{loop}", SELF)]),
             ("archivist", [Root(loop, f"s{loop}", SELF), Root(turn, f"s{turn}", TREE)])]
    assert sorted(sessions_of(loop, table, proc)) == ["archivist", "operator"]
    assert sessions_of(turn, table, proc) == ["archivist"]
    assert sessions_of(proc.child(turn), table, proc) == ["archivist"]
    assert sessions_of(stray, table, proc) == []
    assert not inside(turn, "operator", table, proc)


def test_a_reused_pid_is_not_its_dead_root():
    proc = FakeProc()
    root = proc.spawn()
    child = proc.child(root)
    table = [("dead", [Root(root, "an-earlier-start", TREE)])]
    assert sessions_of(child, table, proc) == []
    assert not claimfile.tree_root_alive({"roots": [table[0][1][0].as_dict()]}, proc)


def test_the_real_proc_reads_a_start_time_and_a_parent():
    view = ProcView()
    me = root_of(os.getpid())
    assert me is not None and me.start.isdigit()
    assert view.parent(os.getpid()) == os.getppid()
    assert view.alive(os.getpid(), me.start)
    assert not view.alive(os.getpid(), str(int(me.start) + 1))


# --------------------------------------------------------------------------- classes

def test_claim_answers_inside_the_session_only(rig, world):
    p, n = pair(world)
    assert world.call(p, p.verb_pid, "claim")["claim"]["qualified"] == SEAT
    stranger = world.proc.spawn()
    answer = world.call(p, stranger, "claim")
    assert not answer["ok"] and answer["error"] == NOT_INSIDE
    answer = world.call(n, p.verb_pid, "claim")               # P's verb on N's socket
    assert not answer["ok"] and answer["error"] == NOT_INSIDE
    assert world.call(n, n.verb_pid, "claim")["claim"]["qualified"] == OTHER


def test_state_answers_anyone_and_carries_nothing_to_send_as(rig, world):
    p, n = pair(world)
    answer = world.call(p, world.proc.spawn(), "state")
    assert answer["ok"] and answer["claim"] == SEAT
    assert world.call(p, n.verb_pid, "state")["claim"] == SEAT    # N's verb on P's socket
    # the trimmed answer, and nothing in it to send as
    assert set(answer) == {"ok", "state", "kind", "key", "claim", "claim_name", "lost"}


def test_the_real_socket_reads_the_callers_pid(rig, tmp_path):
    """SO_PEERCRED through a real socket: a real subprocess outside every recorded
    root is refused `claim` and answered `state`."""
    me = root_of(os.getpid(), SELF)
    h = ClaimHolder(kind="codex", key="real-test", keying="pid",
                    socket_path=tmp_path / "c.sock", roots=[me])
    h.start_launch({"name": "x", "scope": "proj-a", "qualified": "x@proj-a",
                    "slot": 1, "directors": []})
    h.state = claimfile.BOUND
    h.listen()
    try:
        assert claimhold.call(h.socket_path, "claim")["claim"]["qualified"] == "x@proj-a"
        code = ("import sys,json\nfrom moxi_comms import claimhold\n"
                "a=claimhold.call(sys.argv[1],'claim'); b=claimhold.call(sys.argv[1],'state')\n"
                "print(json.dumps([a.get('error'), b.get('claim')]))")
        out = subprocess.run([sys.executable, "-c", code, str(h.socket_path)],
                             capture_output=True, text=True, check=True).stdout
        assert NOT_INSIDE in out and "x@proj-a" in out
    finally:
        h.close()


# --------------------------------------------------------------------------- transitions

def test_a_name_held_elsewhere_moves_a_held_claim_to_standby_and_leaves_a_launch_waiting(
        rig, world):
    from moxi_comms.client import DaemonError
    p = world.session("seat", slot=2)
    late = world.session("seat", slot=2, bind=False)
    with pytest.raises(DaemonError) as e:
        late.bind()
    holder = (e.value.response.get("holder") or {}).get("qualified")
    late.holder.name_refused(holder)
    assert late.state == "waiting" and late.holder.claim
    p.holder.name_refused(holder)                 # as if its register were refused now
    assert p.state == "standby" and p.holder.lost == SEAT and p.holder.claim is None


def test_the_file_holds_the_recorded_keys_and_goes_with_a_clean_stop(rig, world):
    p, n = pair(world)
    rec = n.record()
    for key in ("roots", "launch_session", "hold", "exact_slot", "lost"):
        assert key in rec
    assert rec["state"] == "bound" and rec["claim"]["qualified"] == OTHER
    for key in ("for", "pass_from", "launch_argv", "retired", "contingent",
                "contingent_to", "grants"):                  # keys an older file carried
        assert key not in rec
    assert "temp" not in rec["claim"]
    assert oct(os.stat(claimfile.file_for(n.key)).st_mode)[-3:] == "600"
    n.stop()
    assert claimfile.read(n.key) is None


def test_an_old_files_passing_state_loads_as_standby(rig, tmp_path):
    """A claim file left by an older process may say
    `state: passing`: it loads as `standby`, holding nothing and registering nothing."""
    h = ClaimHolder(kind="claude-shim", key="old-passing", keying="session",
                    socket_path=tmp_path / "c.sock", roots=[])
    h.load({"state": "passing", "hold": 3, "lost": None,
            "claim": {"name": "seat", "scope": "proj-a", "global": False,
                      "qualified": SEAT, "slot": 2, "tier": None, "directors": []}})
    assert h.state == "standby"
    assert not h.holds and not h.may_register()
    h.listen()
    try:
        answer = claimhold.call(h.socket_path, "state")
        assert answer["ok"] and answer["state"] == "standby" and answer["claim"] is None
    finally:
        h.close()


def test_a_cursor_moves_only_in_the_hold_its_digest_was_injected_under(rig, world):
    p, n = pair(world)
    mark = p.holder.hold_mark()
    assert mark == (SEAT, 1) and p.holder.may_advance(mark)
    assert not p.holder.may_advance((SEAT, 2))                   # another hold
    assert not p.holder.may_advance((OTHER, 1))                  # another name
    assert not p.holder.may_advance(None)
    assert not n.holder.may_advance(mark)
    p.holder.went_offline()                                      # still held, same hold
    assert p.state == "offline" and p.holder.may_advance(mark)
    p.holder.name_refused(SEAT)                                  # the name went elsewhere
    assert p.state == "standby" and p.holder.hold_mark() is None
    assert not p.holder.may_advance(mark)

