"""The fast local stall signal.

A short timer armed at the injection site in the shim, saying locally that the
injection never visibly started: the adapter's own report.

Nothing in this file sleeps for a window. The timer takes its `timer_factory`, so a
test fires it by hand at the instant it chooses, and the shim is exercised on the
ordinary rig with its real injection path.
"""

from __future__ import annotations

import pytest
from conftest import wait_until

from moxi_comms import constants, cursors, daemon as daemon_mod, stall

import shim_drive


class ManualTimer:
    """A `threading.Timer` that fires when a test says so, and not a moment before."""

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class Timers:
    """Hands out ManualTimers and keeps them, so a test can fire the live one."""

    def __init__(self):
        self.made: list[ManualTimer] = []

    def __call__(self, interval, function):
        t = ManualTimer(interval, function)
        self.made.append(t)
        return t

    @property
    def live(self) -> ManualTimer:
        alive = [t for t in self.made if t.started and not t.cancelled]
        assert len(alive) == 1, f"expected one live timer, found {len(alive)}"
        return alive[0]

    def fire(self) -> None:
        self.live.function()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def timer(sink=None, **kw):
    said = sink if sink is not None else []
    timers = Timers()
    clock = Clock()
    st = stall.StallTimer(said.append, timer_factory=timers, clock=clock, **kw)
    return st, said, timers, clock


# --------------------------------------------------------------------------- the timer

def test_the_window_is_the_measured_one():
    """15s was measured: the shim's injection-to-turn-start ran 0.04-0.06s over twelve real
    turns, and a driven codex invocation's first in-band line came 0.23-1.16s after the
    spawn over eight. The window is thirteen times the worst of those, because the
    failure worth avoiding is a warning on a healthy turn.
    """
    assert constants.INJECT_STALL_WINDOW == 15.0
    assert stall.StallTimer(lambda _: None).window == constants.INJECT_STALL_WINDOW


def test_a_sign_of_life_disarms_it_and_hands_back_the_gap():
    st, said, timers, clock = timer()
    st.arm("planner@proj-a", "1400000000001")
    assert st.armed
    clock.advance(0.05)
    assert st.witness() == pytest.approx(0.05)
    assert st.armed is False
    assert timers.made[0].cancelled
    assert said == [], "a turn that started is not news"


def test_an_injection_nothing_answers_is_reported_once():
    st, said, timers, _ = timer()
    st.arm("planner@proj-a", "1400000000007")
    timers.fire()
    assert len(said) == 1
    line = said[0]
    assert "planner@proj-a" in line
    assert stall.STALL_REASON in line
    assert "1400000000007" in line
    assert "comms explain planner@proj-a" in line, "it points at the command that answers"
    assert "alarm" not in line, "no other detector to name"
    assert st.fired == 1
    assert st.armed is False


def test_it_says_it_once_and_then_stops():
    """One report per injection. A timer that repeats is a timer people filter."""
    st, said, timers, _ = timer()
    st.arm("planner@proj-a", "9")
    fired = timers.made[0]
    fired.function()
    fired.function()
    fired.function()
    assert len(said) == 1


def test_a_sign_of_life_with_nothing_armed_is_not_an_error():
    """A typed turn, a chatty child's tenth line. `witness` answers None and says
    nothing -- callers must be able to hand it every sign of life they get."""
    st, said, _, _ = timer()
    assert st.witness() is None
    assert said == []


def test_a_witness_that_lands_between_the_timer_and_the_lock_wins():
    """The timer thread has already been scheduled when the turn starts. It must not
    report a stall that was answered a microsecond earlier."""
    st, said, timers, _ = timer()
    st.arm("planner@proj-a", "9")
    pending = timers.made[0]
    assert st.witness() is not None
    pending.function()               # the thread that was already on its way
    assert said == []


def test_a_second_injection_replaces_the_first():
    st, said, timers, _ = timer()
    st.arm("planner@proj-a", "1")
    st.arm("planner@proj-a", "2")
    assert timers.made[0].cancelled
    timers.fire()
    assert len(said) == 1 and "through 2" in said[0]


def test_cancel_disarms_without_reporting():
    st, said, timers, _ = timer()
    st.arm("planner@proj-a", "1")
    st.cancel()
    timers.made[0].function()
    assert said == []


def test_the_gap_is_kept_so_the_window_can_be_re_measured():
    """`last_latency` is the measurement surface: the number was taken against a harness
    that ships a new version most weeks, so re-taking it must not need a rig."""
    st, _, _, clock = timer()
    assert st.last_latency is None
    st.arm("a@b", "1")
    clock.advance(0.042)
    st.witness()
    assert st.last_latency == pytest.approx(0.042)


def test_the_report_carries_no_body():
    """A status line is a log line: ids and lengths, never a digest."""
    st, said, timers, _ = timer()
    st.arm("planner@proj-a", "1400000000001")
    timers.fire()
    assert "==" not in said[0] and "[comms]" not in said[0]


def test_a_sink_that_throws_does_not_take_the_timer_thread_with_it():
    def angry(_text):
        raise RuntimeError("no")

    st = stall.StallTimer(angry, timer_factory=Timers())
    st.arm("a@b", "1")
    st._fire()                        # must not raise
    assert st.fired == 1


def test_the_two_detectors_have_two_names():
    """`comms explain`'s case has its name; this is a different fact and gets a
    different name.
    They differ by the word that differs: no turn END versus no turn START."""
    assert stall.STALL_REASON == "injected-but-the-turn-never-started"
    assert stall.STALL_REASON != daemon_mod.CASE_TURN_INCOMPLETE
    assert "never-started" in stall.STALL_REASON
    assert "never-completed" in daemon_mod.CASE_TURN_INCOMPLETE


# ----------------------------------------------------------------------------- the shim

def _deliver_one(rig, shim, text="answer me"):
    rig.post_from_slot(6, [shim.identity], text)
    wait_until(lambda: len(shim.queue) >= 1)
    return shim.queue[0]["message_id"]


def _manual(adapter):
    """Put a manual timer under an adapter's stall signal and return the handle."""
    timers = Timers()
    adapter.stall._timer_factory = timers
    return timers


def test_the_shim_arms_at_the_moment_it_injects(rig):
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    timers = _manual(b)
    mid = _deliver_one(rig, b)
    assert b.stall.armed is False, "nothing is armed before anything is injected"
    b.on_turn_boundary()
    assert b.digests, "the injector ran"
    assert b.stall.armed
    assert timers.live.interval == constants.INJECT_STALL_WINDOW
    timers.fire()
    assert len(b.statuses) == 1
    assert stall.STALL_REASON in b.statuses[0] and mid in b.statuses[0]


def test_a_turn_start_is_the_shims_sign_of_life(rig):
    """turns reports a delivered turn's start, so an injection that became a turn lands
    here and one that vanished never does."""
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    timers = _manual(b)
    _deliver_one(rig, b)
    b.on_turn_boundary()
    assert b.stall.armed
    shim_drive.turn_started(b)
    assert b.stall.armed is False
    timers.made[0].function()
    assert b.statuses == [], "a turn that started is not reported"


def test_a_typed_turn_disarms_nothing_because_nothing_was_armed(rig):
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    shim_drive.turn_started(b)
    assert b.statuses == []
    assert b.stall.last_latency is None


def test_a_turn_boundary_also_answers_it(rig):
    """The idle-flush path and the `--inject-file` double reach a boundary without ever
    passing through a turn's start, and a boundary proves the turn both ran and ended."""
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    timers = _manual(b)
    _deliver_one(rig, b)
    b.on_turn_boundary()
    assert b.stall.armed
    b.on_turn_boundary()              # the turn completed
    assert b.stall.armed is False
    assert cursors.read("reviewer@proj-a") is not None


def test_the_shim_report_goes_to_the_status_sink_and_nowhere_else(rig):
    """The whole argument: a detector armed at the injection site depends on no
    socket. Firing it must not touch the daemon."""
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    timers = _manual(b)
    mid = _deliver_one(rig, b)
    b.on_turn_boundary()
    wait_until(lambda: rig.daemon.injected_through.get("reviewer@proj-a") == mid)
    before = (dict(rig.daemon.injected_through), dict(rig.daemon.activity),
              rig.gateway.history(rig.names_channel))
    timers.fire()
    assert len(b.statuses) == 1
    assert (dict(rig.daemon.injected_through), dict(rig.daemon.activity),
            rig.gateway.history(rig.names_channel)) == before
    assert cursors.read("reviewer@proj-a") is None, "and it moves no cursor"


def test_a_shim_that_is_stopping_is_not_a_stalled_one(rig):
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    timers = _manual(b)
    _deliver_one(rig, b)
    b.on_turn_boundary()
    b.stop()
    # `stop()` says its own offline line, as it always has. What must
    # not appear is a stall report: a shim on its way out never stalled.
    assert b.stall.armed is False
    before = list(b.statuses)
    timers.made[0].function()
    assert b.statuses == before
    assert not any(stall.STALL_REASON in s for s in b.statuses)

