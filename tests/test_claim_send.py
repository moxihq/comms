"""Sending as the claim: the send verb takes its identity from its session's claim
socket, not from the launch stamp, on both backends. Ordinary messaging through the
claim prints, exits, delivers and attributes as any ordinary send, and `comms whoami` names what the session holds."""

from __future__ import annotations

import pytest

from claim_kit import World, wait_for
from conftest import clock_reads_warm, wait_for_queue
from moxi_comms.client import DaemonClient, DaemonError

SEAT = "seat@proj-a"


@pytest.fixture
def world(rig, monkeypatch):
    clock_reads_warm(monkeypatch, "codex")
    w = World()
    yield w
    w.stop()


def as_claim(s):
    return {"MOXI_COMMS_CLAIM_SOCKET": s.socket}


def last(rig):
    return rig.gateway.history(rig.names_channel)[-1]


def test_a_send_inside_the_session_goes_out_as_the_claim_on_its_bound_slot(rig, world):
    s = world.real("seat", slot=3)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], env=as_claim(s))
    assert code == 0, out
    msg = last(rig)
    assert msg.author_display == SEAT
    assert msg.author_id == rig.slot_webhook_id(3)


def test_the_stamp_is_not_read_when_the_claim_socket_is_there(rig, world):
    s = world.real("seat", slot=3)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], name="impostor", slot=5,
                        env=as_claim(s))
    assert code == 0, out
    assert last(rig).author_display == SEAT


def test_a_process_outside_the_session_is_refused(rig, world):
    s = world.session("seat", slot=3)               # rooted in a fake, unrelated tree
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], env=as_claim(s))
    assert code == 2
    assert "not inside the session" in out and "nothing was sent" in out


def test_a_session_that_holds_nothing_says_why(rig, world):
    s = world.real("seat", slot=3)
    s.holder.claim, s.holder.state = None, "standby"
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], env=as_claim(s))
    assert code == 2
    assert "this session holds no comms identity — nothing was sent." in out
    s.holder.lost = SEAT                            # another session took its name
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], env=as_claim(s))
    assert code == 2
    assert f"{SEAT} is held by another session — nothing was sent." in out


def test_offline_with_the_name_bound_elsewhere_is_refused(rig, world):
    s = world.real("seat", slot=3)
    s.client.close()
    assert wait_for(lambda: s.holder.state == "offline")
    rig.shim(name="seat", scope="proj-a", slot=4)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], env=as_claim(s))
    assert code == 2 and "bound by another session" in out


def test_offline_with_the_name_unbound_posts_as_the_claim(rig, world):
    s = world.real("seat", slot=3)
    s.client.close()
    assert wait_for(lambda: s.holder.state == "offline")
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], env=as_claim(s))
    assert code == 0, out
    assert last(rig).author_id == rig.slot_webhook_id(3)


def test_offline_with_the_lookup_failing_posts_on_the_claims_slot(rig, world, monkeypatch):
    s = world.real("seat", slot=3)
    s.client.close()
    assert wait_for(lambda: s.holder.state == "offline")
    real = DaemonClient.query

    def failing(self, kind, **kw):
        if kind == "agents":
            raise DaemonError("down for this")
        return real(self, kind, **kw)

    monkeypatch.setattr(DaemonClient, "query", failing)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], env=as_claim(s))
    assert code == 0, out
    assert last(rig).author_id == rig.slot_webhook_id(3)


def test_a_dead_adapter_sends_by_its_claim_file_while_nobody_holds_the_name(rig, world):
    s = world.real("seat", slot=3)
    s.client.close()
    s.holder.close(remove_file=False)               # the shim died; Claude lives on
    assert wait_for(lambda: SEAT not in rig.daemon.bindings)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], env=as_claim(s))
    assert code == 0, out
    rig.shim(name="seat", scope="proj-a", slot=4)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "nobody", "hello"], env=as_claim(s))
    assert code == 2 and "bound by another session" in out


def test_a_dead_adapter_with_no_live_file_is_refused(rig, world):
    s = world.real("seat", slot=3)
    s.holder.close()
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], env=as_claim(s))
    assert code == 2 and "comms adapter is gone" in out


def test_a_stamp_is_refused_when_a_live_session_holds_its_name(rig, world):
    world.real("seat", slot=3)
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], name="seat", scope="proj-a",
                        stamp_only=True)
    assert code == 2 and "not inside the session that holds seat@proj-a" in out
    assert rig.gateway.history(rig.names_channel) == []


def test_a_stamp_is_refused_while_a_waiting_launch_names_it(rig, world):
    waiting = world.session("later", bind=False)
    waiting.holder.name_refused("later@proj-a")
    assert waiting.state == "waiting"
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hi"], name="later", scope="proj-a",
                        stamp_only=True)
    assert code == 2 and "not inside the session that holds later@proj-a" in out


def test_a_stamp_no_live_session_names_is_refused_too(rig):
    """There is no env path. A stamp is refused even when no live claim file names its
    identity, and the refusal names the
    identity the stamp claims. The binding is there; only a session holding it sends."""
    rig.shim(name="seat", scope="proj-a", slot=3)
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], name="seat", scope="proj-a",
                        stamp_only=True)
    assert code == 2, out
    assert out.strip() == (
        "this process has no comms claim socket, so it sends under no identity — nothing "
        "was sent. Its launch stamp names seat@proj-a, but a stamp is not a live session "
        "holding a name: a session sends as its identity through its own claim socket.")
    assert rig.gateway.history(rig.names_channel) == []


def test_the_operators_lane_is_refused_inside_a_session(rig, world):
    s = world.real("seat", slot=3)
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hi"],
                        env={**as_claim(s), "MOXI_COMMS_OPERATOR": rig.prov.operator_user_id})
    assert code == 2 and "operator's lane is its own terminal" in out
    code, out = rig.cli(["send", "--posture", "none", "nobody", "hi"],
                        env={"MOXI_COMMS_OPERATOR": rig.prov.operator_user_id})
    assert code == 2 and "inside an agent's session" in out


# --------------------------------------------------------------------------- replies

def _sent_id(out):
    return out.split("sent ", 1)[1].split()[0]


def test_a_reply_through_the_claim_goes_out_to_its_own_or_anothers_message(rig, world):
    s = world.real("seat", slot=3)
    code, out = rig.cli(["send", "--posture", "once", "--new-name", "peer", "before"], env=as_claim(s))
    assert code == 0, out
    mine = _sent_id(out)
    code, out = rig.cli(["reply", mine, "fine"], env=as_claim(s))     # its own message
    assert code == 0, out
    other = rig.post_from_slot(5, ["seat"], "from someone else", display="other@proj-a")
    code, out = rig.cli(["reply", other.id, "fine"], env=as_claim(s))  # another's
    assert code == 0, out


# --------------------------------------------------------------------------- whoami

def test_whoami_prints_the_claim_and_its_session(rig, world):
    """The first line, then `session <id>` when the claim records its launch
    session, and nothing else."""
    s = world.real("seat", slot=3)
    code, out = rig.cli(["whoami"], env=as_claim(s))
    assert code == 0 and out.splitlines() == ["seat@proj-a (bound, slot 3)"], out
    s.holder.launch_session = "0a1b2c3d-sess"
    code, out = rig.cli(["whoami"], env=as_claim(s))
    assert code == 0, out
    assert out.splitlines() == ["seat@proj-a (bound, slot 3)", "session 0a1b2c3d-sess"]


def test_whoami_outside_any_session(rig):
    code, out = rig.cli(["whoami"])
    assert code == 2 and "no claim socket" in out


def test_whois_and_the_cache_caller_read_the_claim(rig, world, monkeypatch):
    s = world.real("seat", slot=3)
    from moxi_comms import claimhold
    env = claimhold.claim_overlay({"MOXI_COMMS_CLAIM_SOCKET": s.socket, "MOXI_COMMS_NAME": "stale"})
    assert env["MOXI_COMMS_NAME"] == "seat" and env["MOXI_COMMS_SCOPE"] == "proj-a"
    assert env["MOXI_COMMS_SLOT"] == "3"
    s.holder.claim, s.holder.state = None, "standby"
    env = claimhold.claim_overlay({"MOXI_COMMS_CLAIM_SOCKET": s.socket, "MOXI_COMMS_NAME": "stale"})
    assert "MOXI_COMMS_NAME" not in env


def test_a_dead_adapters_file_admits_only_a_process_inside_its_session(rig, world):
    """The claim file of a session whose adapter died answers a sender only when the
    sender is inside that session: a live root that is not this process's ancestor
    admits nothing."""
    import subprocess
    import sys

    from moxi_comms.membership import DEFAULT_PROC, TREE, root_of
    from claim_kit import Session
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        s = Session(DEFAULT_PROC, "seat", slot=3, root=root_of(other.pid, TREE))
        world.sessions.append(s)
        s.bind()
        s.client.close()
        s.holder.close(remove_file=False)
        assert wait_for(lambda: SEAT not in rig.daemon.bindings)
        code, out = rig.cli(["send", "--posture", "none", "nobody", "hello"], env=as_claim(s))
        assert code == 2 and "comms adapter is gone" in out
    finally:
        other.kill()


# --------------------------------------------------------------------------- ordinary sends
# Ordinary messaging is no harder for the claim: bare-name sends between agents and sends
# through a claim print, exit, deliver and attribute as any ordinary send. The one visible difference the
# released-slot keep makes is which slot a `next-free` launch gets within SLOT_KEEP of
# another session's exit; that is asserted and nothing else changes.

def sent_line(out, to):
    first = out.splitlines()[0]
    mid = first.split()[1]
    assert first == f"sent {mid} to {to} in #names"
    return mid


def test_bare_name_sends_between_agents_are_unchanged(rig):
    """A bare name is the global identity, so the bare-name case is two global
    agents."""
    a = rig.shim(name="alice", slot=1)
    b = rig.shim(name="bob", slot=3)
    code, out = rig.cli(["send", "--posture", "once", "bob", "hello bob"], name="alice")
    assert code == 0 and out.count("\n") == 1, out
    mid = sent_line(out, "bob")
    wait_for_queue(b, 1)
    assert b.queue[0]["message_id"] == mid
    author = b.queue[0]["author"]
    assert (author["name"], author["tier"], author["attribution"]) == (
        "alice", "chosen", "verified")
    code, out = rig.cli(["reply", mid, "hi alice"], name="bob")
    assert code == 0
    assert out.splitlines()[0] == "replying to alice (chosen, verified) in #names"
    wait_for_queue(a, 1)


def test_sends_through_the_claim_print_and_deliver_as_stamped_sends_did(rig, world):
    a = world.real("alice", slot=1)
    b = rig.shim(name="bob", slot=3)                  # global: a bare `bob` reaches it
    code, out = rig.cli(["send", "--posture", "none", "bob", "hello"], env={"MOXI_COMMS_CLAIM_SOCKET": a.socket})
    assert code == 0 and out.count("\n") == 1, out
    sent_line(out, "bob")
    wait_for_queue(b, 1)
    author = b.queue[0]["author"]
    assert (author["name"], author["tier"], author["attribution"]) == (
        "alice@proj-a", "chosen", "verified")


def test_the_one_visible_difference_is_the_slot_a_next_free_launch_gets(rig, world):
    first = rig.shim(name="first", scope="proj-a", slot=1)
    first.stop()
    assert wait_for(lambda: "first@proj-a" not in rig.daemon.bindings)
    late = rig.shim(name="late", scope="proj-a")              # next-free
    assert late.slot_id != 1, "slot 1 waits for first@proj-a"
    back = rig.shim(name="first", scope="proj-a")             # its own kept slot
    assert back.slot_id == 1
    code, out = rig.cli(["send", "--posture", "none", "late@proj-a", "hi"], name="first", scope="proj-a")
    assert code == 0 and out.count("\n") == 1
    sent_line(out, "late@proj-a")
