"""`ClaimServer`: a binding held by a module's own process. Its holder is a `self` root,
each child it spawns for the binding a `tree` root written before the child runs and
dropped at its exit. Real processes, real `/proc`, real `SO_PEERCRED`."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from claim_kit import wait_for
from moxi_comms import claimfile
from moxi_comms.claimhold import CLAIM_SOCKET_ENV, NOT_INSIDE, ClaimServer, call

ASK = ("import json,os,sys\nfrom moxi_comms import claimhold\n"
       "out={}\n"
       "for path in sys.argv[1:]:\n"
       "    a=claimhold.call(path,'claim')\n"
       "    out[path]=a.get('claim',{}).get('qualified') if a.get('ok') else a.get('error')\n"
       "out['env']=os.environ.get('MOXI_COMMS_CLAIM_SOCKET')\n"
       "print(json.dumps(out))\n")


@pytest.fixture
def servers(rig):
    operator = ClaimServer("operator", "global", kind="codex").start()
    thinker = ClaimServer("thinker", "global", kind="opencode").start()
    operator.set_bound(slot=1, tier="standing")
    thinker.set_bound(slot=2, tier="chosen")
    yield operator, thinker
    operator.stop()
    thinker.stop()


def test_the_holder_sends_as_each_binding_it_holds(servers):
    operator, thinker = servers
    assert call(operator.socket_path, "claim")["claim"]["qualified"] == "operator"
    assert call(thinker.socket_path, "claim")["claim"]["qualified"] == "thinker"


def test_a_child_root_is_inside_its_binding_and_refused_on_the_self_only_one(servers):
    operator, thinker = servers
    child = thinker.spawn([sys.executable, "-c", ASK, str(thinker.socket_path),
                           str(operator.socket_path)], stdout=subprocess.PIPE, text=True)
    out = json.loads(child.communicate(timeout=30)[0])
    assert out[str(thinker.socket_path)] == "thinker"
    assert out[str(operator.socket_path)] == NOT_INSIDE
    assert out["env"] == str(thinker.socket_path)
    assert wait_for(lambda: all(r["pid"] != child.pid
                                for r in claimfile.read(thinker.holder.key)["roots"]))


def test_any_other_child_of_the_holder_is_inside_no_binding(servers):
    operator, thinker = servers
    out = subprocess.run([sys.executable, "-c", ASK, str(thinker.socket_path),
                          str(operator.socket_path)], capture_output=True, text=True,
                         check=True).stdout
    answers = json.loads(out)
    assert answers[str(thinker.socket_path)] == NOT_INSIDE
    assert answers[str(operator.socket_path)] == NOT_INSIDE


def test_state_answers_and_every_old_act_is_an_unknown_op(servers):
    operator, _ = servers
    assert call(operator.socket_path, "state")["claim"] == "operator"
    for op in ("lock", "release", "take", "confirm", "reclaim", "abandon", "contingent",
               "uncontingent"):
        answer = call(operator.socket_path, op, verb="v", receiver="x", token="operator")
        assert not answer["ok"] and f"unknown claim op {op!r}" in answer["error"], op


def test_the_file_records_the_holder_as_a_self_root(servers):
    operator, _ = servers
    rec = claimfile.read(operator.holder.key)
    assert [r["reach"] for r in rec["roots"]] == ["self"]
    assert rec["state"] == "bound" and rec["claim"]["qualified"] == "operator"
    assert operator.env() == {CLAIM_SOCKET_ENV: str(operator.socket_path)}


def test_with_its_own_daemon_arm_it_binds_and_goes_offline_with_the_daemon(rig):
    srv = ClaimServer("voicearm", "proj-a", kind="codex", arm=True).start()
    try:
        assert wait_for(lambda: "voicearm@proj-a" in rig.daemon.bindings)
        assert srv.holder.state == "bound"
        assert call(srv.socket_path, "claim")["claim"]["slot"] is not None
    finally:
        srv.stop()
    assert wait_for(lambda: "voicearm@proj-a" not in rig.daemon.bindings)


def test_a_refused_register_leaves_it_holding_nothing(rig):
    first = ClaimServer("dup", "proj-a", kind="codex", arm=True).start()
    second = ClaimServer("dup", "proj-a", kind="codex", arm=True).start()
    try:
        assert wait_for(lambda: first.holder.state == "bound")
        assert second.holder.state == "waiting" and second.refusal
        assert call(second.socket_path, "claim")["line"] == "dup@proj-a is held by another session"
    finally:
        first.stop()
        second.stop()



def test_a_report_after_stop_writes_no_claim_file(rig):
    """s126.4: once `stop()` has removed the claim file and the socket, a late report
    (a supervisor finishing a register, voice's binding going offline or waiting, a
    reap landing) writes nothing. Before, each wrote back a dead `srv-` file that
    nothing sweeps."""
    srv = ClaimServer("late", "proj-a", kind="codex").start()
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE)
    try:
        srv.set_bound(slot=1, tier="chosen")
        assert srv.add_root(child.pid) is not None
        assert claimfile.read(srv.holder.key)["state"] == "bound"
        srv.stop()
        assert claimfile.read(srv.holder.key) is None
        for what, report in (("set_bound", lambda: srv.set_bound(slot=2, tier="chosen")),
                             ("set_offline", srv.set_offline),
                             ("set_waiting", srv.set_waiting),
                             ("drop_root", lambda: srv.drop_root(child.pid))):
            report()
            assert claimfile.read(srv.holder.key) is None, what
        # A spawn racing the stop (notify's outcomes loop sending as the service stops)
        # records no root: before, `add_root` wrote the file back as `bound` with the
        # child as a `tree` root that the reaper, a no-op by then, never dropped.
        roots = list(srv.holder.roots)
        root = srv.add_root(child.pid)
        assert claimfile.read(srv.holder.key) is None, \
            ("add_root", claimfile.read(srv.holder.key))
        assert root is None
        late = srv.spawn([sys.executable, "-c", "pass"])
        assert late.wait(timeout=30) == 0
        assert claimfile.read(srv.holder.key) is None, "spawn"
        assert srv.holder.roots == roots
    finally:
        child.communicate()


# --------------------------------------------------------------------------- s126.1

def line_of(srv) -> str:
    return call(srv.socket_path, "claim")["line"]


def test_a_binding_never_bound_says_so(rig, tmp_path):
    """s126.1: a claim the daemon has not bound yet says that, not that another session
    holds it: before any report, and through a daemon that is down at start."""
    srv = ClaimServer("fresh", "proj-a", kind="codex").start()
    down = ClaimServer("early", "proj-a", kind="codex", arm=True,
                       daemon_socket=tmp_path / "no-daemon.sock").start()
    try:
        assert line_of(srv) == "fresh@proj-a is not bound to comms yet"
        code, out = rig.cli(["send", "nobody", "hello"],
                            env={CLAIM_SOCKET_ENV: str(srv.socket_path)})
        assert code == 2
        assert "fresh@proj-a is not bound to comms yet — nothing was sent." in out, out
        assert down.holder.state == "waiting"
        assert line_of(down) == "early@proj-a is not bound to comms yet"
    finally:
        srv.stop()
        down.stop()


def test_a_refusal_that_names_no_holder_says_the_daemons_words(rig):
    """s126.1: a register refused for good (`unknown adapter kind`) names the refusal,
    from the arm's own register and from a holder that reports it."""
    arm = ClaimServer("odd", "proj-a", kind="no-such-kind", arm=True).start()
    held = ClaimServer("told", "proj-a", kind="codex").start()
    try:
        assert arm.holder.state == "waiting" and arm.refusal
        assert line_of(arm) == f"comms refused odd@proj-a: {arm.refusal}"
        assert arm.refusal.startswith("unknown adapter kind 'no-such-kind'")
        held.set_bound(slot=1, tier="chosen")
        held.set_waiting(refusal="unknown adapter kind 'x'")
        assert line_of(held) == "comms refused told@proj-a: unknown adapter kind 'x'"
    finally:
        arm.stop()
        held.stop()


def test_a_held_name_still_says_another_session_holds_it(rig):
    """s126.1 changes only the never-bound and the refused lines: a holder that drops its
    claim to `waiting` without the daemon's words (voice's link, after a retried
    refusal) says what it said before."""
    srv = ClaimServer("taken", "proj-a", kind="codex").start()
    try:
        srv.set_bound(slot=1, tier="chosen")
        srv.set_waiting()
        assert line_of(srv) == "taken@proj-a is held by another session"
        code, out = rig.cli(["send", "nobody", "hello"],
                            env={CLAIM_SOCKET_ENV: str(srv.socket_path)})
        assert code == 2
        assert "taken@proj-a is held by another session — nothing was sent." in out, out
    finally:
        srv.stop()


def test_a_claim_socket_outside_comms_runtime_directory_is_never_asked(
        rig, servers, tmp_path):
    """A claim socket from the environment must lie inside RUNTIME/comms: one stamped by
    another copy of comms, or by hand, is refused naming the variable and the directory,
    and it is not asked who this process is, though a live server answers there."""
    from moxi_comms import claimhold, paths

    operator, _ = servers
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stray = ClaimServer("stray", "proj-a", kind="codex",
                        socket_path=elsewhere / "c.sock").start()
    stray.set_bound(slot=3, tier="chosen")
    try:
        env = {CLAIM_SOCKET_ENV: str(stray.socket_path)}
        with pytest.raises(paths.OutsideRuntime) as caught:
            claimhold.socket_from_env(env)
        assert CLAIM_SOCKET_ENV in str(caught.value)
        assert str(paths.runtime_dir()) in str(caught.value)

        said: list[str] = []
        assert claimhold.whoami(env, say=said.append) == 2
        assert said and "outside comms' runtime directory" in said[0]
        assert "stray" not in " ".join(said)

        overlay = claimhold.claim_overlay(dict(env, MOXI_COMMS_NAME="stray",
                                               MOXI_COMMS_SCOPE="proj-a"))
        assert "MOXI_COMMS_NAME" not in overlay and "MOXI_COMMS_SCOPE" not in overlay

        # A send is refused the same way, naming the variable, and nothing is sent.
        code, out = rig.cli(["send", "main", "hello"], env=env)
        assert code == 2, out
        assert "outside comms' runtime directory" in out and CLAIM_SOCKET_ENV in out, out
        assert "Nothing was sent." in out and "stray" not in out, out

        # A socket inside the directory is the one comms asks.
        inside = {CLAIM_SOCKET_ENV: str(operator.socket_path)}
        assert claimhold.socket_from_env(inside) == operator.socket_path
        assert claimhold.socket_from_env({}) is None
    finally:
        stray.stop()
