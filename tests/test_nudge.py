"""Fanout layer 2: the nudge.

Layer 1 is in-process and layer 3 is the slow poll; both live in `local_gateway.py` and
are tested there. This is the middle one: a CLI process writes the record itself,
so the daemon learns of the row from a fire-and-forget `{"op": "nudge"}` frame on the
socket it already owns, and the poll is only the safety net underneath it.

Every test that measures the nudge posts through a SECOND `LocalGateway` on the same
file (`cli_gateway`), because that is what a CLI process is. Posting through the rig's
own instance would fan out in-process and the nudge would be measuring layer 1.
"""

from __future__ import annotations

import time

import pytest
from conftest import wait_for_queue, wait_until

from moxi_comms.local_gateway import LocalGateway

from moxi_comms import cli

POLL = 0.5              # the default `poll_interval`, and slow on purpose here


@pytest.fixture
def cli_gateway(rig):
    """The CLI as its own process: a second gateway on the same `record.db`.

    Its own pump never runs (nothing subscribes to it), and it resolves the rig's minted
    webhook tokens out of the box's 0600 store, which is where provision wrote them
    -- so this really is what a second process sees.
    """
    gateway = LocalGateway(clock=rig.clock, poll_interval=3600)
    yield gateway
    gateway.close()


@pytest.fixture
def notified(monkeypatch):
    """Every op the CLI hands to `DaemonClient.notify`, in order, delivered as usual."""
    ops: list = []
    real = cli.DaemonClient.notify

    def spy(self, op, **kwargs):
        ops.append(op)
        return real(self, op, **kwargs)

    monkeypatch.setattr(cli.DaemonClient, "notify", spy)
    return ops


class Frames:
    """A `LineConn` that only records what the daemon wrote to it."""

    def __init__(self):
        self.sent: list = []

    def send(self, frame):
        self.sent.append(frame)


def quiet_pump(gateway, interval: float = 5.0) -> None:
    """Put the rig's 20 ms pump on a long wait, and start that wait NOW.

    The nudge sets the pump's wake event, so nudging once here makes it fan out, clear
    the event and re-enter the wait at the new interval. Without the realignment a pump
    pass could land inside the measurement by luck rather than by nudge.
    """
    gateway.poll_interval = interval
    gateway.nudge()
    time.sleep(0.05)


def a_pair(rig):
    return rig.shim(slot=1, name="planner", scope="proj-a"), \
        rig.shim(slot=2, name="reviewer", scope="proj-a")


# --------------------------------------------------------------------------- the nudge

@pytest.mark.parametrize("backend", ["local"], indirect=True)
def test_a_nudge_delivers_a_cli_post_within_50ms(rig, cli_gateway, notified):
    """A nudge delivers within 50 ms. The pump is parked on a 5 s wait, so
    nothing but the frame can account for the delivery."""
    planner, reviewer = a_pair(rig)
    quiet_pump(rig.gateway)

    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "the nudge carries this"],
                        slot=planner.slot_id, name="planner", scope="proj-a",
                        env={"MOXI_COMMS_GATEWAY": "local"}, gateway=cli_gateway)
    t0 = time.monotonic()
    assert code == 0 and "sent" in out
    assert "nudge" in notified, "the local backend's CLI tells the daemon itself"

    assert wait_until(lambda: len(reviewer.queue) >= 1, timeout=0.05), \
        "the daemon was nudged and the row was committed; nothing else had to happen"
    assert time.monotonic() - t0 < 0.05
    assert reviewer.queue[0]["text"] == "the nudge carries this"


@pytest.mark.parametrize("backend", ["local"], indirect=True)
def test_a_lost_nudge_is_delivered_by_the_poll(rig, cli_gateway, monkeypatch):
    """A lost nudge still delivers within `poll_interval`. The frame never reaches the
    socket (an old daemon, a CLI naming the fallback socket); the pump is the net and the
    message is late, never lost."""
    real = cli.DaemonClient.notify
    monkeypatch.setattr(cli.DaemonClient, "notify",
                        lambda self, op, **kw: None if op == "nudge"
                        else real(self, op, **kw))
    planner, reviewer = a_pair(rig)
    quiet_pump(rig.gateway, POLL)

    code, _ = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "the poll carries this"],
                      slot=planner.slot_id, name="planner", scope="proj-a",
                      env={"MOXI_COMMS_GATEWAY": "local"}, gateway=cli_gateway)
    t0 = time.monotonic()
    assert code == 0
    assert not wait_until(lambda: len(reviewer.queue) >= 1, timeout=0.05), \
        "with the nudge lost there is nothing to deliver it inside the nudge's window"

    assert wait_until(lambda: len(reviewer.queue) >= 1, timeout=3.0)
    assert time.monotonic() - t0 < 2 * POLL, "one poll interval, plus room for a loaded box"
    assert reviewer.queue[0]["text"] == "the poll carries this"


@pytest.mark.parametrize("backend", ["local"], indirect=True)
def test_the_op_wakes_the_local_gateway(rig, monkeypatch):
    """The sixth op's whole body: `self.gateway.nudge()` when the gateway has one."""
    woken: list = []
    monkeypatch.setattr(rig.gateway, "nudge", lambda: woken.append(1))
    conn = Frames()
    rig.daemon._handle(conn, {"op": "nudge"})
    assert woken == [1]
    assert conn.sent == [], "fire-and-forget: a reply would arrive at a closed socket"


# ------------------------------------------------------------------- the old daemon

def test_nudge_is_a_no_op_on_a_gateway_without_one_and_never_replies(rig):
    """The mock has no `nudge` (nor does Discord): the op is taken and does nothing."""
    conn = Frames()
    rig.daemon._handle(conn, {"id": 11, "op": "nudge"})
    rig.daemon._handle(conn, {"op": "nudge"})
    assert conn.sent == []


def test_an_old_daemon_answers_unknown_op_and_is_otherwise_unharmed(rig):
    """Accepted: a daemon built before this op answers `unknown op` to a frame
    nobody waits for. The answer is the ordinary one and the connection lives on."""
    conn = Frames()
    rig.daemon._handle(conn, {"id": 12, "op": "nudge-the-daemon"})
    assert conn.sent == [{"id": 12, "ok": False,
                          "error": "unknown op 'nudge-the-daemon'"}]

    client = cli.DaemonClient(rig.daemon.socket_path).connect()
    try:
        client.notify("nudge-the-daemon")       # what an old daemon makes of a nudge
        assert client.request("ping")["ok"] is True
    finally:
        client.close()


# --------------------------------------------------------------------- the other two

def test_the_cli_on_the_mock_backend_sends_no_nudge(rig, notified):
    """The mock fans out in-process, so the frame would buy nothing. The backend is the
    seam's answer here, not the gateway object's: this rig hands the CLI a gateway, and
    the selection chain answers mock because the environment names it (the default
    would be local)."""
    planner, reviewer = a_pair(rig)
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "no nudge for the mock"],
                        slot=planner.slot_id, name="planner", scope="proj-a",
                        env={"MOXI_COMMS_GATEWAY": "mock"})
    assert code == 0 and "sent" in out
    assert "nudge" not in notified
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["text"] == "no nudge for the mock"
