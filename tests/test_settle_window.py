"""A turn end is a release point that stays open
for a bounded settle window after its frame, not an instant.

A `turn` frame's first read can come before the harness has written the turn (Claude Code
drains its transcript writes on a 100 ms timer), so the turn that warmed a cold cache read
cold at its own end and its held mail waited for the turn after. The daemon now re-reads
at `RELEASE_SETTLE_STEPS` after a frame whose first read does not pass, whatever the queue
holds, and releases every row then queued for the name at the first read that passes,
except the ids the frame reported `unhandled`. A newer `turn` of the binding, or the
binding's end, closes a window early; a `look`, `ready` or register does not.

The daemon's read is cache's seam (`cache.clock_state`), stood in for per name by the
`seam` fixture with a read that can change with time; one test reads a real transcript.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
from conftest import assistant_line, queued, wait_until, write_transcript
from test_queue import HOUR, REVIEWER_Q, Peer, peers, post, row_for, rows  # noqa: F401

from moxi_comms import cache, daemon as daemon_mod

#: A window that runs out in a fraction of a second, for the tests that wait it out.
SHORT_STEPS = (0.05, 0.1, 0.2)


class Seam:
    """`cache.clock_state` per qualified name: a kind, or a callable of no arguments that
    returns one (read at each call). Every call for a name in the table is recorded as
    (name, monotonic time, thread name)."""

    def __init__(self):
        self.table: dict = {}
        self.notes: dict = {}
        self.calls: list = []
        #: Run inside a read, after its kind is decided and before it returns.
        self.during: dict = {}

    def cold_until(self, q: str, t: float) -> None:
        """`q` reads cold until monotonic time `t`, warm from then on."""
        self.table[q] = lambda: cache.COLD if time.monotonic() < t else cache.WARM

    def reads(self, q: str) -> int:
        return sum(1 for name, _, _ in self.calls if name == q)


@pytest.fixture
def seam(monkeypatch):
    s = Seam()
    real = cache.clock_state

    def clock_state(adapter, session_id, **kw):
        q = kw.get("name")
        entry = s.table.get(q)
        if entry is None:
            return real(adapter, session_id, **kw)
        kind = entry() if callable(entry) else entry
        s.calls.append((q, time.monotonic(), threading.current_thread().name))
        hook = s.during.get(q)
        if hook is not None:
            hook()
        notes = s.notes.get(q)
        if kind == cache.WARM:
            return cache.Clock(cache.WARM, age_seconds=1.0, ttl_seconds=HOUR, notes=notes)
        if kind == cache.COLD:
            return cache.Clock(cache.COLD, age_seconds=2 * HOUR, ttl_seconds=HOUR,
                               notes=notes)
        return cache.Clock(kind, notes=notes)

    monkeypatch.setattr(cache, "clock_state", clock_state)
    return s


def released_lines(caplog, q=REVIEWER_Q) -> list:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("released ") and r.getMessage().endswith(q)]


def still_lines(caplog, q=REVIEWER_Q) -> list:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(f"release for {q}: its cache still reads")]


# ------------------------------------------------------------------ the window releases

def test_a_cold_turn_end_releases_at_the_windows_first_passing_read(
        rig, seam, peers, caplog):
    """A held row; the seam reads cold at the frame and warm from 300 ms
    later. Within 2 s the row is handed `held: true` and the release is logged."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    with caplog.at_level(logging.INFO, logger="comms.daemon"):
        seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
        peer.turn()
        assert peer.held() == [], "the frame's own read was cold"
        assert wait_until(lambda: peer.held() == [held], timeout=2.0)
    assert released_lines(caplog) == [f"released 1 queued message(s) to {REVIEWER_Q}"]
    assert rows(rig, REVIEWER_Q) == {held}, "a release deletes no row"


def test_the_real_claude_reader_releases_once_the_turn_reaches_the_transcript(
        rig, peers):
    """The same against the real Claude reader: the session's transcript holds only a
    request two hours old when the frame arrives, and the turn's assistant line is
    appended 150 ms after it."""
    peer = peers()
    sid = rig.daemon.bindings[REVIEWER_Q].session_id
    path = write_transcript(sid, 2 * HOUR)
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    peer.turn()
    assert peer.held() == [], "the transcript did not hold the turn yet"
    time.sleep(0.15)
    with path.open("a") as f:
        f.write(json.dumps(assistant_line(0)) + "\n")
    assert wait_until(lambda: peer.held() == [held], timeout=2.0)


def test_a_send_queued_cold_inside_a_window_on_an_empty_queue_goes_at_its_pass(
        rig, seam, peers):
    """The frame finds the queue empty; 50 ms later a
    send's gate read is cold, so its row is queued; the seam reads warm from 300 ms. The
    row is handed `held: true` within 2 s."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    peer.turn()
    time.sleep(0.05)
    mid = post(rig, [REVIEWER_Q], "while the record lags")
    row_for(rig, REVIEWER_Q, mid)
    assert wait_until(lambda: peer.held() == [mid], timeout=2.0)


def test_a_send_after_the_record_reads_warm_goes_live(rig, seam, peers):
    """A send at 400 ms, after the seam reads warm, is handed live and never queued."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    peer.turn()
    time.sleep(0.4)
    mid = post(rig, [REVIEWER_Q], "after the record caught up")
    assert wait_until(lambda: peer.handed() == [mid])
    assert peer.payloads()[0].get("held") is None
    assert rows(rig, REVIEWER_Q) == set()


def test_a_held_send_after_the_window_passed_waits_for_the_next_turn_end(
        rig, seam, peers):
    """A `[held]` send takes no gate read and always queues: after a window that passed it
    waits for the next turn end, which is what `[held]` means."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    peer.turn()
    time.sleep(0.6)
    assert seam.reads(REVIEWER_Q) >= 2
    held = post(rig, [REVIEWER_Q], "next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    time.sleep(0.3)
    assert peer.handed() == []
    peer.turn()
    assert peer.held() == [held]


def test_a_read_that_stays_cold_releases_nothing_and_says_so_once(
        rig, seam, peers, caplog, monkeypatch):
    """The window runs out with no passing read: nothing is released, one line names the
    binding, the last read and the window's length, and the row still waits."""
    monkeypatch.setattr(daemon_mod, "RELEASE_SETTLE_STEPS", SHORT_STEPS, raising=False)
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    with caplog.at_level(logging.INFO, logger="comms.daemon"):
        peer.turn()
        assert wait_until(lambda: still_lines(caplog), timeout=2.0)
        time.sleep(0.3)
    assert peer.handed() == []
    assert still_lines(caplog) == [
        f"release for {REVIEWER_Q}: its cache still reads cold for 1h (last request 2h "
        "ago) 0.2s after the turn ended; the rows wait for the next release point"]
    assert seam.reads(REVIEWER_Q) == 1 + len(SHORT_STEPS)
    waits = [r for r in caplog.records
             if r.getMessage().startswith(f"release for {REVIEWER_Q} waits")]
    assert len(waits) == 1, "the window's own reads log only its closing line"
    assert rows(rig, REVIEWER_Q) == {held}


# ------------------------------------------------------------------ what closes a window

def test_the_window_never_hands_an_id_its_frame_reported_unhandled(rig, seam, peers):
    """A is handed live; B waits as a held row. The cold frame reports A
    `unhandled`; the window's release hands B and not A, which waits for the next release
    point as `_settle` says."""
    seam.table[REVIEWER_Q] = cache.WARM
    peer = peers()
    a = post(rig, [REVIEWER_Q], "live")
    assert wait_until(lambda: peer.handed() == [a])
    b = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, b)
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    peer.turn(unhandled=[a])
    assert rows(rig, REVIEWER_Q) == {a, b}
    assert wait_until(lambda: peer.held() == [b], timeout=2.0)
    time.sleep(0.6)
    assert peer.held() == [b]
    peer.turn()
    assert peer.held() == [b, a]


def test_a_newer_turn_closes_the_window_and_the_row_goes_once(rig, seam, peers):
    """A second cold `turn` 100 ms into the window closes it and opens its
    own; the row is handed once, and the closed window reads no more."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    peer.turn()
    time.sleep(0.1)
    peer.turn()
    assert wait_until(lambda: peer.held() == [held], timeout=2.0)
    time.sleep(1.2)
    assert peer.held() == [held]
    # the two frames' reads and the second window's first (at 350 ms, warm)
    assert seam.reads(REVIEWER_Q) == 3


def test_a_newer_turn_whose_read_passes_closes_the_old_window(rig, seam, peers):
    """The second `turn` 100 ms into the window reads warm: it releases at once and opens
    no window of its own, so only its own close can stop the first window. After it,
    the first window makes no more reads."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    peer.turn()
    time.sleep(0.1)
    seam.table[REVIEWER_Q] = cache.WARM
    peer.turn()
    assert peer.held() == [held]
    reads = seam.reads(REVIEWER_Q)
    time.sleep(1.2)
    assert seam.reads(REVIEWER_Q) == reads, "the first window kept reading"
    assert peer.held() == [held]


def test_a_cold_look_inside_the_window_does_not_close_it(rig, seam, peers):
    """A `look` 100 ms into the window reads cold and releases nothing; the
    window's later pass hands the row."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    peer.turn()
    time.sleep(0.1)
    peer.activity(kind="look")
    assert peer.held() == []
    assert wait_until(lambda: peer.held() == [held], timeout=2.0)


def test_a_window_hands_nothing_once_its_binding_is_gone(rig, seam, peers):
    """The binding ends 100 ms into its window and the name binds again: the old
    binding's window hands nothing to either, and the held row waits for the new
    binding's own turn end."""
    seam.table[REVIEWER_Q] = cache.COLD
    old = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    seam.cold_until(REVIEWER_Q, time.monotonic() + 0.3)
    old.turn()
    time.sleep(0.1)
    old.close()
    new = peers()
    time.sleep(1.2)
    assert old.handed() == [] and new.handed() == []
    assert rows(rig, REVIEWER_Q) == {held}
    new.turn()
    assert new.held() == [held]


def test_a_frame_whose_first_read_passes_opens_no_window(rig, seam, peers):
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    seam.table[REVIEWER_Q] = cache.WARM
    seam.calls.clear()
    peer.turn()
    assert peer.held() == [held]
    time.sleep(0.6)
    assert seam.reads(REVIEWER_Q) == 1


def test_a_reads_notes_are_logged_for_the_frame_and_not_per_window_read(
        rig, seam, peers, caplog):
    seam.table[REVIEWER_Q] = cache.COLD
    seam.notes[REVIEWER_Q] = ["cache clock: a note about this read"]
    peer = peers()
    held = post(rig, [REVIEWER_Q], "for your next turn", held=True)
    row_for(rig, REVIEWER_Q, held)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="comms.daemon"):
        seam.cold_until(REVIEWER_Q, time.monotonic() + 0.6)
        peer.turn()
        assert wait_until(lambda: peer.held() == [held], timeout=2.0)
    assert seam.reads(REVIEWER_Q) >= 3
    notes = [r for r in caplog.records
             if r.getMessage() == "cache clock: a note about this read"]
    assert len(notes) == 1


# ------------------------------------------------------------------ the send that races the pass

def test_a_send_read_cold_before_the_pass_and_queued_after_it_is_not_left_behind(
        rig, seam, peers):
    """The send's gate read begins while
    the record still lags and reads cold, but its row would be queued only after the
    window's passing read has released. The send reads once more and goes live; nothing
    waits."""
    seam.table[REVIEWER_Q] = cache.COLD
    peer = peers()
    released = threading.Event()
    real_release = rig.daemon._release

    def release(binding, **kw):
        try:
            return real_release(binding, **kw)
        finally:
            released.set()

    rig.daemon._release = release
    routing = threading.local()
    real_route = rig.daemon._route_recipients

    def route(*a, **kw):
        routing.on = True
        try:
            return real_route(*a, **kw)
        finally:
            routing.on = False

    rig.daemon._route_recipients = route
    once = []

    def hold_the_send_read():
        # The send's first gate read decided cold; it returns only after the window
        # has released (or 1.5 s, on a daemon with no window).
        if getattr(routing, "on", False) and not once:
            once.append(1)
            seam.table[REVIEWER_Q] = lambda: cache.WARM
            released.wait(1.5)

    peer.turn()
    seam.during[REVIEWER_Q] = hold_the_send_read
    mid = post(rig, [REVIEWER_Q], "raced the pass")
    assert wait_until(lambda: peer.handed() == [mid], timeout=3.0)
    assert wait_until(lambda: mid not in queued(rig.daemon, REVIEWER_Q))
    assert peer.payloads()[0].get("held") is None, "the second read went live"
