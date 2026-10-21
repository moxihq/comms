"""The binding custody journal: replay attribution runs
the SAME match-gate live delivery runs, retroactively.

Before this, a chosen-tier agent's backlog arrived blanket-unverified -- the daemon knew
who held seat N, and that knowledge died with the socket. Chosen agents hand off across
sessions as normal workflow, so the exceptional case was the common one. The journal
makes custody durable, and nothing else changes: the gate is still two independent facts
agreeing, and every way of not knowing still lands on the display-derived label.

What this file pins is the whole mechanism and its edges -- the boot fence and its declared
residual, containment with no tolerance window, and the four downstream renderings that
now go quiet because the frames under them really were proven.
"""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest
from conftest import FakeClock, queued, wait_for_queue, wait_until

from moxi_comms import attribution, bindings, directory
from moxi_comms.daemon import Daemon
from moxi_comms.paths import bindings_db, directory_db


# --------------------------------------------------------------------------- helpers

def attributed(msg):
    """What a REPLAY/history reader makes of a record entry: `from_record`, no daemon."""
    db = directory.connect(directory_db(), read_only=True)
    try:
        return attribution.from_record(db, msg)
    finally:
        db.close()


def journal_rows():
    conn = bindings.connect(bindings_db(), read_only=True)
    try:
        return conn.execute("SELECT * FROM bindings ORDER BY id").fetchall()
    finally:
        conn.close()


def write_interval(**cols):
    """Forge an interval directly -- the only way to simulate a daemon that died."""
    conn = bindings.init(bindings_db())
    try:
        keys = ", ".join(cols)
        conn.execute(f"INSERT INTO bindings({keys}) "
                     f"VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
        conn.commit()
    finally:
        conn.close()


def dangling(rig, *, at_offset: float = -100.0, slot_id: int = 1,
             qualified: str = "planner@proj-a"):
    """A binding a daemon opened and never closed, because it died holding it."""
    name, _, scope = qualified.partition("@")
    write_interval(qualified=qualified, name=name, scope=scope or "global", tier="chosen",
                   slot_id=slot_id, adapter="claude-shim",
                   bound_at=bindings.stamp(rig.clock.t + at_offset))


@pytest.fixture
def reboot():
    """Restart the rig's daemon -- and OWN the new one, so a rebooted test does not leak
    a listening socket into the next. The rig's own teardown only knows about the daemon
    it started."""
    started = []

    def _reboot(rig, **kw):
        rig.daemon.stop()
        rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False,
                            **kw).start()
        started.append(rig.daemon)
        return rig.daemon

    yield _reboot
    for d in started:
        d.stop()


# ------------------------------------------------------------------ the two moments

def test_the_bind_opens_an_interval_and_the_release_closes_it(rig):
    """The daemon journals exactly what only the daemon witnesses, and nothing else."""
    t0 = rig.clock.t
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())

    row = journal_rows()[0]
    assert (row["qualified"], row["name"], row["scope"]) == \
        ("planner@proj-a", "planner", "proj-a")
    assert (row["tier"], row["slot_id"], row["adapter"]) == ("chosen", 1, "claude-shim")
    assert row["bound_at"] == bindings.stamp(t0)
    assert row["released_at"] is None, "an open interval is a LIVE binding"
    assert row["fenced"] == 0

    rig.clock.advance(30)
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)
    closed = journal_rows()[0]
    assert closed["released_at"] == bindings.stamp(t0 + 30)
    assert closed["fenced"] == 0, "a witnessed release is not a fence"


def test_the_timestamps_are_the_records_own_spelling(rig):
    """The record's own timestamp format, byte for byte -- containment is a string comparison, and two
    formats would make it a silent lie rather than a loud error."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    bound_at = journal_rows()[0]["bound_at"]
    assert len(bound_at) == 20 and bound_at.endswith("Z") and bound_at[10] == "T"
    msg = rig.post_from_slot(1, ["reviewer@proj-a"], "hi", display="planner@proj-a")
    assert len(msg.timestamp) == len(bound_at), "the same width, so <= sorts by time"


def test_an_ephemeral_binding_is_journalled_like_any_other(rig):
    """An `agent-N` is a binding. It gets an interval, and it verifies the same way."""
    ghost = rig.shim(slot=3)
    wait_until(lambda: ghost.identity == "agent-1")
    row = journal_rows()[0]
    assert (row["qualified"], row["tier"], row["slot_id"]) == ("agent-1", "ephemeral", 3)

    rig.clock.advance(5)
    msg = rig.post_from_slot(3, ["curator@proj-a"], "speaking", display="agent-1")
    author = attributed(msg)
    assert (author.qualified, author.tier, author.attribution) == \
        ("agent-1", "ephemeral", "verified")


def test_a_standing_binding_never_needs_the_journal_to_be_verified(rig):
    """Standing is webhook -> directory row and always was. The journal is not in it."""
    rig.shim(slot=1, name="main", scope="proj-a")
    rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a", "a note"], slot=1, name="main", scope="proj-a")
    msg = rig.gateway.history(rig.names_channel)[-1]
    assert msg.author_id == rig.agent_webhook_id("main", "proj-a"), "its OWN webhook"
    os.remove(bindings_db())                      # the journal cannot matter here
    author = attributed(msg)
    assert (author.tier, author.attribution) == ("standing", "verified")


# ------------------------------------------------------- the retroactive match-gate

def test_a_closed_interval_verifies_a_message_from_inside_it(rig):
    """The whole mechanism in one assertion: the session is OVER, the socket is gone, and
    the message it sent is still attributable because custody outlived both."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    rig.clock.advance(5)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "before I hand off",
                             display="planner@proj-a")
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)

    author = attributed(msg)
    assert (author.qualified, author.tier, author.attribution) == \
        ("planner@proj-a", "chosen", "verified")


def test_a_display_that_does_not_match_the_journalled_holder_is_unverified(rig):
    """The retroactive gate BITES. Slot 1 was planner's; an out-of-band spender of that
    slot's credential wearing another name gets what it always got -- the display, and
    the word that says the display is all it is."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    rig.clock.advance(5)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "not the holder speaking",
                             display="ghost@proj-a")
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)

    author = attributed(msg)
    assert (author.qualified, author.tier, author.attribution) == \
        ("ghost@proj-a", "chosen", "labeled")


def test_a_message_outside_every_interval_is_unverified_with_no_tolerance(rig):
    """No fudge factor at the edges. A message a second after the release is a message
    the journal cannot place, and the honest answer to that is the same as always."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    rig.clock.advance(5)
    inside = rig.post_from_slot(1, ["curator@proj-a"], "inside", display="planner@proj-a")
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)

    rig.clock.advance(1)                          # ONE second past the release
    after = rig.post_from_slot(1, ["curator@proj-a"], "after", display="planner@proj-a")

    assert attributed(inside).attribution == "verified"
    assert attributed(after).attribution == "labeled"
    assert attributed(after).qualified == "planner@proj-a", "the display still names them"


def test_a_message_before_the_bind_is_unverified_too(rig):
    """Containment has two edges and both are exact."""
    before = rig.post_from_slot(1, ["curator@proj-a"], "earlier", display="planner@proj-a")
    rig.clock.advance(5)
    rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    assert attributed(before).attribution == "labeled"


def test_a_seat_reused_by_a_second_holder_attributes_each_message_to_its_own_holder(rig):
    """Custody is an INTERVAL, not a current value: the same seat, two sessions, and the
    journal answers per message rather than per slot."""
    first = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    a = rig.post_from_slot(1, ["curator@proj-a"], "first tenant", display="planner@proj-a")
    rig.clock.advance(5)
    first.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)

    rig.clock.advance(5)
    rig.shim(slot=1, name="builder", scope="proj-a")
    wait_until(lambda: "builder@proj-a" in rig.daemon.bindings)
    rig.clock.advance(5)
    b = rig.post_from_slot(1, ["curator@proj-a"], "second tenant", display="builder@proj-a")

    assert (attributed(a).qualified, attributed(a).attribution) == \
        ("planner@proj-a", "verified")
    assert (attributed(b).qualified, attributed(b).attribution) == \
        ("builder@proj-a", "verified")
    # ...and neither can borrow the other's interval
    rig.clock.advance(5)
    crossed = rig.post_from_slot(1, ["curator@proj-a"], "wrong tenant",
                                 display="planner@proj-a")
    assert attributed(crossed).attribution == "labeled"


def test_one_name_across_two_seats_verifies_every_message_from_the_right_seat(rig):
    """The handoff shape, from the other side. The seat-reuse test walks two NAMES through
    one seat; this walks one NAME through two seats, which is what a chosen agent's day
    actually looks like: a session ends, a fresh one claims the same name, and `_pick_slot`
    hands it whatever seat is free. Custody is keyed on the SEAT, so the journal has to
    answer
    per (seat, instant) and still add up to one continuous identity for the reader."""
    first = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    a = rig.post_from_slot(1, ["curator@proj-a"], "from the first session",
                           display="planner@proj-a")
    # ...and at this same instant, the seat the NEXT session will hold is nobody's.
    # A message from it wearing the name must not borrow seat 1's interval.
    early = rig.post_from_slot(4, ["curator@proj-a"], "from a seat this name never held",
                               display="planner@proj-a")
    rig.clock.advance(5)
    first.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)

    rig.clock.advance(60)
    rig.shim(slot=4, name="planner", scope="proj-a")      # same name, a different seat
    wait_until(lambda: rig.daemon.bindings["planner@proj-a"].slot_id == 4)
    rig.clock.advance(5)
    b = rig.post_from_slot(4, ["curator@proj-a"], "from the second session",
                           display="planner@proj-a")

    for msg in (a, b):
        assert (attributed(msg).qualified, attributed(msg).attribution) == \
            ("planner@proj-a", "verified")
    assert {r["slot_id"] for r in journal_rows()} == {1, 4}, "two intervals, two seats"
    assert attributed(early).attribution == "labeled", \
        "custody is (seat, instant); holding a seat LATER proves nothing about earlier"


def test_a_name_that_moved_seats_replays_its_whole_history_verified(rig):
    """What the reader ends up holding: mail from BOTH sessions of one name, in one
    catch-up, every frame bare -- because every frame really was proven, each against
    the interval belonging to the seat it was sent from."""
    first = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "before the handoff"], slot=1, name="planner",
            scope="proj-a")
    rig.clock.advance(5)
    first.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)

    rig.clock.advance(600)
    rig.shim(slot=4, name="planner", scope="proj-a")
    wait_until(lambda: rig.daemon.bindings["planner@proj-a"].slot_id == 4)
    rig.clock.advance(5)
    rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "after the handoff"], slot=4, name="planner",
            scope="proj-a")

    rig.clock.advance(3600)
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    wait_for_queue(reviewer, 2)
    wait_until(lambda: reviewer.replaying is False)
    assert [p["author"]["attribution"] for p in reviewer.queue] == ["verified", "verified"]

    digest = reviewer.on_turn_boundary()
    assert "unverified" not in digest, "two sessions, one name, nothing to warn about"
    assert digest.count("[comms] from planner@proj-a · msg ") == 2


def test_two_overlapping_intervals_for_one_seat_degrade_to_unverified(rig):
    """The daemon cannot produce this; a corrupt or hand-edited journal can. Two claims
    on one seat cannot both be true and the journal cannot say which is, so it says
    nothing -- ambiguity degrades toward unverified, never toward a false verified."""
    rig.daemon.stop()
    dangling(rig, at_offset=-100)
    dangling(rig, at_offset=-50)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "which one", display="planner@proj-a")
    assert attributed(msg).attribution == "labeled"


# --------------------------------------------------------------------- the boot fence

def test_the_boot_fence_closes_what_a_crash_left_open(rig, reboot):
    """While the daemon is down no new binding can exist and no send can resolve, so
    nothing competed for the seat in the gap. The fence records that the interval ENDED
    without a witnessed release rather than pretending it ran to now."""
    rig.daemon.stop()
    t = rig.clock.t
    dangling(rig)
    assert journal_rows()[0]["released_at"] is None

    reboot(rig)
    fenced = journal_rows()[0]
    assert fenced["released_at"] == bindings.stamp(t), "closed at BOOT time"
    assert fenced["fenced"] == 1, "and marked as closed by machinery, not by a release"


def test_a_message_inside_the_fenced_window_verifies_and_one_after_it_does_not(rig, reboot):
    """The fence's declared residual, accepted with its eyes open: a message from the
    gap verifies against the fenced tail. That is the SAME trust-local caveat live
    attribution carries -- whoever holds a credential can wear the holder's name --
    and the journal was never the thing that was going to fix it. What the fence DOES
    buy is the edge: one second past boot and the seat has no holder again."""
    rig.daemon.stop()
    dangling(rig)
    in_gap = rig.post_from_slot(1, ["curator@proj-a"], "sent during the outage",
                                display="planner@proj-a")

    rig.clock.advance(1)
    reboot(rig)
    rig.clock.advance(1)
    after = rig.post_from_slot(1, ["curator@proj-a"], "sent after the fence",
                               display="planner@proj-a")

    assert attributed(in_gap).attribution == "verified"
    assert attributed(after).attribution == "labeled"


def test_the_fence_leaves_witnessed_releases_alone(rig, reboot):
    """It closes what is OPEN. A cleanly released interval is already history, and a
    fence that rewrote it would move a real release time to a boot time."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)
    closed_at = journal_rows()[0]["released_at"]

    rig.clock.advance(100)
    reboot(rig)
    row = journal_rows()[0]
    assert (row["released_at"], row["fenced"]) == (closed_at, 0)


def test_a_clean_stop_leaves_nothing_for_the_fence_to_do(rig, reboot):
    """The common case: `stop()` closes every connection, each deregistration journals
    its release, and the next boot finds no dangling interval at all."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    reboot(rig)
    row = journal_rows()[0]
    assert row["released_at"] is not None, "the release was witnessed, not inferred"
    assert row["fenced"] == 0, "so the fence had nothing to do"


def test_a_start_the_probe_refuses_never_reaches_the_fence(rig):
    """The fence is an unscoped `UPDATE ... WHERE released_at IS NULL`: run by an
    intruder against a LIVE daemon's journal it closes that daemon's open intervals,
    turns its own later release into a silent no-op, and stamps the row `fenced=1` --
    after which `holder_at` answers None for everything that seat posts. Permanent
    falsification of the evidence. So the probe comes first, and a start it refuses has
    not written a thing."""
    dangling(rig)
    assert journal_rows()[0]["released_at"] is None

    intruder = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    with pytest.raises(RuntimeError, match="live comms daemon"):
        intruder.start()

    row = journal_rows()[0]
    assert row["released_at"] is None, "the live daemon's open interval survives"
    assert row["fenced"] == 0, "and nothing was stamped as machinery-closed"


# -------------------------------------------------------- failure is never fatal

def test_a_missing_journal_costs_verification_and_nothing_else(rig):
    """Today's behaviour, everywhere. The journal is evidence; delivery is the product."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "hi", display="planner@proj-a")
    assert attributed(msg).attribution == "verified"

    planner.stop()
    rig.daemon.stop()
    os.remove(bindings_db())
    assert bindings.holder_at(1, msg.timestamp) is None
    author = attributed(msg)
    assert (author.qualified, author.tier, author.attribution) == \
        ("planner@proj-a", "chosen", "labeled")


def test_an_unreadable_journal_is_the_absence_of_evidence_not_an_error(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "hi", display="planner@proj-a")
    path = bindings_db()
    original = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, 0o000)
    try:
        assert bindings.holder_at(1, msg.timestamp) is None
        assert attributed(msg).attribution == "labeled"
    finally:
        os.chmod(path, original)
    assert attributed(msg).attribution == "verified", "and it comes back when it can"


def test_the_daemon_boots_and_binds_with_a_journal_it_cannot_open(rig, reboot, tmp_path):
    """A daemon that refused to run because a history file would not open would be
    trading the product for its evidence."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    daemon = reboot(rig, journal_path=blocker / "comms" / "bindings.db")
    assert daemon.journal._conn is None
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    assert planner.identity == "planner@proj-a", "the binding is real, recorded or not"
    assert daemon.bindings["planner@proj-a"].journal_id is None


def test_a_failed_write_is_logged_and_the_binding_proceeds(rig, caplog):
    """The write fails mid-run (a full disk, a revoked mode). The bind still happens and
    the daemon says so in the log rather than in the agent's face."""
    import logging

    class Broken:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            pass

    caplog.set_level(logging.DEBUG)
    rig.daemon.journal._conn.close()
    rig.daemon.journal._conn = Broken()
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    assert planner.identity == "planner@proj-a"
    assert rig.daemon.bindings["planner@proj-a"].journal_id is None
    assert "failed to record the bind" in caplog.text
    planner.stop()          # ...and the release failure is survivable too
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)


# ------------------------------------------------------------------ file posture

def test_the_journal_is_data_beside_the_directory_and_it_is_0600(rig):
    assert bindings_db() == directory_db().parent / "bindings.db"
    assert "state" not in bindings_db().parts, "DATA: it interprets a device-free record"
    assert stat.S_IMODE(bindings_db().stat().st_mode) == 0o600


def test_a_read_only_consumer_reads_while_the_daemon_holds_the_file(rig):
    """WAL, stated: a rollback journal would hand a concurrent reader a lock error at
    exactly the moment a bind is being written, and an attribution read that fails is an
    attribution read that lies by omission."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    wait_until(lambda: journal_rows())
    conn = bindings.connect(bindings_db(), read_only=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert [r["qualified"] for r in
                conn.execute("SELECT qualified FROM bindings")] == ["planner@proj-a"]
        # a SECOND bind lands while this read-only connection is open, and it sees it
        rig.shim(slot=2, name="reviewer", scope="proj-a")
        wait_until(lambda: len(journal_rows()) == 2)
        assert conn.execute("SELECT count(*) FROM bindings").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM bindings")       # read-only means read-only
    finally:
        conn.close()


def test_the_journal_is_never_asked_about_routing(rig):
    """History is not a seat. A journalled identity that is not bound is not addressable,
    and no amount of custody evidence may say otherwise."""
    ghost = rig.shim(slot=3)
    wait_until(lambda: ghost.identity == "agent-1")
    ghost.stop()
    wait_until(lambda: "agent-1" not in rig.daemon.bindings)
    assert journal_rows()[0]["qualified"] == "agent-1", "the custody row outlives it"

    code, out = rig.cli(["send", "--posture", "none", "agent-1", "are you there"], slot=1, name="planner",
                        scope="proj-a")
    assert code != 0 and "no such ephemeral identity" in out
    assert rig.daemon.resolve("agent-1").binding is None


# ------------------------------------------------- interplay 1: the silent defaults

def test_a_journal_verified_chosen_replay_frame_renders_BARE(rig):
    """Silence means chosen and verified. Without the journal a chosen frame read back
    from the record could never earn that silence; with it, it can, and the digest is
    quiet because the batch really was proven rather than because anyone stopped
    checking. Mail to an offline name is a queue row, released at the bind with
    `held: true`, not replayed (design §3.3.4, §3.5, §3.7); the release reads the record
    and the journal as replay did."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "look at this while you were out"], slot=1,
            name="planner", scope="proj-a")
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)

    rig.clock.advance(3600)
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    wait_for_queue(reviewer, 1)
    wait_until(lambda: reviewer.replaying is False)
    assert reviewer.queue[0]["held"] is True, "it arrives as a release (§3.5)"
    assert reviewer.queue[0]["backlog"] is False
    assert reviewer.queue[0]["author"] == {"name": "planner@proj-a", "tier": "chosen",
                                           "attribution": "verified"}
    digest = reviewer.on_turn_boundary()
    assert digest.splitlines()[0] == "== comms: 1 message =="
    assert "unverified" not in digest, "the all-verified release stays silent"
    assert "[comms] from planner@proj-a · msg" in digest
    assert "[held] sent " in digest and ", waited 1h" in digest, "§3.7: it says it waited"


def test_the_catch_up_note_appears_only_when_frames_GENUINELY_are_unverified(rig):
    """The header's note is computed from the payloads' own attribution, so the journal
    moving one frame to verified moves the header with it. Slot 6 is nobody's binding in
    any interval, so its frame stays unverified -- and one proven frame beside it is now
    enough to take the note away entirely: a blanket note over a batch that
    is not uniform would blame replay for the frame the journal proved, and the
    suppression under it would render proven and unproven identically. Mixed means every
    frame speaks for itself."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "journalled"], slot=1, name="planner", scope="proj-a")
    rig.post_from_slot(6, ["reviewer@proj-a"], "unjournalled", display="ghost@proj-a")
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)
    # The daemon routes each post on its own thread. A register between the two routings
    # would release the first, `held`, and replay the second alone as backlog: a batch of
    # one unverified frame, not the mixed one this test is about.
    assert wait_until(lambda: len(queued(rig.daemon, "reviewer@proj-a")) == 2)

    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    wait_for_queue(reviewer, 2)
    wait_until(lambda: reviewer.replaying is False)
    by_name = {p["author"]["name"]: p["author"]["attribution"] for p in reviewer.queue}
    assert by_name == {"planner@proj-a": "verified", "ghost@proj-a": "labeled"}, \
        "the header must be computed from a batch that really is mixed"

    digest = reviewer.on_turn_boundary()
    assert "attribution is unverified in replay" not in digest, \
        "the journal proved one of these two; a blanket note would misdescribe it"
    assert "[comms] from planner@proj-a · msg" in digest, "proven: silence is earned"
    assert "[comms] from ghost@proj-a (unverified) · msg" in digest, "unproven: marked"


# ------------------------------------------------------- interplay 2: the directors

def test_a_chosen_directors_backlog_keeps_directive_standing(rig):
    """The biggest functional win of the journal. A declared CHOSEN director sends while
    the recipient is offline; the director's session then ends. On rebind the replayed
    frame is verified against the journal, so the authority the recipient granted still
    applies -- where before it silently degraded to information standing every time.
    The offline mail is a queue row released at the bind (design §3.3.4, §3.5); the
    release attributes it from the record and the journal as replay did."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    code, _ = rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "rebase onto main"], slot=1,
                      name="planner", scope="proj-a")
    assert code == 0
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)

    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a",
                        directors=["planner@proj-a"])
    wait_for_queue(reviewer, 1)
    wait_until(lambda: reviewer.replaying is False)
    assert reviewer.queue[0]["held"] is True, "it arrives as a release (§3.5)"
    assert reviewer.queue[0]["backlog"] is False
    assert reviewer.queue[0]["author"]["attribution"] == "verified"
    digest = reviewer.on_turn_boundary()
    assert "directive from planner@proj-a · your director" in digest
    assert "information standing" not in digest
    assert "rebase onto main" in digest


def test_an_unjournalled_directors_backlog_still_degrades_and_says_why(rig):
    """The gate did not get softer. A director whose custody nothing witnessed is a
    director this message cannot be tied to, and the frame spends a sentence saying so."""
    rig.post_from_slot(6, ["reviewer@proj-a"], "do this", display="planner@proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a",
                        directors=["planner@proj-a"])
    wait_for_queue(reviewer, 1)
    wait_until(lambda: reviewer.replaying is False)
    digest = reviewer.on_turn_boundary()
    assert "directive" not in digest
    assert "attribution is unverified — information standing" in digest


# --------------------------------------------------------- interplay 3: the history

def test_history_renders_a_journal_verified_row_as_chosen_verified(rig):
    """`comms history` is the operator's audit surface and spells both words out. It
    reads the record through the same `from_record` everything else does, so it gets the
    journal for free -- there is no real-only path that could skip it."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a", "from a journalled seat"], slot=1, name="planner",
            scope="proj-a")
    rig.post_from_slot(6, ["curator@proj-a"], "from an unjournalled one",
                       display="ghost@proj-a")
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: journal_rows()[0]["released_at"] is not None)

    code, out = rig.cli(["history", "curator@proj-a"], slot=2)
    assert code == 0
    assert "planner@proj-a (chosen, verified)" in out
    assert "ghost@proj-a (chosen, unverified)" in out


def test_reply_names_the_journal_verified_author_as_verified(rig):
    """`comms reply` reads the record too, and it used to be the plainest demonstration
    that replay attribution was blanket-unverified for chosen agents."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    rig.clock.advance(5)
    rig.cli(["send", "--posture", "once", "reviewer@proj-a", "question?"], slot=1, name="planner", scope="proj-a")
    wait_for_queue(reviewer, 1)
    mid = reviewer.queue[0]["message_id"]
    rig.clock.advance(5)
    planner.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)

    code, out = rig.cli(["reply", mid, "answer."], slot=2, name="reviewer", scope="proj-a")
    assert code == 0
    assert "replying to planner@proj-a (chosen, verified)" in out


def test_the_mock_and_the_daemon_agree_because_it_is_one_code_path(rig):
    """Live and replay now answer the same question. The daemon's live shortcut reads
    memory and `from_record` reads the journal, and on the same message they must not
    disagree -- that is what makes the invariant one invariant."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "same message",
                             display="planner@proj-a")
    live = rig.daemon.attribute(msg, live=True)
    replayed = rig.daemon.attribute(msg, live=False)
    assert (live.qualified, live.tier, live.attribution) == \
        (replayed.qualified, replayed.tier, replayed.attribution)
    assert replayed.attribution == "verified"


# --------------------------------------------------------------- the reader's edges

@pytest.mark.parametrize("slot_id,timestamp", [
    (None, "2026-08-09T00:00:00Z"), (1, None), (1, ""),
])
def test_the_reader_answers_nothing_rather_than_guessing(rig, slot_id, timestamp):
    assert bindings.holder_at(slot_id, timestamp) is None


def test_the_journal_survives_a_daemon_restart_and_keeps_its_history(rig, reboot):
    """Durable is the point. A second daemon reads what the first one wrote."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    rig.clock.advance(5)
    msg = rig.post_from_slot(1, ["curator@proj-a"], "hi", display="planner@proj-a")
    rig.clock.advance(5)
    planner.stop()

    reboot(rig)
    assert len(journal_rows()) == 1
    assert attributed(msg).attribution == "verified"


def test_a_fresh_clock_does_not_confuse_the_stamp():
    """`stamp` is UTC and second-resolution, and nothing else."""
    clock = FakeClock(1_786_233_600.0)            # 2026-08-09T00:00:00Z
    assert bindings.stamp(clock()) == "2026-08-09T00:00:00Z"
    assert bindings.stamp(clock() + 0.9) == "2026-08-09T00:00:00Z", "truncated, not rounded"
