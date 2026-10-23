"""Slot keeps across restarts, the shim's slot fallback, the
supervisor that ends only on purpose, the refused-register line, and the send path that
follows the binding.

On both backends (`BOTH_BACKENDS` in conftest). Every daemon here is a throwaway on the
rig's temporary roots: its socket, its STATE (so its keep mark, `paths.slot_keeps()`) and
its injected clock.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import sys
import threading

import pytest
from conftest import FakeClock, wait_until

from moxi_comms import constants
from moxi_comms import daemon as daemon_mod
from moxi_comms.adapters import claude_shim
from moxi_comms.adapters.claude_shim import (REFUSED_PREFIX, REFUSED_RETRYING,
                                             REFUSED_TERMINAL, REFUSED_WAITING, SLOT_MOVED,
                                             SUPERVISOR_ERROR)
from moxi_comms.client import DaemonClient, DaemonError, DaemonOffline
from moxi_comms.daemon import Daemon
from moxi_comms.paths import daemon_socket, slot_keeps

WINDOW = constants.SLOT_KEEP


# ------------------------------------------- the session's start, through the inject seam

class Gate:
    """A backoff generator the test steps one retry at a time.

    `_supervise` waits `next(delays)` between attempts, so an iterator that BLOCKS in
    `__next__` until the test says otherwise turns an unbounded retry loop into
    something a test can single-step. Paired with `gated()` it also makes "the loop
    continues on the CURRENT backoff, re-seeded only on success" an assertion: a
    re-seed is a new `Gate` object.
    """

    def __init__(self):
        self.waits = 0
        self._go = threading.Event()

    def __iter__(self):
        return self

    def __next__(self) -> float:
        self.waits += 1
        self._go.wait(5.0)      # a stopped supervisor must not hang the test either
        self._go.clear()
        return 0.0

    def step(self) -> None:
        self._go.set()


def gated(monkeypatch) -> list:
    """Replace the backoff FACTORY. Returns the list of generators it has handed out."""
    seeds: list = []

    def factory(*args, **kwargs):
        seeds.append(Gate())
        return seeds[-1]

    monkeypatch.setattr(claude_shim, "backoff_delays", factory)
    return seeds


class Launch:
    def __init__(self, shim, thread, box):
        self.shim, self.thread, self._box = shim, thread, box

    @property
    def code(self):
        """The exit code, or None while the session is still running."""
        return self._box.get("code")

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


@contextlib.contextmanager
def launched(rig, **shim_kwargs):
    """A session's start, as its adapter makes it: a start that tolerates a daemon that is down (`start(tolerate_offline=True)`), served
    until the block ends. A refused first bind is `EXIT_REFUSED` with the one line."""
    shim_kwargs.setdefault("autoreconnect", True)
    shim_kwargs.setdefault("backoff", itertools.repeat(0.02))
    shim = rig.shim(start=False, **shim_kwargs)
    done = threading.Event()
    box: dict = {}

    def serve():
        try:
            shim.start(tolerate_offline=True)
            done.wait(10.0)
            box["code"] = 0
        except DaemonError as e:
            print(f"{REFUSED_PREFIX}: {e}", file=sys.stderr)
            box["code"] = claude_shim.EXIT_REFUSED
        finally:
            shim.stop()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield Launch(shim, thread, box)
    finally:
        done.set()
        thread.join(timeout=5.0)


@pytest.fixture(autouse=True)
def rooted_in_tmp(tmp_path):
    """Every path this suite can reach stays under the test's roots, before the test and
    after it. Never call `monkeypatch.undo()` here: the rig's `env` fixture shares the
    one `monkeypatch`, so an undo puts the operator's real roots back mid-test and the
    next shim registers with the LIVE daemon. Scoped patches use
    `pytest.MonkeyPatch.context()` instead."""
    def check():
        assert str(slot_keeps()).startswith(str(tmp_path)), slot_keeps()
        assert "/run/user/" not in str(daemon_socket()), daemon_socket()
    check()
    yield
    check()


# --------------------------------------------------------------------------- helpers

@pytest.fixture
def boot(rig):
    """Stop the rig's daemon (cleanly, unless the test crashed it) and start another on
    the same socket, record, STATE and clock. Every daemon made here is stopped at the
    end, so a rebooted test leaks no listener into the next."""
    made: list = []

    def again(*, stop=True, **kw):
        if stop:
            rig.daemon.stop()
        kw.setdefault("clock", rig.clock)
        fresh = Daemon(rig.gateway, start_trim_thread=False, **kw).start()
        rig.daemon = fresh
        made.append(fresh)
        return fresh

    yield again
    for d in made:
        d.stop()


def crash(rig) -> None:
    """The daemon dies without `stop()`: the mark stays exactly as last written. A dead
    process writes nothing, so writing is switched off before its connections close."""
    rig.daemon._keeps_armed = False
    rig.daemon.stop()


def mark() -> list:
    return json.loads(slot_keeps().read_text())


def live_in(entries) -> dict:
    return {e["slot"]: e["qualified"] for e in entries if e["kind"] == "live"}


def kept_in(entries) -> dict:
    return {e["slot"]: e["qualified"] for e in entries if e["kind"] == "keep"}


def register(**kw):
    """One raw register, the way the daemon sees it. Returns (client, response)."""
    c = DaemonClient(daemon_socket()).connect()
    try:
        return c, c.register(**kw)
    except BaseException:
        c.close()
        raise


def status_of(shim) -> dict:
    """What the shim records of its supervisor, its slot and its last refusal."""
    thread = shim._supervisor
    supervisor = "none" if thread is None else "running" if thread.is_alive() else "ended"
    return {"supervisor": supervisor, "slot": shim.slot_id,
            "ended_because": shim.ended_because if supervisor == "ended" else None,
            "state": shim._last_reported_state, "last_refusal": shim.last_refusal,
            "slot_asked": shim.slot_asked, "slot_refused_by": shim.slot_refused_by}


def keep_lines(caplog) -> list:
    return [r.getMessage() for r in caplog.records if "slot keep" in r.getMessage()]


def gone(rig, qualified) -> bool:
    return wait_until(lambda: qualified not in rig.daemon.bindings)


# ----------------------------------------------------------------- restarts

def test_the_restart_race_a_returning_pinned_seat_gets_its_slot_back(rig, boot):
    """The restart race, replayed: a `next-free` binding registers first after the restart. Without
    keeps it would take slot 1, the pinned seat's slot."""
    rig.shim(slot=1, name="captain", scope="proj-b")
    scout = rig.shim(name="scout", global_=True)
    assert scout.slot_id == 2
    boot()
    assert rig.shim(name="scout", global_=True).slot_id == 2, "its own keep, not slot 1"
    assert rig.shim(slot=1, name="captain", scope="proj-b").slot_id == 1
    assert rig.daemon.keeps == {}, "both keeps claimed"


def test_a_double_restart_inside_the_window_carries_the_unclaimed_keep(rig, boot):
    rig.shim(slot=1, name="a", scope="proj-a")
    rig.shim(name="b", scope="proj-a")                        # next-free: 2
    second = boot()
    boot_2 = rig.clock()
    assert rig.shim(name="b", scope="proj-a").slot_id == 2    # only b rebinds
    assert kept_in(mark()) == {1: "a@proj-a"} and live_in(mark()) == {2: "b@proj-a"}
    rig.clock.advance(2)
    third = boot()
    assert second is not third
    keep = rig.daemon.keeps[1]
    assert keep.qualified == "a@proj-a" and keep.carried
    assert keep.expires == boot_2 + WINDOW, "the second daemon's boot plus SLOT_KEEP"
    assert rig.shim(name="c", scope="proj-a").slot_id == 3, "skips 1 (a) and 2 (b)"
    assert rig.shim(slot=1, name="a", scope="proj-a").slot_id == 1


def test_a_crash_and_the_5s_auto_restart_keep_what_the_mark_last_said(rig, boot):
    rig.shim(slot=1, name="a", scope="proj-a")
    rig.shim(name="b", scope="proj-a")                        # 2
    crash(rig)
    assert live_in(mark()) == {1: "a@proj-a", 2: "b@proj-a"}, "as at the moment of death"
    rig.clock.advance(5)
    boot(stop=False)
    boot_2 = rig.clock()
    assert rig.shim(name="c", scope="proj-a").slot_id == 3, "a returns after c: 1 skipped"
    assert rig.shim(slot=1, name="a", scope="proj-a").slot_id == 1

    # ...and a crash landing inside an earlier keep window: b never came back to the
    # second daemon, so its keep is carried with its ORIGINAL expiry.
    rig.clock.advance(3)
    crash(rig)
    rig.clock.advance(5)
    boot(stop=False)
    keep = rig.daemon.keeps[2]
    assert (keep.qualified, keep.carried) == ("b@proj-a", True)
    assert keep.expires == boot_2 + WINDOW
    assert rig.daemon.keeps[1].expires == rig.clock() + WINDOW, "a was live: a new window"


# ----------------------------------------------------------------- the pick

def test_next_free_skips_kept_slots_and_gives_one_up_only_when_nothing_else_is_free(
        rig, boot, caplog):
    rig.shim(slot=1, name="a", scope="proj-a")
    rig.shim(slot=2, name="b", scope="proj-a")
    boot()
    assert rig.shim(name="x", scope="proj-a").slot_id == 3, "1 and 2 are kept"

    for n in range(4, 7):
        rig.shim(slot=n, name=f"n{n}", scope="proj-a")
    caplog.set_level(logging.INFO, logger="comms.daemon")
    caplog.clear()
    giver = rig.shim(name="y", scope="proj-a")
    assert giver.slot_id == 1, "only kept slots are free: the lowest is given up"
    assert keep_lines(caplog) == [
        "slot keep for a@proj-a on slot 1 ended: given up to y@proj-a (no other slot free)"]
    assert 1 not in rig.daemon.keeps and 2 in rig.daemon.keeps

    # A register that reaches step 3 and is then refused for its NAME changes nothing.
    caplog.clear()
    with pytest.raises(DaemonError, match="already bound"):
        register(name="y", scope="proj-a")
    assert rig.daemon.keeps[2].qualified == "b@proj-a", "the keep is left in place"
    assert keep_lines(caplog) == [], "and no keep line is written"


def test_after_the_window_the_pick_is_plain_next_free_and_the_lapse_is_one_line(
        rig, boot, caplog):
    rig.shim(slot=1, name="a", scope="proj-a")
    rig.shim(slot=2, name="b", scope="proj-a")
    boot()
    rig.clock.advance(WINDOW + 1)
    caplog.set_level(logging.INFO, logger="comms.daemon")
    assert rig.shim(name="x", scope="proj-a").slot_id == 1, "exactly the plain next-free pick"
    assert keep_lines(caplog) == ["slot keep(s) lapsed unclaimed: 1 a@proj-a, 2 b@proj-a"]
    assert rig.daemon.keeps == {} and kept_in(mark()) == {}


def test_a_numbered_register_takes_a_kept_slot_and_the_keep_ends_as_taken(
        rig, boot, caplog):
    rig.shim(slot=1, name="a", scope="proj-a")
    boot()
    caplog.set_level(logging.INFO, logger="comms.daemon")
    assert rig.shim(slot=1, name="z", scope="proj-a").slot_id == 1, "a keep refuses nobody"
    assert keep_lines(caplog) == ["slot keep for a@proj-a on slot 1 ended: taken by z@proj-a"]
    assert rig.daemon.keeps == {}
    assert live_in(mark()) == {1: "z@proj-a"} and kept_in(mark()) == {}


def test_a_missing_mark_gives_no_keeps_and_says_so_once(rig, boot, caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="comms.daemon")
    boot(keeps_path=tmp_path / "absent.json")
    assert rig.daemon.keeps == {}
    assert len([r for r in caplog.records if "no mark at" in r.getMessage()]) == 1
    assert rig.shim(name="x", scope="proj-a").slot_id == 1


def test_an_unparseable_or_wrong_shaped_mark_gives_no_keeps(rig, boot, caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="comms.daemon")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    boot(keeps_path=bad)
    assert rig.daemon.keeps == {}
    assert len([r for r in caplog.records if "does not parse" in r.getMessage()]) == 1
    assert rig.shim(name="x", scope="proj-a").slot_id == 1
    shaped = tmp_path / "shaped.json"
    shaped.write_text('{"kind": "live", "slot": 1, "qualified": "a@proj-a"}')
    boot(keeps_path=shaped)
    assert rig.daemon.keeps == {}


def test_conflicting_entries_are_all_dropped_and_only_they_are(rig, boot, caplog,
                                                                 tmp_path):
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps([
        {"kind": "live", "slot": 1, "qualified": "a@proj-a"},
        {"kind": "live", "slot": 1, "qualified": "b@proj-a"},          # slot 1 twice
        {"kind": "live", "slot": 2, "qualified": "c@proj-a"},
        {"kind": "keep", "slot": 3, "qualified": "c@proj-a",           # c twice
         "expires": rig.clock() + 60},
        {"kind": "live", "slot": 4, "qualified": "d@proj-a"},
        {"kind": "live", "slot": 5, "qualified": "agent-3"},           # never kept
        {"kind": "live", "slot": 99, "qualified": "e@proj-a"},         # not provisioned
    ]))
    caplog.set_level(logging.INFO, logger="comms.daemon")
    boot(keeps_path=path)
    assert {s: k.qualified for s, k in rig.daemon.keeps.items()} == {4: "d@proj-a"}
    drops = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert len(drops) == 6, "each drop logged once"


def test_expiries_are_validated_and_clamped_to_one_window(rig, boot, tmp_path):
    """`NaN` and `Infinity` parse, and would keep a slot forever;
    a value far ahead (a wrong clock at boot) is clamped to boot + SLOT_KEEP."""
    path = tmp_path / "values.json"
    path.write_text(
        '[{"kind": "keep", "slot": 1, "qualified": "a@proj-a", "expires": NaN},'
        ' {"kind": "keep", "slot": 2, "qualified": "b@proj-a", "expires": Infinity},'
        ' {"kind": "keep", "slot": 3, "qualified": "c@proj-a", "expires": "soon"},'
        f' {{"kind": "keep", "slot": 4, "qualified": "d@proj-a", '
        f'"expires": {rig.clock() + 10**6}}},'
        f' {{"kind": "keep", "slot": 5, "qualified": "e@proj-a", '
        f'"expires": {rig.clock() - 1}}}]')
    boot(keeps_path=path)
    assert list(rig.daemon.keeps) == [4], "non-finite, non-numeric and lapsed dropped"
    assert rig.daemon.keeps[4].expires == rig.clock() + WINDOW, "clamped"
    assert all(math.isfinite(e["expires"]) for e in mark() if e["kind"] == "keep")


def test_a_clock_jump_only_shortens_a_keep(rig, boot):
    """The wall clock carries a keep across boots; the monotonic clock bounds it inside
    one. A backward step of the wall clock cannot stretch it; a forward one ends it."""
    rig.shim(slot=1, name="a", scope="proj-a")
    rig.shim(slot=2, name="b", scope="proj-a")
    wall, mono = FakeClock(rig.clock()), FakeClock(1000.0)
    d = boot(clock=wall, monotonic=mono)
    wall.advance(-3600)                         # the wall clock steps back an hour
    assert d._keeps_view()[1]["expires_in"] == pytest.approx(WINDOW)
    mono.advance(WINDOW + 1)                     # ...and one real window passes
    with d._lock, d._db() as db:
        assert d._pick_slot(db, "next-free", "x@proj-a") == 1, "lapsed on time anyway"
    assert d.keeps == {}

    rig.shim(slot=1, name="a", scope="proj-a")
    wall2, mono2 = FakeClock(rig.clock()), FakeClock(0.0)
    d = boot(clock=wall2, monotonic=mono2)
    wall2.advance(WINDOW + 1)                    # a forward step ends it early
    with d._lock, d._db() as db:
        assert d._pick_slot(db, "next-free", "x@proj-a") == 1
    assert d.keeps == {}


def test_a_failed_write_does_not_stop_a_bind_and_the_next_write_repairs_it(
        rig, monkeypatch, caplog):
    """The file is then OLDER than memory (stale), not current.
    The next pick tries again, and a whole-file write from memory corrects it."""
    caplog.set_level(logging.WARNING, logger="comms.daemon")

    def refuse(*a, **kw):
        raise OSError(28, "No space left on device")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon_mod.tempfile, "mkstemp", refuse)
        a = rig.shim(slot=1, name="a", scope="proj-a")
        assert a.identity == "a@proj-a", "the bind went through"
        assert rig.daemon._keeps_stale
        assert any("could not be written" in r.getMessage() for r in caplog.records)
    assert live_in(mark()) == {}, "the file is stale"
    rig.shim(slot=2, name="b", scope="proj-a")
    assert live_in(mark()) == {1: "a@proj-a", 2: "b@proj-a"}
    assert not rig.daemon._keeps_stale


def test_a_deregister_whose_write_failed_leaves_a_false_keep_that_lapses_harmlessly(
        rig, boot):
    a = rig.shim(slot=1, name="a", scope="proj-a")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon_mod.tempfile, "mkstemp",
                   lambda *x, **k: (_ for _ in ()).throw(OSError(5, "EIO")))
        a.stop()
        assert gone(rig, "a@proj-a")
    assert live_in(mark()) == {1: "a@proj-a"}, "the unbind never reached the file"
    crash(rig)                                   # nothing wrote after that
    boot(stop=False)
    assert rig.daemon.keeps[1].qualified == "a@proj-a", "a false keep"
    assert rig.shim(name="x", scope="proj-a").slot_id == 2, "it steers next-free only"
    assert rig.shim(slot=1, name="y", scope="proj-a").slot_id == 1, "it refuses no one"
    assert rig.daemon.keeps == {}, "and a takeover ends it"


def test_a_start_the_probe_refuses_writes_no_mark(rig, caplog):
    """The load sits after the probe, and a refused start --
    and its `stop()` -- never touch the live daemon's mark."""
    rig.shim(slot=1, name="a", scope="proj-a")
    before = slot_keeps().read_bytes()
    intruder = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    with pytest.raises(RuntimeError, match="live comms daemon"):
        intruder.start()
    intruder.stop()
    assert slot_keeps().read_bytes() == before
    assert live_in(mark()) == {1: "a@proj-a"}


def test_the_keep_clock_starts_at_the_listen_not_at_the_load(rig, boot):
    """A boot that dies before listening writes nothing, so the
    next boot still finds the `live` entries and gives them a full window. The boot step
    between the keep load and the listen is the queue load."""
    rig.shim(slot=1, name="a", scope="proj-a")
    rig.daemon.stop()
    before = slot_keeps().read_bytes()

    def dies(self):
        raise RuntimeError("the gateway would not open")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Daemon, "_load_queue", dies)
        doomed = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
        with pytest.raises(RuntimeError):
            doomed.start()
        doomed.stop()
    assert slot_keeps().read_bytes() == before
    rig.clock.advance(WINDOW * 3)                # a crash loop that long
    boot(stop=False)
    assert rig.daemon.keeps[1].expires == rig.clock() + WINDOW


# ------------------------------------------------------------------ the freeze

def test_the_freeze_turns_away_a_register_in_flight_and_keeps_the_mark(
        rig, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="comms.daemon")
    rig.shim(slot=1, name="a", scope="proj-a")
    d = rig.daemon
    reached = threading.Event()
    real = d._high_water_mark

    def hwm():
        answer = real()
        reached.set()                            # the register is about to take the lock
        return answer

    monkeypatch.setattr(d, "_high_water_mark", hwm)
    outcome: dict = {}

    def late():
        try:
            outcome["resp"] = register(name="late", scope="proj-a")[1]
        except Exception as e:                   # noqa: BLE001 -- the outcome is the test
            outcome["error"] = e

    with d._lock:
        t = threading.Thread(target=late, daemon=True)
        t.start()
        assert reached.wait(3.0)
        d.stop()                                 # the freeze lands first, under the lock
    t.join(5.0)
    assert isinstance(outcome.get("error"), DaemonOffline), outcome
    entries = mark()
    assert live_in(entries) == {1: "a@proj-a"}, \
        "a's serve loop ended after the freeze and did not remove it"
    assert "late@proj-a" not in json.dumps(entries)
    assert any("turned away" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.exc_info], "no traceback for a stop"


# -------------------------------------------------------- the refused-register line

def test_a_refused_register_is_one_journal_line_naming_what_was_asked(rig, caplog):
    rig.shim(slot=1, name="planner", scope="proj-a")
    caplog.set_level(logging.INFO, logger="comms.daemon")
    with pytest.raises(DaemonError):
        register(slot=1, name="other", scope="proj-a", adapter="codex")
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("register refused")]
    assert lines == ["register refused: name=other scope=proj-a slot=1 adapter=codex: "
                     "slot 1 is already bound by planner@proj-a"]
    caplog.clear()
    with pytest.raises(DaemonError):
        register(slot=7)
    assert [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("register refused")] == [
        "register refused: name=auto scope=global slot=7 adapter=claude-shim: "
        "slot 7 is not provisioned"]


# ------------------------------------------------ the shim

def test_a_has_held_seat_whose_slot_was_taken_falls_back_at_once_on_reconnect(
        rig, boot, monkeypatch):
    seeds = gated(monkeypatch)
    lead = rig.shim(slot=1, name="captain", scope="proj-b", autoreconnect=True)
    rig.shim(slot=2, name="peer", scope="proj-b")
    rig.daemon.stop()
    assert wait_until(lambda: seeds and seeds[0].waits >= 1)
    boot()
    rig.clock.advance(WINDOW + 1)                # away past the keep window
    rig.shim(name="scout", global_=True)       # next-free, takes slot 1
    assert rig.daemon.bindings["scout"].slot_id == 1

    seeds[0].step()
    assert wait_until(lambda: lead.identity == "captain@proj-b" and not lead.offline)
    assert lead.slot_id == 2, "the lowest free unkept slot"
    assert seeds[0].waits == 1, "no backoff step between the refusal and the retry"
    reason = "slot 1 is already bound by scout"
    assert lead.statuses.count(SLOT_MOVED.format(asked=1, reason=reason, got=2)) == 1
    status = status_of(lead)
    assert status["supervisor"] == "running" and status["slot"] == 2
    assert status["slot_asked"] == 1 and status["slot_refused_by"] == reason
    assert lead.slot == "next-free", "the process registers next-free from now on"

    # A later restart gives it slot 2 back through its keep.
    rig.daemon.stop()
    assert wait_until(lambda: len(seeds) >= 2 and seeds[1].waits >= 1)
    boot()
    assert rig.shim(name="third", scope="proj-b").slot_id == 3, "1 and 2 are both kept"
    seeds[1].step()
    assert wait_until(lambda: not lead.offline)
    assert lead.slot_id == 2


def test_a_has_held_shim_refused_for_its_own_name_still_stops_and_says_why(
        rig, boot, monkeypatch):
    seeds = gated(monkeypatch)
    shim = rig.shim(slot=2, name="reviewer", scope="proj-a", autoreconnect=True)
    rig.daemon.stop()
    assert wait_until(lambda: seeds and seeds[0].waits >= 1)
    boot()
    rig.shim(slot=1, name="reviewer", scope="proj-a")          # the successor
    seeds[0].step()
    assert wait_until(lambda: REFUSED_TERMINAL in shim.statuses)
    assert wait_until(lambda: not shim._supervisor.is_alive())
    status = status_of(shim)
    assert status["supervisor"] == "ended"
    assert status["ended_because"].startswith(
        "name held by another session: reviewer@proj-a is already bound")
    assert status["state"] == "refused"
    assert status["last_refusal"].startswith("reviewer@proj-a is already bound")
    assert status["slot_asked"] is None, "a name refusal is no slot fallback"


def test_a_never_held_shim_refused_for_a_slot_falls_back_not_waits(
        rig, boot, monkeypatch):
    seeds = gated(monkeypatch)
    rig.daemon.stop()
    with launched(rig, slot=1, name="captain", scope="proj-b",
                  backoff=None) as launch:
        shim = launch.shim
        assert wait_until(lambda: seeds and seeds[0].waits >= 1)
        assert shim.ever_bound is False
        boot(stop=False)
        rig.shim(slot=1, name="holder", scope="proj-b")
        seeds[0].step()
        assert wait_until(lambda: shim.identity == "captain@proj-b")
        assert shim.slot_id == 2
        assert REFUSED_WAITING not in shim.statuses
        assert any(s.startswith("comms online on another bundle") for s in shim.statuses)
        assert launch.running
    assert launch.code == 0


def test_a_never_held_shim_refused_for_its_name_still_waits(rig, boot, monkeypatch):
    seeds = gated(monkeypatch)
    rig.daemon.stop()
    with launched(rig, slot=2, name="reviewer", scope="proj-a",
                  backoff=None) as launch:
        shim = launch.shim
        assert wait_until(lambda: seeds and seeds[0].waits >= 1)
        boot(stop=False)
        rig.shim(slot=1, name="reviewer", scope="proj-a")
        seeds[0].step()
        assert wait_until(lambda: REFUSED_WAITING in shim.statuses)
        assert shim.slot == 2 and shim.slot_asked is None, "no fallback for a name"
        assert shim._supervisor.is_alive()
    assert launch.code == 0


def test_an_unexpected_exception_in_connect_is_survived(rig, boot, monkeypatch, caplog):
    seeds = gated(monkeypatch)
    shim = rig.shim(slot=2, name="reviewer", scope="proj-a", autoreconnect=True)
    rig.daemon.stop()
    assert wait_until(lambda: seeds and seeds[0].waits >= 1)
    boot()
    real = shim._connect_once
    calls = []

    def once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(shim, "_connect_once", once)
    caplog.set_level(logging.INFO, logger="comms.shim")
    seeds[0].step()
    expected = SUPERVISOR_ERROR.format(error="RuntimeError: boom")
    assert wait_until(lambda: expected in shim.statuses)
    assert shim._last_reported_state == "error:RuntimeError"
    assert shim._supervisor.is_alive()
    errors = [r for r in caplog.records
              if r.getMessage() == "comms supervisor: unexpected error, retrying"]
    assert errors and errors[0].exc_info, "logged with its traceback"
    assert wait_until(lambda: seeds[0].waits >= 2), "and it waited its next backoff"
    seeds[0].step()
    assert wait_until(lambda: shim.identity == "reviewer@proj-a" and not shim.offline)
    assert shim.statuses.count(expected) == 1


def test_a_finite_backoff_that_runs_dry_falls_back_to_the_real_one(rig, boot,
                                                                      monkeypatch):
    shim = rig.shim(slot=2, name="reviewer", scope="proj-a", autoreconnect=True,
                    backoff=iter([0.01]))
    rig.daemon.stop()
    boot()
    assert wait_until(lambda: not shim.offline, timeout=5.0), "it bound anyway"
    assert shim._supervisor.is_alive()


def test_a_shim_without_a_supervisor_and_a_stopped_one_answer_honestly(rig):
    plain = rig.shim(slot=2, name="reviewer", scope="proj-a")
    assert status_of(plain)["supervisor"] == "none"
    supervised = rig.shim(slot=3, name="curator", scope="proj-a", autoreconnect=True)
    assert status_of(supervised)["supervisor"] == "running"
    assert status_of(supervised)["ended_because"] is None
    supervised.stop()
    assert wait_until(lambda: not supervised._supervisor.is_alive())
    assert status_of(supervised)["ended_because"] == "stopped"


# ------------------------------------------------------------------- codex

def _flaky_daemon_connect(adapter):
    calls = []

    def connect_daemon():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        adapter.offline = False

    adapter.connect_daemon = connect_daemon
    return calls


def test_the_codex_supervisor_survives_an_unexpected_exception(rig, tmp_path):
    from moxi_comms import codex_threads
    from moxi_comms.adapters.codex import CodexAdapter
    from moxi_comms.adapters.codex_roster import CodexAgent
    from fake_turns import FakeSession, launcher

    agent = CodexAgent(qualified="planner@proj-a", name="planner", scope="proj-a", slot=3,
                       cwd=str(tmp_path), thread_file=codex_threads.path("planner@proj-a"),
                       directors=[])
    statuses: list = []
    adapter = CodexAdapter(agent, launch=launcher(FakeSession()), autoreconnect=True,
                           backoff=itertools.repeat(0.01), status_sink=statuses.append)
    calls = _flaky_daemon_connect(adapter)
    t = threading.Thread(target=adapter._supervise, daemon=True)
    t.start()
    try:
        assert wait_until(lambda: len(calls) >= 2 and not adapter.offline)
        assert t.is_alive()
        assert statuses.count(SUPERVISOR_ERROR.format(error="RuntimeError: boom")) == 1
    finally:
        adapter._stop.set()
        t.join(2.0)


# ------------------------------------------------ keeps where people look

def test_keeps_show_in_the_agents_query_and_the_seats_view_only(rig, boot):
    rig.shim(slot=1, name="a", scope="proj-a")
    boot()
    rig.clock.advance(30)
    c, _ = register(name="x", scope="proj-a")
    try:
        info = c.query("agents")
    finally:
        c.close()
    assert info["keeps"] == {"1": {"qualified": "a@proj-a", "expires_in": WINDOW - 30}}
    code, seats = rig.cli(["agents", "--seats"])
    assert code == 0
    assert "slot keeps" in seats and "a@proj-a" in seats and "90s left" in seats
    code, plain = rig.cli(["agents"])
    assert "slot keeps" not in plain and "a@proj-a" not in plain, \
        "agent-facing surfaces carry no slot vocabulary"


# ------------------------------------------- the first bind

def test_a_launch_pinned_to_a_held_slot_binds_next_free_and_serves(rig):
    rig.shim(slot=1, name="scout", global_=True)
    with launched(rig, slot=1, name="captain",
                  scope="proj-b") as launch:
        shim = launch.shim
        assert wait_until(lambda: shim.identity == "captain@proj-b")
        assert shim.slot_id == 2
        assert shim.statuses == [SLOT_MOVED.format(
            asked=1, reason="slot 1 is already bound by scout", got=2)]
        assert launch.running and launch.code is None, "no exit"
        status = status_of(shim)
        assert status["slot"] == 2 and status["slot_asked"] == 1
    assert launch.code == 0


def test_a_launch_pinned_to_an_unprovisioned_slot_does_the_same(rig):
    with launched(rig, slot=99, name="captain",
                  scope="proj-b") as launch:
        shim = launch.shim
        assert wait_until(lambda: shim.identity == "captain@proj-b")
        assert shim.slot_id == 1
        assert shim.slot_refused_by == "slot 99 is not provisioned"
        assert launch.running
    assert launch.code == 0


def test_the_fallback_takes_an_unkept_slot_before_a_kept_one(rig, boot):
    rig.shim(slot=2, name="other", scope="proj-a")
    boot()
    rig.shim(slot=1, name="x", scope="proj-a")
    moved = rig.shim(slot=1, name="a", scope="proj-a")
    assert moved.slot_id == 3, "2 is kept for another name and 3 is free"
    assert rig.daemon.keeps[2].qualified == "other@proj-a"


def test_with_every_free_slot_kept_the_fallback_gives_one_up(rig, boot, caplog):
    for n in range(1, 7):
        rig.shim(slot=n, name=f"n{n}", scope="proj-a")
    boot()
    rig.shim(slot=1, name="x", scope="proj-a")               # takes n1's keep
    caplog.set_level(logging.INFO, logger="comms.daemon")
    caplog.clear()
    moved = rig.shim(slot=1, name="a", scope="proj-a")
    assert moved.identity == "a@proj-a", "nothing is refused on a keep's account"
    assert moved.slot_id == 2
    assert keep_lines(caplog) == [
        "slot keep for n2@proj-a on slot 2 ended: given up to a@proj-a (no other slot free)"]


def test_a_first_bind_name_refusal_waits_for_the_name(rig, capsys):
    """A launch into a name another session holds waits for it and cannot send
    meanwhile; it does not exit."""
    rig.shim(slot=1, name="reviewer", scope="proj-a")
    with launched(rig, slot=2, name="reviewer",
                  scope="proj-a") as launch:
        assert wait_until(lambda: launch.shim.claims.state == "waiting")
        assert launch.code is None and launch.shim.identity is None
    assert REFUSED_PREFIX not in capsys.readouterr().err


def test_a_fallback_refused_no_free_slot_exits_at_first_bind(rig, capsys):
    for n in range(1, 7):
        rig.shim(slot=n, name=f"n{n}", scope="proj-a")
    with launched(rig, slot=1, name="captain",
                  scope="proj-b") as launch:
        assert wait_until(lambda: launch.code is not None)
    assert launch.code == claude_shim.EXIT_REFUSED
    err = capsys.readouterr().err
    assert REFUSED_PREFIX in err and "no free slot" in err


def test_on_reconnect_no_free_slot_retries_on_the_backoff(rig, boot, monkeypatch):
    seeds = gated(monkeypatch)
    lead = rig.shim(slot=1, name="captain", scope="proj-b", autoreconnect=True)
    rig.daemon.stop()
    assert wait_until(lambda: seeds and seeds[0].waits >= 1)
    boot()
    others = [rig.shim(slot=n, name=f"n{n}", scope="proj-a") for n in range(1, 7)]
    seeds[0].step()
    line = REFUSED_RETRYING.format(reason="no free slot")
    assert wait_until(lambda: line in lead.statuses)
    assert wait_until(lambda: seeds[0].waits >= 2), "waiting on the SAME generator"
    assert len(seeds) == 1 and lead._supervisor.is_alive()
    assert status_of(lead)["state"] == "refused:no free slot"
    seeds[0].step()
    assert wait_until(lambda: seeds[0].waits >= 3)
    assert lead.statuses.count(line) == 1, "said once per distinct reason"
    others[5].stop()
    assert gone(rig, "n6@proj-a")
    seeds[0].step()
    assert wait_until(lambda: not lead.offline)
    assert lead.slot_id == 6 and lead.slot_asked == 1


def test_a_transient_at_first_bind_still_exits_the_declared_gap(rig, monkeypatch):
    monkeypatch.setattr(rig.daemon, "_high_water_mark",
                        lambda: ("0", RuntimeError("429 Too Many Requests")))
    with launched(rig, slot=2, name="reviewer",
                  scope="proj-a") as launch:
        assert wait_until(lambda: launch.code is not None)
    assert launch.code == claude_shim.EXIT_REFUSED, \
        "unchanged; a later fix shows up as this test changing"


# ------------------------------------------------ the bundle follows the binding

def _moved_planner(rig):
    """planner, stamped slot 1 at launch, bound on another slot after a fallback."""
    rig.shim(slot=1, name="holder", scope="proj-a")
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    assert planner.slot_id == 3
    rig.clock.advance(5)
    return planner


def test_a_moved_session_posts_on_the_slot_it_is_bound_on_verified(rig):
    _moved_planner(rig)
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "hi"], slot=1, name="planner",
                        scope="proj-a")
    assert code == 0, out
    posted = rig.gateway.history(rig.names_channel)[-1]
    assert posted.author_id == rig.slot_webhook_id(3), "slot 3's token, not the stamp's"
    live = rig.daemon.attribute(posted, live=True)
    replayed = rig.daemon.attribute(posted, live=False)
    assert (live.qualified, live.attribution) == ("planner@proj-a", "verified")
    assert (replayed.qualified, replayed.attribution) == ("planner@proj-a", "verified")


def test_with_the_name_unbound_an_offline_claim_posts_on_its_slot(rig):
    """No adapter binds planner; the rig sends from a session holding its claim offline
    on slot 1 (what a stamped send spent before the env path closed)."""
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    code, _ = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "hi"], slot=1, name="planner", scope="proj-a")
    assert code == 0
    posted = rig.gateway.history(rig.names_channel)[-1]
    assert posted.author_id == rig.slot_webhook_id(1)


def test_a_malformed_stamp_is_refused_before_any_lookup(rig, monkeypatch):
    asked = []
    real = DaemonClient.query

    def query(self, kind, **kw):
        asked.append(kind)
        return real(self, kind, **kw)

    monkeypatch.setattr(DaemonClient, "query", query)
    rig.shim(slot=3, name="planner", scope="proj-a")
    code, out = rig.cli(["send", "--posture", "none", "reviewer", "hi"], slot="x", name="planner",
                        scope="proj-a")
    assert code == 2 and "launch stamp names planner@proj-a" in out
    assert "x" not in out.split("—")[0], "the malformed value is not echoed"
    assert asked == [], "refused before the daemon was asked anything"
    assert rig.gateway.history(rig.names_channel) == []


def test_a_bound_claim_posts_on_its_slot_with_no_lookup_to_fail(rig, monkeypatch):
    """The env path's binding lookup is gone with it: a session sends as its claim, which
    carries the slot the daemon bound it on, so a lookup that would fail is never made
    and the post goes out verified on that slot."""
    _moved_planner(rig)
    real = DaemonClient.query

    def query(self, kind, **kw):
        if kind == "agents":
            raise DaemonError("the daemon could not answer")
        return real(self, kind, **kw)

    monkeypatch.setattr(DaemonClient, "query", query)
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "hi"], slot=1, name="planner",
                        scope="proj-a")
    assert code == 0, out
    posted = rig.gateway.history(rig.names_channel)[-1]
    assert posted.author_id == rig.slot_webhook_id(3), "the claim's slot, not the stamp's"
    assert rig.daemon.attribute(posted, live=True).attribution == "verified"


def test_a_post_in_the_reconnect_backoff_goes_out_on_the_claims_slot(rig, boot,
                                                                         monkeypatch):
    """While the shim is still in its backoff after the daemon returns, the session is
    unbound and its claim `offline`: its post goes out as the claim on the claim's own
    slot, never on the launch stamp's (a slot another name holds), and is not verified.
    Before the env path closed it spent the stamp."""
    seeds = gated(monkeypatch)
    rig.shim(slot=1, name="holder", scope="proj-a")
    planner = rig.shim(slot=1, name="planner", scope="proj-a", autoreconnect=True)
    assert planner.slot_id == 2
    rig.daemon.stop()
    assert wait_until(lambda: seeds and seeds[0].waits >= 1)
    boot()
    rig.shim(slot=1, name="holder", scope="proj-a")
    rig.shim(slot=3, name="reviewer", scope="proj-a")
    rig.clock.advance(5)
    assert "planner@proj-a" not in rig.daemon.bindings, "still in its backoff"
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "hi"], slot=1, name="planner", scope="proj-a")
    assert code == 0, out
    assert planner.claims.state == "offline"
    posted = rig.gateway.history(rig.names_channel)[-1]
    assert posted.author_id == rig.slot_webhook_id(2), "the claim's own slot"
    assert posted.author_id != rig.slot_webhook_id(1), "never the stamp's, which holder has"
    assert rig.daemon.attribute(posted, live=True).attribution != "verified"
    seeds[0].step()
    assert wait_until(lambda: not planner.offline)
    assert planner.slot_id == 2, "and it comes back to its own kept slot"
