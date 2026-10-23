"""The one queue: rows, release points, settle, the readers' rules, the
migration, the trim, and the send's cold-at at the daemon.

Every rig test here runs on both backends. The daemon's cache read is set per name by the
`reads` fixture (it stands in for `cache.clock_state`, so the daemon's own read, its
exception handling and the verb's `session` answer all see it). Bindings are bare
`DaemonClient`s (`Peer`) that record what the daemon pushes and send the adapters'
`activity` frames raw, so these tests do not depend on which adapter code sends them; the
Claude shim's own frames are tested in `test_shim_events.py`, and against this daemon in
section (h), which needs the current shim.

Sections: (a) a held id survives, and settle, (b) no piggyback, (c) the release table, (d) re-hand,
(e) the readers, (f) failed hand-off, (g) migration and trim, (h) the Claude shim end to
end, (j) the send's cold-at at the daemon, (k) adapters started before the deploy, (l) a
row whose message the record lost.
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest
from conftest import projects_root, queued, record_rows, wait_until, write_config

from moxi_comms import cache, constants, cursors, machine_line
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import META_QUEUE_MIGRATED, Daemon
from moxi_comms.mock_gateway import NO_MENTIONS
from moxi_comms.paths import daemon_socket


def pytest_generate_tests(metafunc):
    """Both backends, as the suites in conftest's BOTH_BACKENDS run."""
    if "backend" in metafunc.fixturenames:
        metafunc.parametrize("backend", ["mock", "local"], indirect=True)


PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
REVIEWER_Q = "reviewer@proj-a"
CODER = dict(slot=5, name="coder", scope="proj-a", adapter="codex")
CODER_Q = "coder@proj-a"
SENTINEL_Q = "main@proj-b"          # a standing name nothing binds: its rows mark a pass
HOUR = 3600



# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def reads(monkeypatch):
    """The daemon's cache read, per qualified name: `table[q] = kind` (warm, cold,
    unknown, fresh, no-cache) or an exception to raise. A name not in the table reads
    as the real clock reads it."""
    table: dict = {}
    real = cache.clock_state

    def clock_state(adapter, session_id, **kw):
        kind = table.get(kw.get("name"))
        if kind is None:
            return real(adapter, session_id, **kw)
        if isinstance(kind, BaseException):
            raise kind
        if kind == cache.WARM:
            return cache.Clock(cache.WARM, age_seconds=60.0, ttl_seconds=HOUR)
        if kind == cache.COLD:
            return cache.Clock(cache.COLD, age_seconds=2 * HOUR, ttl_seconds=HOUR)
        if kind == cache.UNKNOWN:
            return cache.Clock(cache.UNKNOWN, reason="the rig says unknown")
        return cache.Clock(kind)

    monkeypatch.setattr(cache, "clock_state", clock_state)
    return table


class Peer:
    """One binding on a bare daemon connection: it records what the daemon pushes and
    sends an adapter's `activity` frames raw. Every frame is followed by a `ping` on the
    same connection, which the daemon answers only after it has handled the frame (one
    connection, one serving thread), so a test reads the outcome without sleeping."""

    def __init__(self, rig, *, slot, name, scope=None, global_=False,
                 adapter="claude-shim", holding=None):
        self.rig = rig
        self.frames: list = []
        self.client = DaemonClient(daemon_socket(), on_push=self.frames.append).connect()
        kw = dict(slot=slot, name=name, adapter=adapter, holding=holding)
        if global_:
            kw["global_"] = True
        else:
            kw["scope"] = scope
        if adapter == "claude-shim":
            kw.update(session_id=str(uuid.uuid4()), projects_root=str(projects_root()),
                      turned=True)
        elif adapter in ("codex", "opencode"):
            kw["report"] = {"session_id": f"thread-{uuid.uuid4()}", "turned": True}
        answer = self.client.register(**kw)
        self.q = answer["identity"]["qualified"]
        self.sync()

    def sync(self) -> None:
        self.client.request("ping")

    def payloads(self) -> list:
        return [f["payload"] for f in self.frames if f.get("op") == "deliver"]

    def handed(self) -> list:
        return [p["message_id"] for p in self.payloads()]

    def held(self) -> list:
        return [p["message_id"] for p in self.payloads() if p.get("held") is True]

    def activity(self, **fields) -> None:
        self.client.notify("activity", identity=self.q, **fields)
        self.sync()

    def turn(self, handled=(), unhandled=()) -> None:
        self.activity(kind="turn", turned=True, handled=[str(i) for i in handled],
                      unhandled=[str(i) for i in unhandled])

    def ready(self, source) -> None:
        self.activity(kind="ready", **({"source": source} if source is not None else {}))

    def close(self) -> None:
        self.client.close()
        wait_until(lambda: self.q not in self.rig.daemon.bindings)


class ShimPeer(Peer):
    """A real ClaudeShim of the rig, read and driven as a Peer: the payloads are the
    shim's own queue, and the frames go on the shim's own connection."""

    def __init__(self, rig, **kw):
        self.rig = rig
        self.shim = rig.shim(**kw)
        wait_until(lambda: not self.shim.replaying)
        self.client = self.shim._client
        self.q = self.shim.identity
        self.sync()

    def payloads(self) -> list:
        return list(self.shim.queue)

    def close(self) -> None:
        self.shim.stop()
        wait_until(lambda: self.q not in self.rig.daemon.bindings)


@pytest.fixture
def peers(rig):
    made: list = []

    def make(kind="claude-shim", **kw):
        if kind == "shim":
            p = ShimPeer(rig, **(kw or REVIEWER))
        elif kind == "codex":
            p = Peer(rig, **{**CODER, **kw})
        else:
            p = Peer(rig, **{**REVIEWER, **kw})
        made.append(p)
        return p

    yield make
    for p in made:
        if p.client is not None:
            p.client.close()


@pytest.fixture
def reboot(rig):
    started: list = []

    def _reboot():
        rig.daemon.stop()
        rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
        started.append(rig.daemon)
        return rig.daemon

    yield _reboot
    for d in started:
        d.stop()


@pytest.fixture
def blocked(rig):
    """Routing off while the block holds: what is posted then reaches the record with no
    row and no delivery, so replay and a rescan are the first readers to find it. The
    block lifts only once the daemon has seen every message posted under it."""
    class Block:
        def __enter__(self):
            write_config(None, "block = true\n")

        def __exit__(self, *exc):
            top = int(rig.gateway.max_message_id() or 0)
            try:
                assert wait_until(lambda: int(rig.daemon._seen_max) >= top)
            finally:
                write_config(None, "block = false\n")

    return Block()


def post(rig, recipients, text="hello", *, held=False, cold_at=None, raw=None):
    """One message through slot 6's #names webhook, line zero composed here: `held`
    writes `[held]`, `cold_at` the send's `[cold-at:…]`. `raw` posts that body as it is."""
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(6, "planner@proj-a", "global"))
    finally:
        db.close()
    body = raw if raw is not None else machine_line.compose(
        list(recipients), text, next_turn=held, cold_at=cold_at)
    return rig.gateway.execute_webhook(cred, body, username="planner@proj-a",
                                       allowed_mentions=NO_MENTIONS).id


def drain(rig) -> None:
    """Wait until the daemon has routed everything posted so far: routing is one ordered
    pass, so once a message posted now has its row, every earlier one is decided."""
    mid = post(rig, [SENTINEL_Q], "sentinel")
    assert wait_until(lambda: mid in queued(rig.daemon, SENTINEL_Q))


def row_for(rig, q, mid) -> None:
    assert wait_until(lambda: mid in queued(rig.daemon, q)), f"no row for {mid} ({q})"


def rows(rig, q) -> set:
    rig.daemon._queue_flush()
    return queued(rig.daemon, q)


def send(rig, to, text, *flags, sender=PLANNER):
    return rig.cli(["send", "--posture", "none", to, text, *flags], **sender)


def sent_id(out) -> str:
    import re
    return re.search(r"^sent (\d+) ", out, re.M).group(1)


def whole_seconds(rig) -> int:
    """Put the shared clock on a whole second, so a cold-at can equal it exactly."""
    rig.clock.t = float(int(rig.clock.t))
    return int(rig.clock.t)


# ============================================ (a) a held id survives, and settle

def test_a_held_id_the_cursor_passed_survives_a_restart_and_goes_at_the_next_turn(
        rig, reads, peers, reboot):
    """`--held` to warm reviewer, then a live message; the turn that
    handles the live one moves the cursor past the held id; the daemon restarts; the held
    id is still a row and goes at reviewer's next turn."""
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    code, out = send(rig, REVIEWER_Q, "when you are next about", "--held")
    assert code == 0, out
    held = sent_id(out)
    row_for(rig, REVIEWER_Q, held)
    code, out = send(rig, REVIEWER_Q, "now")
    assert code == 0, out
    live = sent_id(out)
    assert wait_until(lambda: live in peer.handed())
    cursors.advance(REVIEWER_Q, live)              # the adapter's cursor write
    peer.turn(handled=[live])
    assert int(cursors.read(REVIEWER_Q)) > int(held)
    assert rows(rig, REVIEWER_Q) == {held}
    peer.client.close()
    daemon = reboot()
    assert queued(daemon, REVIEWER_Q) == {held}, "the boot load kept the row"
    again = peers()
    assert again.handed() == [], "a register holds flagged rows"
    again.turn()
    assert again.held() == [held]
    again.turn(handled=[held])
    assert rows(rig, REVIEWER_Q) == set()
    assert record_rows(rig.gateway, REVIEWER_Q) == set()


def test_only_a_turn_naming_the_id_handled_deletes_its_row(rig, reads, peers, reboot):
    """The held id is handed after the cursor already passed it. A `turn`
    with `handled` empty, the binding's exit and a daemon restart each leave the row; only
    a `turn` that names it handled deletes it."""
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    held = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, held)
    live = post(rig, [REVIEWER_Q], "now")
    assert wait_until(lambda: live in peer.handed())
    cursors.advance(REVIEWER_Q, live)
    peer.ready("clear")                             # hands the row past the cursor
    assert peer.held() == [held]
    peer.turn()                                     # nothing handled
    assert rows(rig, REVIEWER_Q) == {held}
    peer.close()                                    # the binding ends
    assert rows(rig, REVIEWER_Q) == {held}
    assert record_rows(rig.gateway, REVIEWER_Q) == {held}
    reboot()
    assert queued(rig.daemon, REVIEWER_Q) == {held}
    again = peers()
    again.ready("startup")
    assert again.held() == [held]
    again.turn(handled=[held])
    assert rows(rig, REVIEWER_Q) == set()
    assert record_rows(rig.gateway, REVIEWER_Q) == set()


def test_a_handled_report_for_an_id_this_binding_was_not_handed_is_ignored(
        rig, reads, peers, caplog):
    """Settle acts only on ids the binding was handed; another is logged."""
    reads[REVIEWER_Q] = cache.COLD                # the frame's release hands nothing
    peer = peers()
    held = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, held)
    with caplog.at_level(logging.INFO, logger="comms.daemon"):
        peer.turn(handled=[held])
    assert rows(rig, REVIEWER_Q) == {held}
    assert any(held in r.getMessage() and "which it was not handed" in r.getMessage()
               for r in caplog.records)


# ============================================================ (b) no piggyback

def test_a_live_message_with_rows_waiting_hands_only_itself(rig, reads, peers):
    """A flagged row and a row queued while cold both wait while a
    live message to the name goes alone."""
    reads[REVIEWER_Q] = cache.COLD
    peer = peers()
    cold = post(rig, [REVIEWER_Q], "while cold")
    row_for(rig, REVIEWER_Q, cold)
    reads[REVIEWER_Q] = cache.WARM
    flagged = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, flagged)
    live = post(rig, [REVIEWER_Q], "now")
    assert wait_until(lambda: live in peer.handed())
    peer.sync()
    assert peer.handed() == [live]
    assert peer.payloads()[0].get("held") is None
    assert rows(rig, REVIEWER_Q) == {cold, flagged}


# ============================================================ (c) the release table

#: The release table, one row each: (event, the read at it, what it releases).
RELEASE_TABLE = [
    ("ready startup", cache.COLD, "all"),
    ("ready clear", cache.COLD, "all"),
    ("ready compact", cache.COLD, "all"),
    ("ready resume", cache.WARM, "all"),
    ("ready resume", cache.COLD, "none"),
    ("ready fork", cache.WARM, "all"),
    ("ready fork", cache.COLD, "none"),
    ("ready other", cache.WARM, "all"),
    ("ready none", cache.WARM, "none"),
    ("turn", cache.WARM, "all"),
    ("turn", cache.COLD, "none"),
    ("turn none", cache.WARM, "none"),
    ("register", cache.WARM, "unflagged"),
    ("register", cache.COLD, "none"),
    ("no kind", cache.WARM, "none"),
]


@pytest.mark.parametrize("kind", ["shim", "codex"], ids=["claude-shim", "harness"])
@pytest.mark.parametrize("event,read,releases", RELEASE_TABLE,
                         ids=[f"{e}-{r}" for e, r, _ in RELEASE_TABLE])
def test_the_release_table(rig, reads, peers, kind, event, read, releases):
    """One test per row of the release table, on the Claude shim and a harness double.
    Each name has a flagged row and an unflagged one (queued while cold, or while
    unbound for the register rows). `fork` and any other source read as resume; a `ready` with no source, a `turn` with no `handled` and an `activity` with
    no kind release nothing."""
    q = REVIEWER_Q if kind == "shim" else CODER_Q
    if event == "register":
        plain = post(rig, [q], "while away")
        flagged = post(rig, [q], "later", held=True)
        row_for(rig, q, plain)
        row_for(rig, q, flagged)
        reads[q] = read
        peer = peers(kind)
    else:
        reads[q] = cache.COLD
        peer = peers(kind)
        plain = post(rig, [q], "while cold")
        row_for(rig, q, plain)
        flagged = post(rig, [q], "later", held=True)
        row_for(rig, q, flagged)
        reads[q] = read
        verb, _, arg = event.partition(" ")
        if verb == "ready":
            peer.ready({"none": None, "other": "a-source-this-daemon-does-not-know"}
                       .get(arg, arg))
        elif event == "turn":
            peer.turn()
        elif event == "turn none":
            peer.activity(kind="turn", turned=True)
        else:
            peer.activity()
    peer.sync()
    expected = {"all": [plain, flagged], "unflagged": [plain], "none": []}[releases]
    assert peer.handed() == expected
    assert peer.held() == expected, "every release carries held: true"
    assert rows(rig, q) == {plain, flagged}, "a release deletes no row"


# ============================================================ (d) re-hand

def test_a_handed_row_is_not_handed_again_until_named_unhandled(rig, reads, peers):
    """A second `turn` does not hand a row again. Named `unhandled`, it is
    not handed by the frame that reported it (its release runs before settle), and is
    handed at the next release point."""
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    held = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, held)
    peer.turn()
    assert peer.held() == [held]
    peer.turn()
    assert peer.held() == [held], "handed once"
    peer.turn(unhandled=[held])
    assert peer.held() == [held], "not by the frame that reported it"
    assert held not in rig.daemon.bindings[REVIEWER_Q].delivered
    assert rows(rig, REVIEWER_Q) == {held}
    peer.turn()
    assert peer.held() == [held, held]


def test_an_unhandled_live_id_becomes_a_row_and_goes_at_the_next_release(rig, reads, peers):
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    live = post(rig, [REVIEWER_Q], "now")
    assert wait_until(lambda: live in peer.handed())
    assert rows(rig, REVIEWER_Q) == set()
    peer.turn(unhandled=[live])
    assert rows(rig, REVIEWER_Q) == {live}
    assert record_rows(rig.gateway, REVIEWER_Q) == {live}
    assert peer.handed() == [live]
    peer.ready("compact")
    assert peer.handed() == [live, live] and peer.held() == [live]
    peer.turn(handled=[live])
    assert rows(rig, REVIEWER_Q) == set()


def test_an_unhandled_id_this_binding_was_not_handed_is_ignored(rig, reads, peers):
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    other = post(rig, [SENTINEL_Q], "not for reviewer")
    row_for(rig, SENTINEL_Q, other)
    peer.turn(unhandled=[other])
    assert rows(rig, REVIEWER_Q) == set()


def test_unbind_queues_handed_ids_not_handled_and_not_past_the_cursor(rig, reads, peers):
    """At unbind, every handed id with no row, that the adapter did not
    name handled and that its cursor has not passed, gets a row. Of three handed ids,
    one is at the cursor (no row), one is handled above it (no row: the clamp kept the
    cursor below it), one is neither (a row)."""
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    passed = post(rig, [REVIEWER_Q], "handed, at the cursor")
    handled = post(rig, [REVIEWER_Q], "handed and handled, above the cursor")
    unfinished = post(rig, [REVIEWER_Q], "handed, never finished")
    assert wait_until(lambda: peer.handed() == [passed, handled, unfinished])
    cursors.advance(REVIEWER_Q, passed)           # clamped below what it still holds
    peer.turn(handled=[handled])
    assert rows(rig, REVIEWER_Q) == set()
    peer.close()
    assert rows(rig, REVIEWER_Q) == {unfinished}
    assert record_rows(rig.gateway, REVIEWER_Q) == {unfinished}


# ============================================================ (e) the readers

def test_a_rescan_while_bound_hands_no_row_id(rig, reads, peers):
    """Rows wait for a release point: a rescan, which routes
    the record again as live, skips them, unflagged and flagged alike, on a warm read."""
    reads[REVIEWER_Q] = cache.COLD
    peer = peers()
    cold = post(rig, [REVIEWER_Q], "while cold")
    flagged = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, cold)
    row_for(rig, REVIEWER_Q, flagged)
    reads[REVIEWER_Q] = cache.WARM
    rig.daemon.rescan()
    peer.sync()
    assert peer.handed() == []
    assert rows(rig, REVIEWER_Q) == {cold, flagged}


def test_replay_makes_a_flagged_message_with_no_row_a_row_and_delivers_it_not(
        rig, reads, peers, blocked):
    """A flagged message replay finds with no row becomes a row and is not
    delivered; an unflagged one beside it is replayed."""
    reads[REVIEWER_Q] = cache.WARM
    with blocked:
        flagged = post(rig, [REVIEWER_Q], "later", held=True)
        plain = post(rig, [REVIEWER_Q], "while routing was off")
    assert rows(rig, REVIEWER_Q) == set()
    peer = peers()
    assert peer.handed() == [plain]
    assert peer.payloads()[0]["backlog"] is True and "held" not in peer.payloads()[0]
    assert rows(rig, REVIEWER_Q) == {flagged}
    assert record_rows(rig.gateway, REVIEWER_Q) == {flagged}


def test_a_rescan_writes_no_row_at_or_below_the_cursor(rig, reads, peers, blocked):
    """Unbound: an id at or below the cursor gets no row from a rescan,
    one above it does. Bound: an id at or below the cursor is neither delivered nor
    queued."""
    with blocked:
        at = post(rig, [REVIEWER_Q], "handled before it unbound")
        above = post(rig, [REVIEWER_Q], "not yet")
    cursors.advance(REVIEWER_Q, at)
    rig.daemon.rescan()
    assert rows(rig, REVIEWER_Q) == {above}
    reads[CODER_Q] = cache.WARM
    peer = peers("codex")
    with blocked:
        below = post(rig, [CODER_Q], "handled")
        flagged_below = post(rig, [CODER_Q], "handled too", held=True)
    cursors.advance(CODER_Q, flagged_below)
    rig.daemon.rescan()
    peer.sync()
    assert below not in peer.handed()
    assert rows(rig, CODER_Q) == set()


# ============================================================ (f) failed hand-off

def test_a_failed_hand_off_is_a_row_the_next_binding_gets(rig, reads, peers, monkeypatch):
    """The socket fails at `sendall`; the id leaves `delivered`
    and gets a row, and the name's next binding is handed it."""
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    binding = rig.daemon.bindings[REVIEWER_Q]
    real = binding.conn.send

    def closed_before_sendall(frame):
        if frame.get("op") == "deliver":
            raise BrokenPipeError(32, "the socket closed before sendall")
        return real(frame)

    monkeypatch.setattr(binding.conn, "send", closed_before_sendall)
    mid = post(rig, [REVIEWER_Q], "this one is lost on the wire")
    row_for(rig, REVIEWER_Q, mid)
    assert mid not in binding.delivered
    assert wait_until(lambda: record_rows(rig.gateway, REVIEWER_Q) == {mid})
    peer.close()
    again = peers()
    assert again.held() == [mid]


def test_a_failed_hand_off_to_an_agent_n_writes_no_row(rig, reads, peers, monkeypatch):
    auto = Peer(rig, slot=4, name=None)
    try:
        reads[auto.q] = cache.WARM
        binding = rig.daemon.bindings[auto.q]
        real = binding.conn.send

        def closed_before_sendall(frame):
            if frame.get("op") == "deliver":
                raise BrokenPipeError(32, "the socket closed before sendall")
            return real(frame)

        monkeypatch.setattr(binding.conn, "send", closed_before_sendall)
        post(rig, [auto.q], "to an agent-N")
        drain(rig)
        assert rows(rig, auto.q) == set()
    finally:
        auto.client.close()


# ============================================================ (g) migration and trim

def _forget_migration(rig) -> None:
    gw = rig.gateway
    if rig.backend == "local":
        with gw._write() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (META_QUEUE_MIGRATED,))
            conn.execute("CREATE TABLE IF NOT EXISTS cold_refusals("
                         "message_id INTEGER NOT NULL, name TEXT NOT NULL)")
            conn.execute("INSERT INTO cold_refusals VALUES (1, 'reviewer@proj-a')")
    else:
        with gw._txn(write=True) as st:
            st.meta.pop(META_QUEUE_MIGRATED, None)


def _has_refusals_table(rig) -> bool:
    row = rig.gateway._conn().execute(
        "SELECT name FROM sqlite_master WHERE name = 'cold_refusals'").fetchone()
    return row is not None


def test_the_migration_moves_held_mail_into_the_queue_once(rig, monkeypatch):
    """A gate file with held and refused ids and a `[next-turn]`
    record: the first boot makes rows for the held ones (not past the cursor, still in
    the record) and the `[next-turn]` one, none for refused and none for `[held]` or
    `[next-turn]` mail at or below its recipient's cursor, deletes the file, drops
    `cold_refusals` and sets the meta key; a second boot changes nothing."""
    import json
    monkeypatch.setattr(constants, "RESCAN_LIMIT", 1)     # the boot rescan reads past none
    rig.daemon.stop()
    # Old flagged mail the cursor has passed: handled long ago, it gets no row.
    old_held = post(rig, [REVIEWER_Q], "held, handled before", held=True)
    old_next_turn = post(rig, [], raw="::to reviewer@proj-a [next-turn]\nhandled too")
    passed = post(rig, [REVIEWER_Q], "cold-held, then handled")
    held = post(rig, [REVIEWER_Q], "cold-held by the old daemon")
    refused = post(rig, [CODER_Q], "refused by the old daemon")
    next_turn = post(rig, [], raw="::to planner@proj-a [next-turn]\nwhen you are next about")
    gone = "4611686018427399999"                        # no such message in the record
    post(rig, [], raw="prose, the last thing in the record")
    cursors.advance(REVIEWER_Q, passed)
    gate = rig.daemon.gate_path
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(json.dumps({"held": {REVIEWER_Q: [passed, held, gone],
                                         "agent-3@global": [held]},
                                "refused": {CODER_Q: [refused]}}))
    _forget_migration(rig)
    rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    assert queued(rig.daemon, REVIEWER_Q) == {held}
    assert not {old_held, old_next_turn} & record_rows(rig.gateway, REVIEWER_Q)
    assert queued(rig.daemon, "planner@proj-a") == {next_turn}
    assert queued(rig.daemon, CODER_Q) == set()
    assert not gate.exists()
    assert rig.gateway.meta_get(META_QUEUE_MIGRATED) is not None
    if rig.backend == "local":
        assert not _has_refusals_table(rig)
    before = record_rows(rig.gateway)
    stamp = rig.gateway.meta_get(META_QUEUE_MIGRATED)
    gate.write_text(json.dumps({"held": {CODER_Q: [refused]}}))
    rig.daemon.stop()
    rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    assert record_rows(rig.gateway) == before
    assert rig.gateway.meta_get(META_QUEUE_MIGRATED) == stamp
    assert gate.exists(), "the second boot runs no migration"


def _age(rig, mid, days) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rig.clock() - days * 86400))
    rig.age_message(mid, stamp)


def test_the_trim_keeps_every_queued_message_past_retention(rig, reads, peers,
                                                            monkeypatch):
    """The record keeps
    every message with a row whatever its age. Both of the trim's guards: the record's
    own (a row in the `queue` table keeps its message even when the daemon passes no
    `keep`), and the daemon's `keep` (a row whose record write has not landed yet)."""
    if rig.backend != "local":
        pytest.skip("only the local record trims; the mock keeps every message")
    offline = post(rig, [REVIEWER_Q], "for a name away 200 days")
    flagged = post(rig, [SENTINEL_Q], "later", held=True)
    plain = post(rig, [], raw="prose nobody waits for")
    row_for(rig, REVIEWER_Q, offline)
    row_for(rig, SENTINEL_Q, flagged)
    for mid in (offline, flagged, plain):
        _age(rig, mid, 200)
    assert rig.gateway.trim(keep=()) == 1
    assert rig.gateway.get_message(offline) is not None
    assert rig.gateway.get_message(flagged) is not None
    assert rig.gateway.get_message(plain) is None
    # A row only in memory: its record write failed and waits for the next pass.
    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rig.gateway, "queue_add", disk_full)
    unwritten = post(rig, [REVIEWER_Q], "its row is not in the record yet")
    row_for(rig, REVIEWER_Q, unwritten)
    assert unwritten not in record_rows(rig.gateway, REVIEWER_Q)
    _age(rig, unwritten, 200)
    rig.daemon.trim_record()
    assert rig.gateway.get_message(unwritten) is not None


def test_the_mock_record_has_no_trim():
    """The trim's other backend: the in-memory double keeps every message, so a row's
    message can only be lost from a local record."""
    from moxi_comms.mock_gateway import MockGateway
    assert not hasattr(MockGateway, "trim")


# ============================================================ (h) the Claude shim, end to end

class Frames:
    """Every `activity` frame the daemon handles, with the frame's identity's cursor as
    it stood when the frame arrived."""

    def __init__(self):
        self.seen: list = []

    def of(self, q, **match):
        return [f for f, _ in self.seen
                if f.get("identity") == q and all(f.get(k) == v for k, v in match.items())]


@pytest.fixture
def frames(monkeypatch):
    seen = Frames()
    real = Daemon._handle

    def handle(self, conn, frame):
        if frame.get("op") == "activity":
            seen.seen.append((dict(frame), cursors.read(str(frame.get("identity")))))
        return real(self, conn, frame)

    monkeypatch.setattr(Daemon, "_handle", handle)
    return seen


def test_the_idle_tick_flushes_live_mail_and_releases_no_row(rig, frames):
    """A live message to an idle warm shim with a flagged row is flushed by
    the idle tick; the tick's frame carries no kind; the row is not handed; the next real
    Stop releases it."""
    peer = ShimPeer(rig, **REVIEWER)
    shim = peer.shim
    held = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, held)
    live = post(rig, [REVIEWER_Q], "now")
    assert wait_until(lambda: [p["message_id"] for p in shim.queue] == [live])
    before = len(frames.of(REVIEWER_Q))
    shim.on_turn_boundary(idle=True)
    peer.sync()
    tick = frames.of(REVIEWER_Q)[before:]
    assert tick and all("kind" not in f for f in tick), tick
    assert shim.queue == [] and "now" in shim.injected[-1]
    assert rows(rig, REVIEWER_Q) == {held}
    assert held not in rig.daemon.bindings[REVIEWER_Q].delivered
    shim.on_turn_boundary()                      # the real Stop
    peer.sync()
    (turn,) = frames.of(REVIEWER_Q, kind="turn")
    assert turn["handled"] == [live]
    assert held in rig.daemon.bindings[REVIEWER_Q].delivered, "the Stop released it"
    assert wait_until(lambda: any(f"msg {held}" in t for t in shim.injected)
                      or held in [p["message_id"] for p in shim.queue])


def test_a_ready_before_the_register_goes_after_it_once(rig, frames):
    """A SessionStart that comes before the shim is bound is sent
    right after the register answer, once; its `startup` releases the flagged row a
    register holds."""
    held = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, held)
    shim = rig.shim(**REVIEWER, start=False)
    shim.notify_ready("startup")
    shim.start()
    assert wait_until(lambda: [p["message_id"] for p in shim.queue] == [held])
    shim._client.request("ping")
    assert len(frames.of(REVIEWER_Q, kind="ready")) == 1
    assert frames.of(REVIEWER_Q, kind="ready")[0]["source"] == "startup"
    assert all(p.get("held") is True for p in shim.queue)


def _clamp_setup(rig, blocked):
    """A live id L handed to the shim below a digest H in flight: L is posted with
    routing off (no row, not handed), H is a flagged row released by a `clear` and
    injected, then a rescan hands L while H's turn runs."""
    peer = ShimPeer(rig, **REVIEWER)
    shim = peer.shim
    with blocked:
        low = post(rig, [REVIEWER_Q], "the lower, live id")
    high = post(rig, [REVIEWER_Q], "the higher, released row", held=True)
    row_for(rig, REVIEWER_Q, high)
    assert int(low) < int(high)
    peer.ready("clear")
    assert [p["message_id"] for p in shim.queue] == [high]
    shim._flush()                                    # H in flight
    rig.daemon.rescan()                              # hands L
    assert wait_until(lambda: [p["message_id"] for p in shim.queue] == [low])
    shim.on_turn_boundary()                          # H completes; L is injected
    peer.sync()
    return peer, low, high


def test_the_clamp_keeps_the_cursor_below_a_held_live_id_and_unbind_queues_it(
        rig, frames, blocked):
    """The turn's cursor stops below L; the shim dies;
    L is a row at unbind."""
    peer, low, high = _clamp_setup(rig, blocked)
    assert cursors.read(REVIEWER_Q) is None or int(cursors.read(REVIEWER_Q)) < int(low)
    assert low not in rows(rig, REVIEWER_Q)
    peer.shim.die_mid_turn()
    wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
    assert rows(rig, REVIEWER_Q) == {low}


def test_the_clamps_high_water_mark_reaches_the_earlier_digest(rig, frames, blocked):
    """Once L completes, the cursor reaches H's `through`."""
    peer, low, high = _clamp_setup(rig, blocked)
    peer.shim.on_turn_boundary()                     # L completes
    peer.sync()
    assert cursors.read(REVIEWER_Q) == high
    assert rows(rig, REVIEWER_Q) == set()


def test_the_shim_writes_its_cursor_before_its_turn_frame(rig, frames):
    """When the daemon reads the `turn` frame, the cursor file
    already names the digest it reports handled."""
    peer = ShimPeer(rig, **REVIEWER)
    shim = peer.shim
    live = post(rig, [REVIEWER_Q], "now")
    assert wait_until(lambda: [p["message_id"] for p in shim.queue] == [live])
    shim._flush()
    shim.on_turn_boundary()
    peer.sync()
    reported = [(f, c) for f, c in frames.seen
                if f.get("identity") == REVIEWER_Q and f.get("kind") == "turn"
                and live in (f.get("handled") or ())]
    assert reported, frames.seen
    assert all(c == live for _, c in reported)


# ============================================================ (j) the send's cold-at

def _cold_at_cases(now):
    """(label, line zero's cold-at for a one-tag line or None, delivered?)"""
    return [("ahead", (now + 600,), True),
            ("dash", (machine_line.COLD_AT_LIVE,), True),
            ("equal", (now,), False),
            ("behind", (now - 1,), False),
            ("no token", None, False)]


@pytest.mark.parametrize("failure", ["unknown", "raises"])
def test_an_unknown_read_at_live_routing_is_decided_by_the_cold_at(rig, reads, peers,
                                                                  failure):
    """On an `unknown` read (or a read that raised) a message
    whose cold-at for the recipient is `-` or ahead of the daemon's clock goes live; one
    at or behind the clock, or with no token, gets a row. The value is the one at the
    recipient's position on line zero."""
    now = whole_seconds(rig)
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    reads[REVIEWER_Q] = (cache.UNKNOWN if failure == "unknown"
                         else RuntimeError("the read blew up"))
    ids = {label: post(rig, [REVIEWER_Q], label, cold_at=cold_at)
           for label, cold_at, _ in _cold_at_cases(now)}
    second = post(rig, [SENTINEL_Q, REVIEWER_Q], "second tag",
                  cold_at=(machine_line.COLD_AT_LIVE, now - 1))
    drain(rig)
    peer.sync()
    live = [ids[label] for label, _, go in _cold_at_cases(now) if go]
    waiting = {ids[label] for label, _, go in _cold_at_cases(now) if not go}
    assert peer.handed() == live
    assert rows(rig, REVIEWER_Q) == waiting | {second}


@pytest.mark.parametrize("failure", ["unknown", "raises"])
def test_an_unknown_read_at_replay_splits_one_batch_by_cold_at(rig, reads, peers, blocked,
                                                               failure):
    """The same cases found by replay in one batch split the same way;
    the register's release on that read hands nothing."""
    now = whole_seconds(rig)
    with blocked:
        ids = {label: post(rig, [REVIEWER_Q], label, cold_at=cold_at)
               for label, cold_at, _ in _cold_at_cases(now)}
    reads[REVIEWER_Q] = (cache.UNKNOWN if failure == "unknown"
                         else RuntimeError("the read blew up"))
    peer = peers()
    live = [ids[label] for label, _, go in _cold_at_cases(now) if go]
    waiting = {ids[label] for label, _, go in _cold_at_cases(now) if not go}
    assert peer.handed() == live
    assert all(p["backlog"] is True and "held" not in p for p in peer.payloads())
    assert rows(rig, REVIEWER_Q) == waiting


def test_a_known_read_decides_alone_whatever_the_cold_at(rig, reads, peers):
    now = whole_seconds(rig)
    reads[REVIEWER_Q] = cache.COLD
    peer = peers()
    ahead = post(rig, [REVIEWER_Q], "cold, cold-at ahead", cold_at=(now + 600,))
    dash = post(rig, [REVIEWER_Q], "cold, dash", cold_at=(machine_line.COLD_AT_LIVE,))
    row_for(rig, REVIEWER_Q, ahead)
    row_for(rig, REVIEWER_Q, dash)
    reads[REVIEWER_Q] = cache.WARM
    behind = post(rig, [REVIEWER_Q], "warm, cold-at behind", cold_at=(now - 600,))
    bare = post(rig, [REVIEWER_Q], "warm, no token")
    assert wait_until(lambda: peer.handed() == [behind, bare])
    assert rows(rig, REVIEWER_Q) == {ahead, dash}


def test_a_dash_never_expires(rig, reads, peers, blocked):
    """`-` has no time. Live, a year on, an `unknown`
    read still delivers it; replayed into a later binding a month after the send, too."""
    reads[REVIEWER_Q] = cache.UNKNOWN
    with blocked:
        old = post(rig, [REVIEWER_Q], "sent a month ago", cold_at=(machine_line.COLD_AT_LIVE,))
    rig.clock.advance(30 * 86400)
    cursors.advance(REVIEWER_Q, str(int(old) - 1))   # present: replay is not bounded
    peer = peers()
    assert peer.handed() == [old]
    rig.clock.advance(365 * 86400)
    live = post(rig, [REVIEWER_Q], "a year on", cold_at=(machine_line.COLD_AT_LIVE,))
    assert wait_until(lambda: live in peer.handed())
    assert rows(rig, REVIEWER_Q) == set()


def test_release_points_read_no_cold_at(rig, reads, peers):
    """A row whose message carries a cold-at ahead waits through a
    `turn` whose read is `unknown`; a `ready source=clear` releases it."""
    now = whole_seconds(rig)
    reads[REVIEWER_Q] = cache.COLD
    peer = peers()
    mid = post(rig, [REVIEWER_Q], "cold-at ahead", cold_at=(now + HOUR,))
    row_for(rig, REVIEWER_Q, mid)
    reads[REVIEWER_Q] = cache.UNKNOWN
    peer.turn()
    peer.ready("resume")
    assert peer.handed() == []
    peer.ready("clear")
    assert peer.held() == [mid]


# ============================================================ (k) adapters started before the deploy

@pytest.mark.parametrize("kind", ["claude-shim", "codex"])
def test_an_older_adapter_gets_rows_queued_while_cold_only_at_a_register(
        rig, reads, peers, kind):
    """An adapter started before the deploy sends `turn` with no
    `handled` and `ready` with no `source`. Unflagged mail queued while it was cold waits
    through both, as flagged mail does, and goes at its next register on a passing read;
    the flagged row still waits for a binding of the current code."""
    q = REVIEWER_Q if kind == "claude-shim" else CODER_Q
    reads[q] = cache.COLD
    old = peers(kind)
    cold = post(rig, [q], "while cold")
    flagged = post(rig, [q], "later", held=True)
    row_for(rig, q, cold)
    row_for(rig, q, flagged)
    reads[q] = cache.WARM
    old.activity(kind="turn", turned=True)       # its turns carry no `handled`
    old.activity(kind="ready")                   # its SessionStart carries no `source`
    assert old.handed() == []
    assert rows(rig, q) == {cold, flagged}
    old.close()
    again = peers(kind)                          # the reconnect
    assert again.held() == [cold]
    again.activity(kind="turn", turned=True)
    assert again.held() == [cold], "its turn deletes nothing and releases nothing"
    assert rows(rig, q) == {cold, flagged}


def test_a_turn_with_no_handled_deletes_no_handed_row(rig, reads, peers):
    """A `turn` with no `handled` settles nothing, whatever else it says."""
    reads[REVIEWER_Q] = cache.WARM
    peer = peers()
    held = post(rig, [REVIEWER_Q], "later", held=True)
    row_for(rig, REVIEWER_Q, held)
    peer.ready("startup")
    assert peer.held() == [held]
    peer.activity(kind="turn", turned=True, unhandled=[held])
    assert rows(rig, REVIEWER_Q) == {held}
    assert held in rig.daemon.bindings[REVIEWER_Q].delivered


# ============================================================ (l) a row whose message is gone

def _lose(rig, mid) -> None:
    """Take a message out of the record by hand."""
    if rig.backend == "local":
        with rig.gateway._write() as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (int(mid),))
    else:
        with rig.gateway._txn(write=True) as st:
            st.messages = [m for m in st.messages if m["id"] != str(mid)]


def _garble(rig, mid) -> None:
    """Make a message's line zero something no parser reads, by hand."""
    if rig.backend == "local":
        with rig.gateway._write() as conn:
            conn.execute("UPDATE messages SET content = ? WHERE id = ?",
                         ("not a machine line any more", int(mid)))
    else:
        with rig.gateway._txn(write=True) as st:
            for m in st.messages:
                if m["id"] == str(mid):
                    m["content"] = "not a machine line any more"


@pytest.mark.parametrize("damage", [_lose, _garble], ids=["gone", "unparseable"])
def test_a_row_whose_message_the_record_lost_is_kept_skipped_and_logged_once(
        rig, reads, peers, caplog, damage):
    """A row is deleted only on `handled`. A row whose message a hand-damaged record no longer has (or no longer
    parses) is skipped at every release point and kept, the rows beside it go out, and
    the daemon log says so once."""
    reads[REVIEWER_Q] = cache.COLD
    peer = peers()
    lost = post(rig, [REVIEWER_Q], "the record will lose this")
    kept = post(rig, [REVIEWER_Q], "this one stays")
    row_for(rig, REVIEWER_Q, lost)
    row_for(rig, REVIEWER_Q, kept)
    damage(rig, lost)
    with caplog.at_level(logging.WARNING, logger="comms.daemon"):
        peer.ready("clear")
        peer.ready("compact")
        reads[REVIEWER_Q] = cache.WARM
        peer.turn()
    assert peer.held() == [kept]
    assert rows(rig, REVIEWER_Q) == {lost, kept}
    assert record_rows(rig.gateway, REVIEWER_Q) == {lost, kept}
    said = [r for r in caplog.records if lost in r.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.WARNING, said
    assert "its row stays" in said[0].getMessage()
    peer.turn(handled=[kept])
    assert rows(rig, REVIEWER_Q) == {lost}
