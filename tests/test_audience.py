"""The audience mark, from the verb that writes it to the frame that shows it.

A send may say who its answer is for; the verb writes `[audience:<name>]` on line zero,
copies an ask's audience onto the reply, and refuses a reply that
would change it. The daemon fills an absent audience in from its own index at
`_deliver`, and the frame line and `moxi comms history` show it.

Everything here runs against the gateway seam, on the mock and on the local backend
(`BOTH_BACKENDS` in conftest), and never against the Discord implementation.
The parser's own grammar is pinned in `test_machine_line.py`.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest
from conftest import no_cold_at, queued, wait_for_queue, wait_until

from moxi_comms import constants, cursors, machine_line
from moxi_comms.client import DaemonOffline
from moxi_comms.adapters.claude_shim import frame_line
from moxi_comms.daemon import AUDIENCE_INDEX_SLACK, AUDIENCE_INDEX_UNKNOWN, Daemon
from moxi_comms.gateway import GatewayError

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")


def posted(rig):
    return rig.gateway.history(rig.names_channel)


def line_zero(msg):
    return machine_line.parse(msg.content)


def ask(rig, audience=None, text="question?"):
    """An ask from planner to reviewer, posted straight into the record."""
    return rig.post_from_slot(6, ["reviewer@proj-a"], text, display="planner@proj-a",
                              audience=audience)


@pytest.fixture
def reboot():
    """Restart the rig's daemon and own the new one (the `test_custody` pattern)."""
    started = []

    def _reboot(rig):
        rig.daemon.stop()
        rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
        started.append(rig.daemon)
        return rig.daemon

    yield _reboot
    for d in started:
        d.stop()


class ReadFails:
    """A gateway whose record read raises -- everything else is the real one."""

    def __init__(self, inner, error):
        self._inner = inner
        self._error = error

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_message(self, message_id):
        raise self._error


# --------------------------------------------------------------------- the verbs

def test_send_writes_the_audience_lowercased_onto_line_zero(rig):
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "for him",
                          "--audience", "Operator"], **PLANNER)
    assert code == 0, out
    (msg,) = posted(rig)
    assert no_cold_at(msg.content).splitlines()[0] == \
        "::to reviewer@proj-a [audience:operator] [no reply]"


def test_send_without_the_flag_is_byte_identical_to_before(rig):
    code, _ = rig.cli(["send", "reviewer@proj-a", "plain", "--posture", "once", "--new-name"],
                      **PLANNER)
    assert code == 0
    # The same bytes compose writes with no audience, the send's cold-at included.
    content = posted(rig)[0].content
    cold_at = machine_line.parse(content).cold_at
    assert cold_at is not None
    assert content == machine_line.compose(["reviewer@proj-a"], "plain", posture="once",
                                           cold_at=cold_at)


def test_a_malformed_audience_is_refused_before_anything_is_posted(rig):
    for bad in ("op erator", "", "a/b", "@x"):
        code, out = rig.cli(["send", "--posture", "none", "reviewer", "hi", "--audience", bad], **PLANNER)
        assert code == 2, bad
        assert "Nothing was sent" in out
    code, out = rig.cli(["reply", ask(rig).id, "hi", "--audience", "op erator"], **REVIEWER)
    assert code == 2 and "Nothing was sent" in out
    assert len(posted(rig)) == 1, "only the ask is in the record"


def test_reply_table_row_1_none_and_nothing_carries_none(rig):
    original = ask(rig)
    code, out = rig.cli(["reply", original.id, "answer."], **REVIEWER)
    assert code == 0, out
    reply = line_zero(posted(rig)[-1])
    assert (reply.reply_to, reply.audience) == (original.id, None)


def test_reply_table_row_2_a_reply_may_add_an_audience(rig):
    """the ask had none, the replier adds one."""
    original = ask(rig)
    code, out = rig.cli(["reply", original.id, "answer.", "--audience", "operator"],
                        **REVIEWER)
    assert code == 0, out
    assert line_zero(posted(rig)[-1]).audience == "operator"


def test_reply_table_row_3_the_asks_audience_is_copied(rig):
    """the replier passes nothing and the reply carries the ask's audience."""
    original = ask(rig, audience="operator")
    code, out = rig.cli(["reply", original.id, "answer."], **REVIEWER)
    assert code == 0, out
    assert no_cold_at(posted(rig)[-1].content).splitlines()[0] == \
        f"::to planner@proj-a re:{original.id} [audience:operator]"


def test_reply_table_row_4_the_same_audience_is_accepted(rig):
    original = ask(rig, audience="operator")
    code, out = rig.cli(["reply", original.id, "answer.", "--audience", "OPERATOR"],
                        **REVIEWER)
    assert code == 0, out
    assert line_zero(posted(rig)[-1]).audience == "operator"


def test_reply_table_row_5_a_different_audience_is_refused(rig):
    """a replier may not change the ask's audience. The refusal names the ask's
    audience, says a reply cannot change it, and names the cure: a new send."""
    original = ask(rig, audience="operator")
    code, out = rig.cli(["reply", original.id, "answer.", "--audience", "archivist"],
                        **REVIEWER)
    assert code == 2
    assert "'operator'" in out and "cannot change" in out
    assert "new message" in out and "Nothing was sent" in out
    assert posted(rig) == [original]


def test_send_reply_to_gets_the_same_table(rig):
    marked = ask(rig, audience="operator")
    plain = ask(rig)
    # copy
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "a.", "--reply-to",
                         marked.id], **REVIEWER)
    assert code == 0, out
    assert line_zero(posted(rig)[-1]).audience == "operator"
    # add
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "b.", "--reply-to", plain.id,
                         "--audience", "operator"], **REVIEWER)
    assert code == 0, out
    assert line_zero(posted(rig)[-1]).audience == "operator"
    # refuse
    before = len(posted(rig))
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "c.", "--reply-to", marked.id,
                         "--audience", "archivist"], **REVIEWER)
    assert code == 2 and "cannot change" in out
    assert len(posted(rig)) == before


def test_send_reply_to_a_referent_not_in_the_record_posts_as_typed(rig):
    """A well-formed id pointing at nothing is accepted by contract: there is
    nothing to copy, so the line posts exactly as typed."""
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "hi", "--reply-to",
                         "1400000009999", "--audience", "operator"], **REVIEWER)
    assert code == 0, out
    parsed = line_zero(posted(rig)[-1])
    assert (parsed.reply_to, parsed.audience) == ("1400000009999", "operator")
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "hi", "--reply-to",
                         "1400000009998"], **REVIEWER)
    assert code == 0, out
    assert line_zero(posted(rig)[-1]).audience is None


def test_a_failed_referent_read_refuses_in_both_verbs(rig):
    """A read that FAILED is not a referent that is missing: posting as typed would drop
    an audience the referent may carry, so both verbs refuse and say so."""
    original = ask(rig, audience="operator")
    broken = ReadFails(rig.gateway, GatewayError("500 on GET: the record is unreadable"))
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "hi", "--reply-to",
                         original.id], **REVIEWER, gateway=broken)
    assert code == 5 and "could not be read" in out and "Nothing was sent" in out
    code, out = rig.cli(["reply", original.id, "hi"], **REVIEWER, gateway=broken)
    assert code == 5 and "Nothing was sent" in out
    offline = ReadFails(rig.gateway, DaemonOffline("comms offline"))
    code, out = rig.cli(["send", "--new-name", "planner@proj-a", "hi", "--reply-to",
                         original.id], **REVIEWER, gateway=offline)
    assert code == 4 and "Nothing was sent" in out
    assert posted(rig) == [original]


def test_the_operator_lane_takes_the_same_flag(rig):
    if rig.backend != "local":
        pytest.skip("the credentialled operator lane exists on the local backend alone")
    reviewer = rig.shim(**REVIEWER)
    code, out = rig.cli(["send", "reviewer@proj-a", "ship it", "--audience", "operator"],
                        env={"MOXI_COMMS_OPERATOR": "900001"})
    assert code == 0, out
    assert no_cold_at(posted(rig)[-1].content).splitlines()[0] == \
        "::to reviewer@proj-a [audience:operator]"
    wait_for_queue(reviewer, 1)


# ------------------------------------------------- the index and the fill-in

def reply_to_planner(rig, referent_id, text="answer.", audience=None):
    """A reply typed by hand: `re:` on line zero, no audience unless one is typed."""
    return rig.post_from_slot(6, ["planner@proj-a"], text, display="reviewer@proj-a",
                              reply_to=referent_id, audience=audience)


def delivered(shim, message_id):
    wait_until(lambda: any(p["message_id"] == message_id for p in shim.queue))
    (p,) = [p for p in shim.queue if p["message_id"] == message_id]
    return p["audience"], p["audience_source"]


def test_a_written_audience_is_delivered_as_written(rig):
    reviewer = rig.shim(**REVIEWER)
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "for him", "--audience",
                          "operator"], **PLANNER)
    assert code == 0, out
    wait_for_queue(reviewer, 1)
    assert delivered(reviewer, posted(rig)[-1].id) == ("operator", "written")


def test_a_message_that_is_not_a_reply_and_has_no_audience_carries_none(rig):
    reviewer = rig.shim(**REVIEWER)
    rig.cli(["send", "--posture", "none", "reviewer@proj-a", "plain"], **PLANNER)
    wait_for_queue(reviewer, 1)
    assert delivered(reviewer, posted(rig)[-1].id) == (None, None)


def test_a_hand_typed_reply_inherits_the_asks_audience(rig):
    """the daemon fills the absence in from its index. The record is not
    rewritten: line zero stays what the sender typed."""
    planner = rig.shim(**PLANNER)
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id)
    assert delivered(planner, reply.id) == ("operator", "inherited")
    assert line_zero(rig.gateway.get_message(reply.id)).audience is None


def test_a_chain_of_hand_typed_replies_inherits_transitively(rig):
    planner = rig.shim(**PLANNER)
    original = ask(rig, audience="operator")
    first = reply_to_planner(rig, original.id, "first.")
    second = reply_to_planner(rig, first.id, "second.")
    third = reply_to_planner(rig, second.id, "third.")
    assert delivered(planner, third.id) == ("operator", "inherited")
    assert rig.daemon.audiences[second.id] == "operator", "stored effective, no walk"


def test_a_present_audience_is_never_overridden(rig):
    """The fill-in touches absence only. A hand-typed reply with its own, different
    audience is delivered as typed: the verb is where a change is refused, and the
    daemon polices no line zero."""
    planner = rig.shim(**PLANNER)
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id, audience="archivist")
    assert delivered(planner, reply.id) == ("archivist", "written")


def test_a_reply_to_a_referent_the_daemon_never_indexed_is_unknown(rig, caplog):
    planner = rig.shim(**PLANNER)
    with caplog.at_level(logging.INFO, logger="comms.daemon"):
        reply = reply_to_planner(rig, "1400000009999")
        assert delivered(planner, reply.id) == (None, "unknown")
    assert any("audience unknown" in r.getMessage() and "1400000009999" in r.getMessage()
               for r in caplog.records), "one log line naming the referent"


def test_a_reply_to_an_indexed_ask_with_no_audience_carries_none(rig):
    """The referent is KNOWN to carry no audience, which is a different fact from a
    referent this daemon cannot answer for: `unknown` is kept for the second."""
    planner = rig.shim(**PLANNER)
    original = ask(rig)
    reply = reply_to_planner(rig, original.id)
    assert delivered(planner, reply.id) == (None, None)


def test_register_replay_feeds_the_index_before_it_delivers(rig):
    """Register replays the record past the cursor, then releases the queue;
    both call `_deliver`, so the walk must index what it parses first. The reply
    to the unbound planner is a queue row, handed by the register's release.
    The index is emptied here to prove the ask was learned from the register's own walk."""
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id)
    wait_until(lambda: reply.id in rig.daemon.audiences)
    assert wait_until(lambda: reply.id in queued(rig.daemon, "planner@proj-a"))
    with rig.daemon._lock:
        rig.daemon.audiences.clear()
    planner = rig.shim(**PLANNER)
    payload = next(p for p in wait_for_queue(planner, 1) if p["message_id"] == reply.id)
    assert payload["backlog"] is False and payload["held"] is True, "released, not replayed"
    assert (payload["audience"], payload["audience_source"]) == ("operator", "inherited")


def test_the_boot_rescan_rebuilds_the_index(rig, reboot):
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id)
    plain = ask(rig)
    rig.daemon.stop()
    daemon = reboot(rig)
    assert daemon.audiences[original.id] == "operator"
    assert daemon.audiences[reply.id] == "operator"
    assert plain.id in daemon.audiences and daemon.audiences[plain.id] is None


def test_rescan_deliveries_carry_the_fill_in(rig, reboot):
    """The boot rescan routes the backlog through `_deliver` like everything else: a
    bound identity handed a pre-boot reply by the rescan gets the same fields."""
    original = ask(rig, audience="operator")
    rig.daemon.stop()
    reply = reply_to_planner(rig, original.id)      # lands while the daemon is down
    daemon = reboot(rig)
    planner = rig.shim(**PLANNER)
    payload = next(p for p in wait_for_queue(planner, 1) if p["message_id"] == reply.id)
    assert (payload["audience"], payload["audience_source"]) == ("operator", "inherited")
    # and the rescan pass itself, against a bound identity past nothing
    with daemon._lock:
        daemon.audiences.clear()
        daemon.bindings["planner@proj-a"].delivered.clear()
    daemon.rescan()
    wait_until(lambda: len([p for p in planner.queue if p["message_id"] == reply.id]) >= 2)
    again = [p for p in planner.queue if p["message_id"] == reply.id]
    assert (again[-1]["audience"], again[-1]["audience_source"]) == \
        ("operator", "inherited")


def test_a_defect_in_the_fill_in_costs_the_fields_never_the_delivery(rig, monkeypatch,
                                                                      caplog):
    planner = rig.shim(**PLANNER)

    def broken(msg, ml):
        raise RuntimeError("a defect in the fill-in")

    monkeypatch.setattr(rig.daemon, "_audience_of", broken)
    with caplog.at_level(logging.ERROR, logger="comms.daemon"):
        reply = reply_to_planner(rig, ask(rig, audience="operator").id)
        assert delivered(planner, reply.id) == (None, "unknown")
    assert any("audience fill-in failed" in r.getMessage() for r in caplog.records)


def test_the_index_is_bounded_to_the_rescan_window(rig):
    daemon = rig.daemon
    with daemon._lock:
        daemon.audiences.clear()
    top = 1_400_000_000_000 + 3 * constants.RESCAN_LIMIT
    for n in range(1_400_000_000_000, top):
        msg = SimpleNamespace(id=str(n))
        daemon._index_audience(msg, machine_line.parse("::to a [audience:operator]\nx"))
        assert len(daemon.audiences) <= constants.RESCAN_LIMIT + AUDIENCE_INDEX_SLACK
    assert str(top - 1) in daemon.audiences
    assert str(top - constants.RESCAN_LIMIT - AUDIENCE_INDEX_SLACK - 1) \
        not in daemon.audiences


def test_a_live_reply_racing_the_boot_rescan_delivers_as_unknown(rig, monkeypatch):
    """The declared boot race: the live event loop starts before the rescan, so a
    live reply to a pre-boot referent the rescan has not reached yet is delivered before
    its referent is indexed. It must say `unknown`, never a wrong audience, and nothing
    is dropped. Once the rescan has passed the referent, the next reply inherits."""
    original = ask(rig, audience="operator")
    rig.daemon.stop()
    # planner has handled everything up to the ask, so its own catch-up walks nothing
    # and cannot index the ask for it: only the rescan can.
    cursors.advance("planner@proj-a", original.id)

    gate = threading.Event()
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    rescan = daemon.rescan

    def held_rescan():
        gate.wait(10)
        rescan()

    monkeypatch.setattr(daemon, "rescan", held_rescan)
    rig.daemon = daemon
    booting = threading.Thread(target=daemon.start, daemon=True)
    booting.start()
    try:
        assert wait_until(lambda: daemon._sub is not None and daemon.bound)
        planner = rig.shim(**PLANNER)
        early = reply_to_planner(rig, original.id, "raced the rescan.")
        assert delivered(planner, early.id) == (None, "unknown")
    finally:
        gate.set()
        booting.join(10)
    assert daemon.audiences[original.id] == "operator"
    late = reply_to_planner(rig, original.id, "after the rescan.")
    assert delivered(planner, late.id) == ("operator", "inherited")


# ----------------------- the three-valued index

def ask_re(rig, referent_id, text="follow-up?", audience=None):
    """A hand-typed reply from planner to reviewer: `re:` on line zero, nothing else."""
    return rig.post_from_slot(6, ["reviewer@proj-a"], text, display="planner@proj-a",
                              reply_to=referent_id, audience=audience)


def test_an_ordinary_reply_chain_to_an_unmarked_ask_carries_no_mark_at_every_hop(rig):
    """An audience mark is shown only when it is needed. A referent known to carry no
    audience delivers no mark, and so does the reply to that reply."""
    planner = rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    original = ask(rig)
    first = reply_to_planner(rig, original.id)
    assert delivered(planner, first.id) == (None, None)
    second = ask_re(rig, first.id)
    payload = next(p for p in wait_for_queue(reviewer, 2) if p["message_id"] == second.id)
    assert (payload["audience"], payload["audience_source"]) == (None, None)
    assert "audience" not in frame_line(payload)
    assert rig.daemon.audiences[first.id] is None, "known none, not unknown"


def test_the_second_hop_of_a_phantom_referent_stays_unknown(rig, caplog):
    """A window edge or a phantom referent: B answers a referent this daemon never indexed and is
    `unknown`. C answers B, and must stay `unknown`, never decay into "no mark"."""
    planner = rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    first = reply_to_planner(rig, "1400000009999")
    assert delivered(planner, first.id) == (None, "unknown")
    assert rig.daemon.audiences[first.id] is AUDIENCE_INDEX_UNKNOWN
    with caplog.at_level(logging.INFO, logger="comms.daemon"):
        second = ask_re(rig, first.id)
        assert delivered(reviewer, second.id) == (None, "unknown")
    assert any("audience unknown" in r.getMessage() and first.id in r.getMessage()
               for r in caplog.records), "one log line naming the referent"
    third = reply_to_planner(rig, second.id, "third.")
    assert delivered(planner, third.id) == (None, "unknown"), "at every hop"


def test_the_second_hop_of_a_referent_older_than_the_window_stays_unknown(rig, reboot):
    """The same at boot: the ask sits just outside the rescan window, its reply just inside.
    The rescan indexes the reply as unknown, and a reply to the reply says so too."""
    original = ask(rig, audience="operator")
    first = reply_to_planner(rig, original.id)
    rig.daemon.stop()
    cursors.advance("planner@proj-a", first.id)
    cursors.advance("reviewer@proj-a", first.id)
    old = constants.RESCAN_LIMIT
    constants.RESCAN_LIMIT = 1                      # the window holds the reply only
    try:
        daemon = reboot(rig)
    finally:
        constants.RESCAN_LIMIT = old
    assert original.id not in daemon.audiences
    assert daemon.audiences[first.id] is AUDIENCE_INDEX_UNKNOWN
    reviewer = rig.shim(**REVIEWER)
    second = ask_re(rig, first.id)
    assert delivered(reviewer, second.id) == (None, "unknown")


def _boot_racing_the_rescan(rig, monkeypatch, *, snapshot_first):
    """Boot a daemon whose rescan is held; a live reply to a pre-boot operator ask lands
    while it is held. With `snapshot_first` the rescan's history read is taken before
    the reply lands, so the rescan never re-parses it."""
    original = ask(rig, audience="operator")
    rig.daemon.stop()
    cursors.advance("planner@proj-a", original.id)
    cursors.advance("reviewer@proj-a", original.id)
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    gate, snap = threading.Event(), {}
    real_rescan, real_history = daemon.rescan, rig.gateway.history

    def held_rescan():
        if snapshot_first:
            snap["msgs"] = real_history(daemon._names_channel(),
                                        limit=constants.RESCAN_LIMIT)
        else:
            snap["msgs"] = None
        gate.wait(10)
        if snap["msgs"] is None:
            real_rescan()
            return
        daemon.gateway = _Snapshot(rig.gateway, snap["msgs"])
        try:
            real_rescan()
        finally:
            daemon.gateway = rig.gateway

    monkeypatch.setattr(daemon, "rescan", held_rescan)
    rig.daemon = daemon
    booting = threading.Thread(target=daemon.start, daemon=True)
    booting.start()
    try:
        assert wait_until(lambda: daemon._sub is not None and daemon.bound and "msgs" in snap)
        planner = rig.shim(**PLANNER)
        reviewer = rig.shim(**REVIEWER)
        early = reply_to_planner(rig, original.id, "raced the rescan.")
        assert delivered(planner, early.id) == (None, "unknown")
    finally:
        gate.set()
        booting.join(10)
    assert daemon.audiences[original.id] == "operator", "the rescan reached the ask"
    return daemon, reviewer, early


class _Snapshot:
    """The real gateway, except that `history` answers from a snapshot taken earlier."""

    def __init__(self, inner, msgs):
        self._inner = inner
        self._msgs = msgs

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def history(self, *args, **kwargs):
        return list(self._msgs)


def test_the_second_hop_after_the_boot_race_stays_unknown(rig, monkeypatch):
    """The boot race: the rescan's snapshot predates the raced reply, so nothing ever
    re-parses it. A reply to the raced reply must be `unknown`, not "no mark", which
    would show an operator exchange as the receiver's own."""
    daemon, reviewer, early = _boot_racing_the_rescan(rig, monkeypatch,
                                                      snapshot_first=True)
    assert daemon.audiences[early.id] is AUDIENCE_INDEX_UNKNOWN
    follow = ask_re(rig, early.id)
    assert delivered(reviewer, follow.id) == (None, "unknown")


def test_a_rescan_that_reaches_the_raced_reply_heals_the_chain(rig, monkeypatch):
    """A recomputed known value replaces an unknown entry: when the rescan's
    snapshot holds the raced reply, it re-indexes it after the ask and the chain heals."""
    daemon, reviewer, early = _boot_racing_the_rescan(rig, monkeypatch,
                                                      snapshot_first=False)
    assert daemon.audiences[early.id] == "operator"
    follow = ask_re(rig, early.id)
    assert delivered(reviewer, follow.id) == ("operator", "inherited")


def _inherited_pair_whose_ask_aged_out(rig):
    """A carries `operator`; B, a hand-typed reply to A, inherited it. Then A leaves the
    index (the trim keeps the newest ids) while B stays."""
    planner = rig.shim(**PLANNER)
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id)
    assert delivered(planner, reply.id) == ("operator", "inherited")
    with rig.daemon._lock:
        del rig.daemon.audiences[original.id]
    return original, reply


def test_a_rescan_does_not_overwrite_a_known_inherited_value_once_the_ask_aged_out(rig):
    """A re-index never replaces a known value with unknown."""
    _, reply = _inherited_pair_whose_ask_aged_out(rig)
    old = constants.RESCAN_LIMIT
    constants.RESCAN_LIMIT = 1                      # the window holds the reply only
    try:
        rig.daemon.rescan()
    finally:
        constants.RESCAN_LIMIT = old
    assert rig.daemon.audiences[reply.id] == "operator"
    reviewer = rig.shim(**REVIEWER)
    follow = ask_re(rig, reply.id)
    assert delivered(reviewer, follow.id) == ("operator", "inherited")


def test_register_replay_does_not_overwrite_a_known_inherited_value(rig):
    """On its frequent path: a register whose cursor sits between the ask and the
    reply re-parses the reply without the ask, and must keep what the index holds."""
    original, reply = _inherited_pair_whose_ask_aged_out(rig)
    cursors.advance("reviewer@proj-a", original.id)
    marker = ask(rig, text="after the reply")       # a queue row for reviewer
    assert wait_until(lambda: marker.id in queued(rig.daemon, "reviewer@proj-a"))
    reviewer = rig.shim(**REVIEWER)
    payload = next(p for p in wait_for_queue(reviewer, 1) if p["message_id"] == marker.id)
    # Register replays past the cursor first, then releases the row: the marker's
    # release says the replay already walked past the reply
    assert payload["backlog"] is False and payload["held"] is True, \
        "the register released the row after its replay walked past the reply"
    assert rig.daemon.audiences[reply.id] == "operator"
    follow = ask_re(rig, reply.id)
    assert delivered(reviewer, follow.id) == ("operator", "inherited")


def test_a_reconnect_without_resume_feeds_the_index(rig):
    """The non-RESUME READY path (the event loop's rescan): an ask and its reply posted
    while the socket was down reach the index only through that rescan."""
    planner = rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    before = rig.daemon.rescans
    rig.gateway.disconnect()
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id)
    rig.gateway.reconnect(resume=False)
    assert wait_until(lambda: rig.daemon.rescans > before)
    assert delivered(planner, reply.id) == ("operator", "inherited")
    assert rig.daemon.audiences[reply.id] == "operator"
    follow = ask_re(rig, reply.id)
    assert delivered(reviewer, follow.id) == ("operator", "inherited")


def test_a_reconnect_without_resume_keeps_a_known_value_once_the_ask_aged_out(rig):
    """The non-RESUME READY path again, with the ask gone from the index and outside the
    window: the rescan re-parses the reply and keeps its inherited value. Reviewer is
    bound first, so no register replay puts the ask back into the index."""
    reviewer = rig.shim(**REVIEWER)
    _, reply = _inherited_pair_whose_ask_aged_out(rig)
    wait_for_queue(reviewer, 1)
    before = rig.daemon.rescans
    old = constants.RESCAN_LIMIT
    constants.RESCAN_LIMIT = 1                      # the window holds the reply only
    try:
        rig.gateway.disconnect()
        rig.gateway.reconnect(resume=False)
        assert wait_until(lambda: rig.daemon.rescans > before)
    finally:
        constants.RESCAN_LIMIT = old
    assert rig.daemon.audiences[reply.id] == "operator"
    follow = ask_re(rig, reply.id)
    assert delivered(reviewer, follow.id) == ("operator", "inherited")


def test_a_reindex_replaces_unknown_and_known_values_with_a_known_one(rig):
    """The rule's other half: a recomputed known value (tag or none) does replace."""
    daemon = rig.daemon
    msg = SimpleNamespace(id="1400000000500")
    reply = machine_line.parse("::to a re:1400000000400\nx")
    with daemon._lock:
        daemon.audiences.clear()
    daemon._index_audience(msg, reply)
    assert daemon.audiences[msg.id] is AUDIENCE_INDEX_UNKNOWN
    daemon.audiences["1400000000400"] = "operator"
    daemon._index_audience(msg, reply)
    assert daemon.audiences[msg.id] == "operator"
    daemon.audiences["1400000000400"] = None
    daemon._index_audience(msg, reply)
    assert daemon.audiences[msg.id] is None
    del daemon.audiences["1400000000400"]
    daemon._index_audience(msg, reply)
    assert daemon.audiences[msg.id] is None, "unknown never replaces known none either"


# ------------------------------------------------------------------ history

def history_row(out, message_id):
    (row,) = [ln for ln in out.splitlines() if ln.startswith(f"{message_id} ")]
    return row


def test_history_spells_out_a_written_audience_beside_re(rig):
    original = ask(rig, audience="operator")
    reply = rig.post_from_slot(6, ["planner@proj-a"], "answer.", display="reviewer@proj-a",
                               reply_to=original.id, audience="operator")
    code, out = rig.cli(["history", "planner@proj-a"])
    assert code == 0, out
    assert history_row(out, original.id).endswith("-> reviewer@proj-a audience: operator")
    assert history_row(out, reply.id).endswith(
        f"-> planner@proj-a re:{original.id} audience: operator")


def test_history_shows_an_audience_inherited_within_the_page(rig):
    original = ask(rig, audience="operator")
    first = reply_to_planner(rig, original.id, "first.")
    second = rig.post_from_slot(6, ["reviewer@proj-a"], "second.", display="planner@proj-a",
                                reply_to=first.id)
    code, out = rig.cli(["history", "planner@proj-a"])
    assert code == 0, out
    assert history_row(out, first.id).endswith(
        f"re:{original.id} audience: operator (inherited)")
    assert history_row(out, second.id).endswith(
        f"re:{first.id} audience: operator (inherited)")


def test_history_says_nothing_about_a_referent_outside_the_page(rig):
    """History reads the page it holds and fetches nothing: a referent outside it leaves
    the row showing line zero only, with nothing claimed about inheritance."""
    original = ask(rig, audience="operator")
    reply = reply_to_planner(rig, original.id)
    code, out = rig.cli(["history", "planner@proj-a", "--limit", "1"])
    assert code == 0, out
    assert not any(ln.startswith(f"{original.id} ") for ln in out.splitlines())
    row = history_row(out, reply.id)
    assert row.endswith(f"re:{original.id}") and "audience" not in row


def test_history_rows_without_an_audience_are_unchanged(rig):
    original = ask(rig)
    reply = reply_to_planner(rig, original.id)
    code, out = rig.cli(["history", "planner@proj-a"])
    assert "audience" not in out
    assert history_row(out, reply.id).endswith(f"re:{original.id}")
