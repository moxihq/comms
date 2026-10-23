"""A daemon arm reads `bound` only while the connection its register was answered
on is open, and a drop at any point after the answer sends it back to register.

Every arm that registers through shared code is driven: the `ClaimServer`'s own arm
(the send-only console) and the harness
adapters' `connect_daemon` (codex and OpenCode). The drop is forced at each
point, not waited for:

* before the answer: the arm's connection is closed before the register goes out;
* between the answer and the arm taking the binding: the
  daemon is stopped, or the connection dropped, after the register is answered, and the
  register returns only once the reader thread has run the arm's close handler;
* after the binding: the daemon is stopped under a bound arm.

Each at the first register (the arm's start) and at a re-register after a daemon
restart. The arm must come back bound on a live connection, with the daemon holding its
binding. Before the fix the forced race left the arm saying `bound` over a dead
connection, its supervisor asleep, and the next daemon holding nothing for it.
"""

from __future__ import annotations

import itertools

import pytest
from claim_kit import RegisterDrop
from conftest import clock_reads_warm, wait_until

from moxi_comms import claimfile, claimhold, codex_threads
from moxi_comms.adapters import harness as harness_mod
from moxi_comms.adapters import opencode_roster
from moxi_comms.adapters.claude_shim import OFFLINE_MESSAGE
from moxi_comms.adapters.codex import CodexAdapter
from moxi_comms.adapters.codex_roster import CodexAgent
from moxi_comms.adapters.opencode import OpenCodeAdapter
from moxi_comms.adapters.opencode_roster import OpenCodeAgent
from moxi_comms.claimhold import ClaimServer
from fake_turns import FakeSession, launcher
from fake_opencode import FakeOpenCode
from test_console_identity import provision_console

FAST = 0.05
PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _warm(monkeypatch):
    clock_reads_warm(monkeypatch, "codex", "opencode")


# --------------------------------------------------------------------------- the arms

class ServerArm:
    """A `ClaimServer` with its own daemon arm."""

    module = claimhold

    def __init__(self, rig, name, scope, kind):
        self.rig = rig
        self.qualified = f"{name}@{scope}"
        self.srv = ClaimServer(name, scope, kind=kind, arm=True,
                               backoff=itertools.repeat(FAST))

    def start(self):
        self.srv.start()

    def bound(self) -> bool:
        return self.srv.holder.state == "bound"

    def live(self) -> bool:
        c = self.srv._client
        return c is not None and c.connected

    def stop(self):
        self.srv.stop()


class HarnessArm:
    """A harness adapter (`connect_daemon`, shared by the three)."""

    module = harness_mod

    def __init__(self, rig, adapter):
        self.rig = rig
        self.adapter = adapter
        self.qualified = adapter.agent.qualified
        self.statuses: list = []
        adapter.status_sink = self.statuses.append

    def start(self):
        self.adapter.start()

    def bound(self) -> bool:
        a = self.adapter
        return not a.offline and a.claims.state == "bound"

    def live(self) -> bool:
        c = self.adapter._daemon
        return c is not None and c.connected

    def stop(self):
        self.adapter.stop()


def codex_arm(rig, tmp_path, request):
    agent = CodexAgent(qualified="planner@proj-a", name="planner", scope="proj-a", slot=3,
                       cwd=str(tmp_path), thread_file=codex_threads.path("planner@proj-a"),
                       directors=[])
    return HarnessArm(rig, CodexAdapter(
        agent, launch=launcher(FakeSession()), autoreconnect=True,
        backoff=itertools.repeat(FAST), clock=rig.clock))


class _Handle:
    def __init__(self, fake):
        self.fake = fake
        self.alive = True
        self.returncode = None

    def wait_listening(self, timeout=None):
        return self.fake.url

    def stop(self):
        pass


def opencode_arm(rig, tmp_path, request):
    fake = FakeOpenCode(password=PASSWORD, default_model="opencode/minimax-m3.1-flash-free",
                        heartbeat=0.5).start()
    request.addfinalizer(fake.stop)
    agent = OpenCodeAgent(qualified="coder@proj-a", name="coder", scope="proj-a", slot=5,
                          cwd=str(tmp_path),
                          session_file=opencode_roster.session_path("coder@proj-a"),
                          models=["opencode/minimax-m3.1-flash-free"], directors=[])
    adapter = OpenCodeAdapter(agent, spawn=False, server=_Handle(fake), password=PASSWORD,
                              autoreconnect=True, backoff=itertools.repeat(FAST),
                              clock=rig.clock)
    fake.on_log = adapter.on_log_line
    return HarnessArm(rig, adapter)


def send_only_arm(rig, tmp_path, request):
    provision_console(rig)
    return ServerArm(rig, "console", "acme", "send-only")


ARMS = {"send-only": send_only_arm, "codex": codex_arm, "opencode": opencode_arm}


@pytest.fixture(params=list(ARMS))
def arm(request, rig, tmp_path, monkeypatch):
    a = ARMS[request.param](rig, tmp_path, request)
    a.kind = request.param
    a.drop = RegisterDrop(monkeypatch, a.module, rig)
    yield a
    a.stop()
    a.drop.stop_all()


def holds(arm) -> bool:
    """The rule's good end: the daemon holds the arm's binding, the arm reads bound, and
    its connection is open."""
    return arm.qualified in arm.rig.daemon.bindings and arm.bound() and arm.live()


def claim_key(arm) -> str:
    """The key of the arm's claim file."""
    if isinstance(arm, ServerArm):
        return arm.srv.holder.key
    return arm.adapter.claims.key


def never_bound_over_a_dead_connection(arm):
    assert not (arm.bound() and not arm.live()), (arm.kind, "bound over a dead connection")


# --------------------------------------------------------------------------- the rule

@pytest.mark.parametrize("point", ["after_answer_restart", "after_answer_drop"])
def test_a_drop_after_the_first_registers_answer_sends_the_arm_back_to_register(arm, point):
    """The arm's start: its first register is answered and the connection drops before
    the arm takes the binding. The arm does not read bound, and it binds once the daemon
    is back (a harness adapter's launch goes on, with the offline line, as after any
    later drop)."""
    arm.drop.arm(point)
    arm.start()
    assert arm.drop.fired.is_set(), arm.drop.unanswered
    if point == "after_answer_restart":
        assert not arm.bound(), (arm.kind, "bound with no daemon up")
    # With the daemon up (a dropped connection) the supervisor may already have bound
    # again, on a new connection: bound is right only on a live one.
    never_bound_over_a_dead_connection(arm)
    if isinstance(arm, HarnessArm):
        assert arm.statuses[:1] == [OFFLINE_MESSAGE], arm.statuses
    if point == "after_answer_restart":
        arm.drop.fresh_daemon()
    assert wait_until(lambda: holds(arm), timeout=5.0), \
        (arm.kind, arm.bound(), arm.live(), list(arm.rig.daemon.bindings))


@pytest.mark.parametrize("point", ["before_answer", "after_answer_restart",
                                   "after_answer_drop"])
def test_a_drop_at_a_reregister_after_a_restart_sends_the_arm_back_to_register(arm, point):
    """A bound arm, a daemon restart, and the drop forced at its re-register: before the
    answer, or after it and before the arm takes the binding. It binds on the daemon
    that is up."""
    arm.start()
    assert wait_until(lambda: holds(arm), timeout=5.0), arm.kind
    arm.rig.daemon.stop()
    assert wait_until(lambda: not arm.bound()), arm.kind
    arm.drop.arm(point)
    arm.drop.fresh_daemon()
    assert arm.drop.fired.wait(5.0), (arm.kind, arm.drop.unanswered)
    if point == "after_answer_restart":
        never_bound_over_a_dead_connection(arm)
        arm.drop.fresh_daemon()
    assert wait_until(lambda: holds(arm), timeout=5.0), \
        (arm.kind, point, arm.bound(), arm.live(), list(arm.rig.daemon.bindings))
    never_bound_over_a_dead_connection(arm)


def test_a_stop_under_an_answered_register_leaves_nothing_bound(arm):
    """The arm's owner stops it while its supervisor re-registers (a service
    stopped during a daemon outage), and the register is answered just as `stop()`
    runs. The arm declines the bind, as the Claude shim does: the daemon holds nothing
    for it and the claim file `stop()` removed stays removed. Otherwise the arm would take the
    binding, which the daemon holds until the process exits, and write the file back
    as `bound` after `stop()` removed it."""
    arm.start()
    assert wait_until(lambda: holds(arm), timeout=5.0), arm.kind
    key = claim_key(arm)
    assert claimfile.read(key) is not None
    arm.rig.daemon.stop()
    assert wait_until(lambda: not arm.bound()), arm.kind
    arm.drop.arm("after_answer_stop", on_answer=arm.stop)
    arm.drop.fresh_daemon()
    assert arm.drop.fired.wait(5.0), (arm.kind, arm.drop.unanswered)
    assert wait_until(lambda: arm.qualified not in arm.rig.daemon.bindings,
                      timeout=5.0), (arm.kind, "the daemon still holds the binding")
    assert not arm.bound() and not arm.live(), arm.kind
    assert claimfile.read(key) is None, (arm.kind, claimfile.read(key))


def test_a_plain_stop_leaves_no_claim_file(arm):
    """A bound arm stopped with no register in flight: the claim file `stop()` removed
    stays removed, even when a harness adapter's reader thread runs its close handler
    after `claims.close()` has unlinked the file, where `went_offline` would write it
    back as `offline`. The `ClaimServer` arms are here for parity."""
    arm.start()
    assert wait_until(lambda: holds(arm), timeout=5.0), arm.kind
    key = claim_key(arm)
    assert claimfile.read(key) is not None
    arm.stop()
    assert claimfile.read(key) is None, arm.kind
    # The close handler runs on the reader thread, after `stop()` returns: give it the
    # time to write, and the file must not come back.
    assert not wait_until(lambda: claimfile.read(key) is not None, timeout=0.5), \
        (arm.kind, claimfile.read(key))


def test_a_drop_after_the_binding_sends_the_arm_back_to_register(arm):
    """The daemon stops under a bound arm: it reads offline, and binds on the next."""
    arm.start()
    assert wait_until(lambda: holds(arm), timeout=5.0), arm.kind
    arm.rig.daemon.stop()
    assert wait_until(lambda: not arm.bound()), arm.kind
    arm.drop.fresh_daemon()
    assert wait_until(lambda: holds(arm), timeout=5.0), arm.kind


def test_a_harness_launch_whose_daemon_never_answered_still_fails(rig, tmp_path, request,
                                                                   monkeypatch):
    """Unchanged: a register the daemon never answered is the launch's failure, as a
    daemon down at launch is. Only a drop after the answer is tolerated at start."""
    from moxi_comms.client import DaemonOffline
    a = codex_arm(rig, tmp_path, request)
    drop = RegisterDrop(monkeypatch, harness_mod, rig)
    drop.arm("before_answer")
    try:
        with pytest.raises(DaemonOffline) as raised:
            a.start()
        assert not isinstance(raised.value, harness_mod.DroppedAfterAnswer)
    finally:
        a.stop()


def test_without_autoreconnect_a_harness_launch_dropped_after_the_answer_fails(
        rig, tmp_path, request, monkeypatch):
    """With no supervisor nothing would register again, so the launch says so."""
    a = codex_arm(rig, tmp_path, request)
    a.adapter.autoreconnect = False
    drop = RegisterDrop(monkeypatch, harness_mod, rig)
    drop.arm("after_answer_drop")
    try:
        with pytest.raises(harness_mod.DroppedAfterAnswer):
            a.start()
        assert a.adapter.offline is True and a.adapter._daemon is None
    finally:
        a.stop()
