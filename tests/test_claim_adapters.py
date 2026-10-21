"""The adapters as their sessions' claim holders: a shim respawned inside its live
session comes back holding its recorded claim, a session never inherits its launcher's claim socket, and the codex app-server is
stamped with its adapter's own. What test_baton_shim and test_baton_harness covered of
the claim system, without the pass (design §6.7.3)."""

from __future__ import annotations

from claim_kit import FakeProc, wait_for
from moxi_comms import claimfile, claimhold
from moxi_comms.adapters import session
from moxi_comms.adapters.codex import CodexAdapter
from moxi_comms.adapters.codex_roster import CodexAgent
from moxi_comms.claimhold import CLAIM_SOCKET_ENV
from moxi_comms.membership import TREE, Root
from fake_turns import FakeSession, launcher

SEAT = "seat@proj-a"


# --------------------------------------------------------------------------- the Claude shim

def test_a_respawned_shim_comes_back_holding_its_recorded_claim(rig):
    """A shim respawned inside its still-live Claude session reads the session's claim
    file and registers what it records, whatever its own arguments say."""
    proc = FakeProc()
    root = proc.spawn()
    roots = [Root(root, f"s{root}", TREE)]
    first = rig.shim(name="seat", scope="proj-a", slot=3, claim_key="sess-r",
                     claim_roots=roots, claim_proc=proc)
    assert first.identity == SEAT and rig.daemon.bindings[SEAT].slot_id == 3
    first.stop()                                  # the file stays; the root still lives
    assert wait_for(lambda: SEAT not in rig.daemon.bindings)
    assert claimfile.read("sess-r")["claim"]["qualified"] == SEAT
    again = rig.shim(scope="proj-a", claim_key="sess-r", claim_roots=roots,
                     claim_proc=proc)             # no name, no slot of its own
    assert again.identity == SEAT, "the recorded claim, not the respawn's arguments"
    assert again.claims.state == "bound" and again.claims.claim["qualified"] == SEAT
    assert rig.daemon.bindings[SEAT].slot_id == 3
    assert claimfile.read("sess-r")["state"] == "bound"


def test_a_session_never_inherits_its_launchers_claim_socket():
    env = session.stamped_env({CLAIM_SOCKET_ENV: "/parent.sock", "PATH": "/bin"}, name="a")
    assert CLAIM_SOCKET_ENV not in env
    assert env["MOXI_COMMS_NAME"] == "a" and env["PATH"] == "/bin"


# --------------------------------------------------------------------------- codex

def test_the_codex_adapter_stamps_its_own_claim_socket_on_the_app_server(rig, tmp_path):
    codex = CodexAdapter(CodexAgent(qualified="cx@proj-a", name="cx", scope="proj-a", slot=4,
                                    cwd=str(tmp_path), thread_file=tmp_path / "cx.json"),
                         launch=launcher(FakeSession()), autoreconnect=False)
    try:
        codex.start()
        env = codex._launch.calls[0]["env"]
        assert env[CLAIM_SOCKET_ENV] == str(codex.claims.socket_path)
    finally:
        codex.stop()
