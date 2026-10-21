"""Register concurrency: the daemon's one lock, and the round trip that was held under it.

`_register` reads `gateway.max_message_id()` ABOVE the daemon's global lock. That read
is a file read against the mock (~5ms) and a Discord REST round trip in production (tens
to hundreds of ms), so taking it under the lock would serialize every register -- and
every router, resolver and query that wanted the lock -- behind a network call, while
the smokes measured a fast daemon. The mock has a DECLARED hold on that call so the
difference is visible from a test at all.

What these pin, in order:

  * concurrent registers OVERLAP their gateway round trips instead of queueing behind
    each other -- with the read under the lock the overlap count would be 1 by
    construction, and no timing luck could make it anything else;
  * no register pays for another register's round trip;
  * exclusivity is exactly as it was: one binding per name, one per slot, the loser
    refused in the daemon's own words with the holder named, and nothing journalled for
    a refusal -- now proven under the concurrency the hoist actually permits;
  * the catch-up boundary stays EXACT. A message that lands DURING the hoisted round
    trip is above the value the gateway answered with, and the router resolved it
    against a table with no binding in it. It is delivered exactly once (as catch-up)
    rather than left in `pending` until some later rescan.

Every timing assertion is one-sided -- it can only fail by the daemon being SLOWER than
the bound -- and every bound is a multiple of a hold this file sets itself.
"""

import threading
import time

import pytest
from conftest import queued, wait_until, warm_session

from moxi_comms.client import DaemonClient, DaemonError
from moxi_comms.gateway import GatewayError, Message
from moxi_comms.mock_gateway import MockGateway
from moxi_comms.paths import daemon_socket

HOLD = 0.3          # seconds of declared round trip, per max_message_id call


def instrument(gateway, hold: float = HOLD) -> list:
    """Engage the declared hold and record each round trip's [enter, exit) interval."""
    gateway.max_message_id_hold = hold
    calls: list[tuple[float, float]] = []
    real = gateway.max_message_id

    def timed(channel_id=None):
        t0 = time.monotonic()
        try:
            return real(channel_id)
        finally:
            calls.append((t0, time.monotonic()))

    gateway.max_message_id = timed
    return calls


def max_overlap(intervals) -> int:
    """The largest number of these intervals that were in flight at one instant."""
    edges = sorted([(t0, 1) for t0, _ in intervals] + [(t1, -1) for _, t1 in intervals])
    live = best = 0
    for _, delta in edges:
        live += delta
        best = max(best, live)
    return best


def race(fn, n: int, timeout: float = 30.0) -> dict:
    """Run `fn(i)` in n threads released together. -> {i: (value, error, seconds)}."""
    barrier = threading.Barrier(n)
    out: dict = {}

    def run(i):
        barrier.wait()
        t0 = time.monotonic()
        try:
            value, error = fn(i), None
        except Exception as e:                       # the refusals are the point
            value, error = None, e
        out[i] = (value, error, time.monotonic() - t0)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    return out


def clients(n: int) -> list:
    """n connected daemon clients. Connecting is not what is under test, so it is done
    before the barrier."""
    return [DaemonClient(daemon_socket()).connect() for _ in range(n)]


# ------------------------------------------------------------------- the lock hold

def test_concurrent_registers_overlap_their_gateway_round_trips(rig):
    """The targeted assertion, and the one that cannot pass on the old code: three
    registers, three round trips, all three in flight at once. With the read inside the
    critical section the overlap would be 1 -- not 'usually 1', but 1 by construction."""
    calls = instrument(rig.gateway)
    cs = clients(3)
    try:
        results = race(lambda i: cs[i].register(slot=i + 1, name=f"peer-{i}",
                                                scope="proj-a"), 3)
        assert [r[1] for r in results.values()] == [None, None, None]
        assert len(calls) == 3, "one boundary read per register, no more"
        assert max_overlap(calls) == 3, (
            "the three round trips were serialized -- the boundary read is back under "
            "the daemon's global lock")
    finally:
        for c in cs:
            c.close()


def test_no_register_waits_out_another_registers_round_trip(rig):
    """The cost, stated as the operator meets it: four sessions launching together used
    to pay up to four round trips each. Now each pays its own.

    The bound is a CONTROL, not a constant: one uncontended register on this machine,
    right now, plus slack. A loaded box slows the control too, so the assertion measures
    contention and nothing else. The slack is TWO round trips because a loaded box also
    queues the short critical section the registers still share (observed: ~0.1s a piece
    at the tail of a full suite run, against a 0.3s hold). The defect it discriminates
    against costs the last of five registers FIVE round trips, so two of slack leaves the
    assertion one-sided by a wide margin in both directions."""
    instrument(rig.gateway)
    control = clients(1)[0]
    cs = clients(5)
    try:
        t0 = time.monotonic()
        control.register(slot=1, name="control", scope="proj-a")
        solo = time.monotonic() - t0

        results = race(lambda i: cs[i].register(slot=i + 2, name=f"seat-{i}",
                                                scope="proj-a"), 5)
        assert [r[1] for r in results.values()] == [None] * 5
        contended = sorted(seconds for _, _, seconds in results.values())
        assert contended[-1] < solo + HOLD * 2, (
            f"a register alone took {solo:.3f}s; five at once took "
            f"{[round(s, 3) for s in contended]} against a {HOLD:.2f}s round trip -- the "
            f"slowest waited out someone else's")
    finally:
        control.close()
        for c in cs:
            c.close()


# ------------------------------------------------------------------- the refusals

def test_one_name_survives_a_concurrent_stampede(rig):
    """Exclusivity is what the lock is FOR, and the hoist must not have loosened it: the
    name check and the table mutation are still one critical section."""
    instrument(rig.gateway)
    cs = clients(4)
    try:
        results = race(lambda i: cs[i].register(slot=i + 1, name="only", scope="proj-a"), 4)
        winners = [i for i, (_, error, _) in results.items() if error is None]
        assert len(winners) == 1, "exactly one claimant binds a name, concurrent or not"
        for i, (_, error, _) in results.items():
            if i in winners:
                continue
            assert isinstance(error, DaemonError)
            assert "only@proj-a is already bound" in str(error)
            assert error.response["holder"]["qualified"] == "only@proj-a"
        assert list(rig.daemon.bindings) == ["only@proj-a"]
        rows = [r for r in rig.daemon.journal.intervals() if r["qualified"] == "only@proj-a"]
        assert len(rows) == 1, "a refused register journals nothing; only the bind is custody"
    finally:
        for c in cs:
            c.close()


def test_one_slot_survives_a_concurrent_stampede(rig):
    """The other refusal, same law: the slot is picked and taken under one lock."""
    instrument(rig.gateway)
    cs = clients(4)
    try:
        results = race(lambda i: cs[i].register(slot=1, name=f"claimant-{i}",
                                                scope="proj-a"), 4)
        winners = [i for i, (_, error, _) in results.items() if error is None]
        assert len(winners) == 1
        held = results[winners[0]][0]["identity"]["qualified"]
        for i, (_, error, _) in results.items():
            if i in winners:
                continue
            assert isinstance(error, DaemonError)
            assert str(error) == f"slot 1 is already bound by {held}"
            assert error.response["holder"] == {"qualified": held, "slot": 1,
                                                "adapter": "claude-shim"}
        assert list(rig.daemon.by_slot) == [1]
    finally:
        for c in cs:
            c.close()


def test_a_broken_gateway_does_not_change_which_refusal_a_register_gets(rig):
    """The boundary read now runs BEFORE the exclusivity checks, and its failure must not
    overtake them: a second claimant for a bound name hears `already bound`, not whatever
    the record was doing at the time. That is why the read is deferred-raise -- the
    exception is raised where the call itself used to sit."""
    held = clients(1)[0]
    held.register(slot=1, name="held", scope="proj-a")

    def unreachable(channel_id=None):
        raise GatewayError("503 the record is unreachable")

    rig.gateway.max_message_id = unreachable

    late = clients(1)[0]
    try:
        with pytest.raises(DaemonError, match="held@proj-a is already bound"):
            late.register(slot=2, name="held", scope="proj-a")
        # ...and a register that WOULD have bound still fails on the gateway, and binds
        # nothing: the boundary is not something a register may proceed without.
        with pytest.raises(DaemonError, match="the record is unreachable"):
            late.register(slot=2, name="fresh", scope="proj-a")
        assert list(rig.daemon.bindings) == ["held@proj-a"]
        assert [r["qualified"] for r in rig.daemon.journal.intervals()] == ["held@proj-a"]
    finally:
        held.close()
        late.close()


def test_next_free_hands_out_distinct_slots_under_the_hoist(rig):
    """`next-free` reads `by_slot` and writes it in the same breath. Six claimants, six
    slots, no two the same -- the check the hoist would have broken had it taken the
    slot pick outside the lock with the gateway read."""
    instrument(rig.gateway)
    cs = clients(6)
    try:
        results = race(lambda i: cs[i].register(name=f"free-{i}", scope="proj-a"), 6)
        assert [r[1] for r in results.values()] == [None] * 6
        slots = sorted(value["slot"] for value, _, _ in results.values())
        assert slots == [1, 2, 3, 4, 5, 6]
    finally:
        for c in cs:
            c.close()


# ------------------------------------------------------------------- the instruments

def test_a_non_numeric_id_on_the_name_lane_is_still_harmless(rig):
    """The boundary read runs for EVERY #names message now, above the machine-line parse,
    so it must not be the thing that takes down a message the router would otherwise have
    shrugged off. An id that is not a number is not one the record could ever answer
    with, so it is no boundary either."""
    chatter = Message(id="not-a-number", channel_id=rig.names_channel, author_id="900001",
                      author_kind="user", author_display="operator",
                      content="no machine line here", timestamp="2026-08-19T00:00:00Z")
    rig.daemon._route(chatter, backlog=False)      # unguarded, this raised ValueError
    assert rig.daemon._seen_max == "0", "and it did not become the catch-up boundary"


def test_the_declared_hold_is_a_class_level_declaration(monkeypatch):
    """Declare-and-enforce, honestly: the class attribute IS the default, as
    `max_content_length` is. A constructor that always assigned an instance attribute
    would leave the declaration dead and `MockGateway.max_message_id_hold = …` silent."""
    monkeypatch.setattr(MockGateway, "max_message_id_hold", 0.05)
    assert MockGateway().max_message_id_hold == 0.05
    assert MockGateway(max_message_id_hold=0.0).max_message_id_hold == 0.0


# ------------------------------------------------------------------- the boundary

def test_a_message_that_lands_during_the_round_trip_is_delivered_not_stranded(rig):
    """The gap the hoist opens, and the guard that closes it.

    With the read outside the lock, the answer can be stale by the width of the round
    trip. A message posted inside that window is ABOVE the value the gateway gave, and
    the router resolves it while the table still has no binding -- so live routing files
    it under `pending` and never delivers it. If the boundary were the gateway's stale
    answer, replay would skip it too (it is above the boundary) and the message would sit
    there until a rescan that only a gateway reconnect brings.

    The daemon's `_seen_max` is what makes that impossible: the boundary is raised to
    what the ROUTER has already taken responsibility for, so replay owns exactly what
    live routing declined. The gate below is an explicit hold rather than a timed one --
    this is a correctness property, and it should not be asserted through a sleep."""
    entered, release = threading.Event(), threading.Event()
    real = rig.gateway.max_message_id

    def gated(channel_id=None):
        answer = real(channel_id)      # the record as of the call's START, as declared
        entered.set()
        release.wait(10)
        return answer

    rig.gateway.max_message_id = gated

    pushes: list = []
    client = DaemonClient(daemon_socket(), on_push=pushes.append).connect()
    outcome: dict = {}
    warm = warm_session()

    def register():
        try:
            outcome["ok"] = client.register(slot=1, name="late", scope="proj-a",
                                            session_id=warm)      # a working agent
        except Exception as e:
            outcome["error"] = e

    thread = threading.Thread(target=register)
    thread.start()
    try:
        assert entered.wait(5), "the register reached its boundary read"
        msg = rig.post_from_slot(2, ["late@proj-a"], "posted while the register was in flight")
        assert wait_until(lambda: msg.id in queued(rig.daemon, "late@proj-a")), (
            "precondition: the router resolved this message against a table with no "
            "binding in it -- that is the window the hoist opens")
        release.set()
        thread.join(10)
        assert outcome.get("error") is None and outcome["ok"]["slot"] == 1

        delivered = wait_until(lambda: [p for p in pushes if p.get("op") == "deliver"])
        assert [p["payload"]["message_id"] for p in delivered] == [msg.id], (
            "the message that landed during the round trip was delivered exactly once")
        assert delivered[0]["payload"].get("held") is True, \
            "the register's release owns it (its row, §3.3.4), not live routing"
        client.notify("activity", identity="late@proj-a", kind="turn", turned=True,
                      handled=[msg.id], unhandled=[], session_id=warm)
        assert wait_until(lambda: not queued(rig.daemon, "late@proj-a")), (
            "and it left the queue when its turn completed (§3.3.3)")
    finally:
        release.set()
        client.close()
