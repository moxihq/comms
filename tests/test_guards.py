"""The guards: the `block` setting, 429-then-loud, the rescan."""

import errno
import itertools
import shutil
import socket
import tempfile
from pathlib import Path

import pytest
from conftest import queued, wait_for_queue, wait_until, write_config

from moxi_comms import constants, cursors, daemon as daemon_mod
from moxi_comms.daemon import Daemon
from moxi_comms.mock_gateway import MockGateway
from moxi_comms.paths import cursors_dir, daemon_socket
from moxi_comms.protocol import CLI_PROTOCOL, LineConn
from moxi_comms.sender import SendFailed, post


# --------------------------------------------------------------------------- block

def test_block_is_symmetric_at_the_routing_point(rig):
    a = rig.shim(slot=1, name="planner", scope="proj-a")
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    write_config(None, "block = true\n")

    rig.post_from_slot(6, ["reviewer@proj-a"], "blocked inbound")
    rig.cli(["send", "--posture", "none", "reviewer", "blocked outbound"], slot=1, name="planner", scope="proj-a")
    rig.post_from_slot(6, ["planner@proj-a"], "also blocked")
    wait_until(lambda: False, timeout=0.3)          # give routing a chance to misbehave
    assert a.queue == [] and b.queue == []

    write_config(None, "block = false\n")   # read at use time: instant, no restart
    rig.post_from_slot(6, ["reviewer@proj-a"], "now flowing")
    wait_for_queue(b, 1)
    assert b.queue[0]["text"] == "now flowing"


def test_send_verb_block_check_is_advisory_and_says_so(rig):
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    write_config(None, "block = true\n")
    code, out = rig.cli(["send", "--posture", "none", "reviewer", "hi"], slot=1, name="planner", scope="proj-a")
    assert code == 3
    assert "Advisory" in out and "enforcement is at daemon routing" in out
    assert rig.gateway.history(rig.names_channel) == []


# --------------------------------------------------------------------------- 429

def test_429_is_retried_honouring_retry_after(rig):
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db, SenderIdentity.for_slot(1, "x", "global"))
    finally:
        db.close()
    slept = []
    rig.gateway.inject_rate_limit(count=2, retry_after=0.5)
    msg = post(rig.gateway, cred, "::to nobody\nhi", username="x", sleep=slept.append)
    assert msg is not None
    assert slept == [0.5, 0.5], "Retry-After is honoured, not guessed"


def test_429_exhaustion_is_loud_and_the_message_is_not_in_the_record(rig):
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db, SenderIdentity.for_slot(1, "x", "global"))
    finally:
        db.close()
    rig.gateway.inject_rate_limit(count=constants.RETRY_ATTEMPTS, retry_after=0.0)
    with pytest.raises(SendFailed, match="SEND FAILED"):
        post(rig.gateway, cred, "::to nobody\nhi", username="x", sleep=lambda s: None)
    assert rig.gateway.history(rig.names_channel) == []


def test_the_verb_reports_the_loud_failure(rig):
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    rig.gateway.inject_rate_limit(count=constants.RETRY_ATTEMPTS, retry_after=0.0)
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "hi"], slot=1, name="planner", scope="proj-a")
    assert code == 5
    assert "SEND FAILED" in out and "was NOT posted" in out


# --------------------------------------------------------------------------- rescan

def test_gateway_reconnect_without_resume_triggers_a_rescan(rig):
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    before = rig.daemon.rescans
    rig.gateway.disconnect()
    rig.post_from_slot(6, ["reviewer@proj-a"], "posted during the gap")
    wait_until(lambda: False, timeout=0.2)
    assert b.queue == [], "the event never reached the socket"

    rig.gateway.reconnect(resume=False)             # fresh IDENTIFY: the gap is gone
    assert wait_until(lambda: rig.daemon.rescans > before)
    wait_for_queue(b, 1)
    assert b.queue[0]["text"] == "posted during the gap"


def test_resume_replays_the_gap_without_a_rescan(rig):
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    before = rig.daemon.rescans
    rig.gateway.disconnect()
    rig.post_from_slot(6, ["reviewer@proj-a"], "gap message")
    rig.gateway.reconnect(resume=True)
    wait_for_queue(b, 1)
    assert b.queue[0]["text"] == "gap message"
    assert rig.daemon.rescans == before


def test_boot_rescan_rebuilds_the_pending_view(rig):
    rig.post_from_slot(6, ["curator@proj-a"], "waiting")
    wait_until(lambda: queued(rig.daemon, "curator@proj-a"))
    fresh = daemon_mod.Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    rig.daemon.stop()
    fresh.start()
    try:
        assert len(queued(fresh, "curator@proj-a")) == 1
    finally:
        fresh.stop()
    rig.daemon = fresh


def test_rescan_does_not_re_pend_what_an_unbound_name_already_handled(rig):
    """The contract is "rescan #names PAST CURSORS". A chosen name that handled a message
    and then unbound gets no row for it: no reader writes a row for an id at or below
    the name's cursor (§3.3.2). The message is posted while the gateway is down, so only
    the rescan reads it."""
    rig.gateway.disconnect()
    msg = rig.post_from_slot(6, ["curator@proj-a"], "handled before unbinding")
    cursors.advance("curator@proj-a", msg.id)

    rig.daemon.rescan()
    assert queued(rig.daemon, "curator@proj-a") == set()


def test_rescan_still_pends_for_a_name_that_never_bound(rig):
    """No cursor at all means nothing was ever handled: the message stays pending."""
    rig.post_from_slot(6, ["curator@proj-a"], "nobody has ever held this name")
    assert cursors.read("curator@proj-a") is None

    rig.daemon.rescan()
    assert len(queued(rig.daemon, "curator@proj-a")) == 1


def test_rescan_still_pends_for_a_message_past_the_cursor(rig):
    """A cursor BEHIND the message proves only the earlier mail was handled. Both are
    posted while the gateway is down, so only the rescan reads them."""
    rig.gateway.disconnect()
    first = rig.post_from_slot(6, ["curator@proj-a"], "handled")
    second = rig.post_from_slot(6, ["curator@proj-a"], "arrived after the cursor")
    cursors.advance("curator@proj-a", first.id)

    rig.daemon.rescan()
    assert queued(rig.daemon, "curator@proj-a") == {second.id}


def test_a_corrupt_cursor_file_does_not_stop_the_daemon_from_BOOTING(rig, tmp_path):
    """New blast radius since the boot rescan moved into `start()`: an `int(cursor)` on a
    file holding junk used to raise out of `rescan()` and take BOOT with it -- no socket,
    no routing, over one unreadable advisory file. An unreadable cursor is treated as no
    cursor, which degrades toward replay and pending; never toward loss."""
    rig.post_from_slot(6, ["curator@proj-a"], "waiting behind a corrupt cursor")
    wait_until(lambda: queued(rig.daemon, "curator@proj-a"))
    cursors_dir().mkdir(parents=True, exist_ok=True)
    (cursors_dir() / "curator@proj-a.json").write_text('{"cursor": "not-a-number"}')

    fresh = daemon_mod.Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    rig.daemon.stop()
    fresh.start()
    try:
        assert len(queued(fresh, "curator@proj-a")) == 1, \
            "the name still pends: an unreadable cursor proves nothing was handled"
    finally:
        fresh.stop()
    rig.daemon = fresh


@pytest.mark.parametrize("body", ['["1400000000001"]', '{"cursor": {"id": "1"}}',
                                  'not json at all', '{"no_cursor_key": 1}'])
def test_an_unreadable_cursor_file_reads_as_no_cursor(rig, body):
    """Every way of being unusable answers the same way. The JSON LIST is the one that
    used to escape: valid JSON, so no decode error, and `["cursor"]` raises TypeError
    rather than KeyError -- straight out of `read` and into the caller."""
    cursors_dir().mkdir(parents=True, exist_ok=True)
    (cursors_dir() / "curator@proj-a.json").write_text(body)
    assert cursors.read("curator@proj-a") is None


@pytest.mark.parametrize("body", ['["1400000000001"]', '{"cursor": {"id": "1"}}',
                                  'not json at all', '{"no_cursor_key": 1}',
                                  '{"cursor": "not-a-number"}'])
def test_a_corrupt_cursor_file_does_not_raise_out_of_the_STOP_HOOK(rig, body):
    """The adapter side of the same rule. `advance` runs inside the Stop
    hook, and a raise there does not just lose a cursor -- `dispatch` never returns, so
    the turn boundary itself is lost and the shim stays 'busy' forever. Unusable cursor
    state is NO cursor: the advance proceeds, which is also the only reading that can
    heal the file."""
    cursors_dir().mkdir(parents=True, exist_ok=True)
    (cursors_dir() / "curator@proj-a.json").write_text(body)

    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    rig.post_from_slot(6, ["curator@proj-a"], "arrives over a corrupt cursor")
    wait_for_queue(curator, 1)
    delivered = curator.queue[0]["message_id"]

    curator.on_turn_boundary()                   # inject
    curator.on_turn_boundary()                   # complete: this is where it used to die
    assert cursors.read("curator@proj-a") == delivered


def test_advance_never_writes_a_cursor_that_is_not_a_message_id():
    """The other direction: a caller bug must not be written down. A cursor holding junk
    would claim mail was handled that never was -- the one thing a cursor must never do --
    so
    nothing is written, nothing is raised, and the batch replays."""
    assert cursors.advance("curator@proj-a", "1400000000009") == "1400000000009"
    assert cursors.advance("curator@proj-a", "not-an-id") is None
    assert cursors.advance("curator@proj-a", None) is None
    assert cursors.read("curator@proj-a") == "1400000000009", "the good one is untouched"


def test_a_cursor_that_cannot_be_written_degrades_to_replay_not_to_a_raise(monkeypatch):
    """A read-only STATE dir, a full disk: the turn still completed and the record still
    holds the mail. Seeing it twice is the cost; the hook surviving is the point."""
    def no_writes(*a, **kw):
        raise OSError("read-only file system")
    monkeypatch.setattr(cursors.tempfile, "mkstemp", no_writes)
    assert cursors.advance("curator@proj-a", "1400000000001") is None
    assert cursors.read("curator@proj-a") is None


def test_a_cursor_record_of_the_wrong_shape_reads_as_no_record(rig):
    """`read_meta` is the diagnostic read, and it answers like the real one: valid JSON
    of the wrong shape is not a record."""
    cursors_dir().mkdir(parents=True, exist_ok=True)
    (cursors_dir() / "curator@proj-a.json").write_text('["1400000000001"]')
    assert cursors.read_meta("curator@proj-a") is None


def test_bind_deliver_unbind_rescan_leaves_the_pending_view_empty(rig):
    """The drain shape end to end: a one-shot drain binding takes the mail, completes the
    turn (cursor advances), unbinds -- and a reconnect rescan must not resurrect it."""
    rig.post_from_slot(6, ["curator@proj-a"], "drain me")
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 1)
    wait_until(lambda: curator.replaying is False)
    delivered = curator.queue[0]["message_id"]
    curator.on_turn_boundary()
    curator.on_turn_boundary()
    assert cursors.read("curator@proj-a") == delivered

    curator.stop()
    wait_until(lambda: "curator@proj-a" not in rig.daemon.bindings)

    before = rig.daemon.rescans
    rig.gateway.reconnect(resume=False)
    assert wait_until(lambda: rig.daemon.rescans > before)
    wait_until(lambda: False, timeout=0.2)          # let a bad row show up
    assert queued(rig.daemon, "curator@proj-a") == set()


# --------------------------------------------------------------------------- offline

def test_shim_surfaces_comms_offline_and_reconnects(rig):
    from moxi_comms.adapters.claude_shim import OFFLINE_MESSAGE
    b = rig.shim(slot=2, name="reviewer", scope="proj-a", autoreconnect=True,
                 backoff=itertools.repeat(0.02))
    rig.daemon.stop()
    assert wait_until(lambda: b.offline)
    assert any(OFFLINE_MESSAGE in s for s in b.statuses)

    fresh = daemon_mod.Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    rig.daemon = fresh
    assert wait_until(lambda: b.offline is False, timeout=5.0)
    assert wait_until(lambda: any("comms online" in s for s in b.statuses))
    rig.post_from_slot(6, ["reviewer@proj-a"], "after the outage")
    assert wait_until(lambda: any(p["text"] == "after the outage" for p in b.queue))


def test_backoff_shape_is_bounded_and_jittered():
    from moxi_comms.client import backoff_delays
    delays = list(itertools.islice(backoff_delays(), 12))
    assert delays[0] <= constants.BACKOFF_START * (1 + constants.BACKOFF_JITTER)
    assert all(d <= constants.BACKOFF_MAX * (1 + constants.BACKOFF_JITTER) for d in delays)
    assert delays[-1] > delays[0]


# --------------------------------------------------------------------------- posture

def test_socket_posture_is_0600_in_a_0700_dir(rig):
    import os
    from moxi_comms.paths import daemon_socket
    sock = daemon_socket()
    assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
    assert oct(os.stat(sock.parent).st_mode & 0o777) == "0o700"


# ------------------------------------------------------------------ the unlink gate

@pytest.fixture
def shortdir():
    """A SHORT directory for socket paths: `sun_path` is 108 bytes and pytest's
    `tmp_path` spends most of them on the test's own name."""
    d = Path(tempfile.mkdtemp(prefix="cmu"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def stale_socket(path: Path) -> Path:
    """A socket file with nothing behind it -- bound, never listened, then dropped.
    Connecting to it gives ECONNREFUSED, which is the allowlist's second entry."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    assert path.exists()
    return path


def says_ping(path: Path) -> bool:
    """Does something at `path` answer the claims protocol? A round trip, not a knock."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(path))
    conn = LineConn(s)
    try:
        conn.send({"id": 7, "op": "ping"})
        return conn.recv() == {"id": 7, "ok": True, "cli_protocol": CLI_PROTOCOL}
    finally:
        conn.close()


def test_stop_without_a_start_removes_nothing(shortdir):
    """The old daemon's exit deleted whatever file was at the path. A daemon that
    never bound has nothing of its own there, so it takes nothing away."""
    path = shortdir / "d.sock"
    path.write_text("someone else's")
    d = Daemon(MockGateway(), socket_path=path, start_trim_thread=False)
    assert d.bound is False
    d.stop()
    assert path.exists(), "a daemon removes only the socket it bound"


def test_a_start_against_a_live_daemon_refuses_and_leaves_it_serving(rig):
    """The incident, run forwards: a second daemon on the live one's path. It must
    refuse, and neither the refusal nor its `stop()` may touch what is already there."""
    path = daemon_socket()
    inode = path.stat().st_ino
    intruder = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)

    with pytest.raises(RuntimeError, match="live comms daemon"):
        intruder.start()
    assert path.stat().st_ino == inode, "the live socket file is untouched"

    intruder.stop()
    assert path.stat().st_ino == inode, "and the refused daemon's stop takes nothing"
    assert says_ping(path), "the live daemon is still serving through its own name"
    assert rig.shim(slot=1, name="planner", scope="proj-a").identity == "planner@proj-a"


def test_a_stale_socket_file_is_cleared_and_the_daemon_serves(rig, shortdir):
    """Unattended recovery still works: the file a dead daemon left behind answers
    ECONNREFUSED, which is the allowlist saying `nothing is here`."""
    path = stale_socket(shortdir / "d.sock")
    d = Daemon(rig.gateway, clock=rig.clock, socket_path=path, start_trim_thread=False,
               journal_path=shortdir / "j.db").start()
    try:
        assert d.bound is True
        assert says_ping(path)
    finally:
        d.stop()
    assert not path.exists()


def test_a_clean_stop_removes_the_socket_and_a_second_stop_does_not(rig, shortdir):
    """The regression, and the flag's reset: once the file is gone the daemon no
    longer claims it, so a second `stop()` cannot take a successor's socket with it."""
    path = shortdir / "d.sock"
    d = Daemon(rig.gateway, clock=rig.clock, socket_path=path, start_trim_thread=False,
               journal_path=shortdir / "j.db").start()
    assert path.exists()
    d.stop()
    assert not path.exists() and d.bound is False

    path.write_text("a successor's")
    d.stop()
    assert path.exists(), "the second stop removes nothing"


@pytest.mark.parametrize("error", [
    BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),
    PermissionError(errno.EACCES, "Permission denied"),
])
def test_a_probe_that_cannot_conclude_refuses_and_says_which_errno(monkeypatch, rig,
                                                                   shortdir, error):
    """The blocker the allowlist exists for. A live listener with a full backlog
    answers EAGAIN and a foreign-owned socket answers EACCES -- and a bare
    `except OSError: proceed` would read both as `dead`, re-introducing the steal
    exactly under load. Neither refusal may claim to know a live daemon is there."""
    path = stale_socket(shortdir / "d.sock")

    def refuse(self, address):
        raise error

    monkeypatch.setattr(socket.socket, "connect", refuse)
    d = Daemon(rig.gateway, clock=rig.clock, socket_path=path, start_trim_thread=False,
               journal_path=shortdir / "j.db")
    with pytest.raises(RuntimeError) as caught:
        d.start()
    assert f"errno {error.errno}" in str(caught.value)
    assert type(error).__name__ in str(caught.value)
    assert "live comms daemon" not in str(caught.value), "it does not claim to know"
    assert path.exists() and d.bound is False


def test_a_refused_start_has_no_side_effects_at_all(rig, shortdir):
    """Refusing is not half-starting: no server socket, no gateway subscription,
    and no journal -- `Journal.open()` would have created the file and its parent."""
    journal = shortdir / "fresh" / "bindings.db"
    subscribers = len(rig.gateway._subs)
    intruder = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False,
                      journal_path=journal)

    with pytest.raises(RuntimeError):
        intruder.start()

    assert intruder._server is None and intruder.bound is False
    assert len(rig.gateway._subs) == subscribers, "it never subscribed"
    assert not journal.exists() and not journal.parent.exists()


def test_every_post_suppresses_mentions():
    from moxi_comms.mock_gateway import GatewayError
    gw = MockGateway()
    ch = gw.create_channel("#names")
    cred = gw.create_webhook(ch, "x", "s")
    with pytest.raises(GatewayError, match="allowed_mentions"):
        gw.execute_webhook(cred, "hi")
    with pytest.raises(GatewayError, match="allowed_mentions"):
        gw.execute_webhook(cred, "hi", allowed_mentions={"parse": ["everyone"]})


# --------------------------------------------------------------------------- handing is
# not handling

def test_mail_handed_to_a_session_that_unbinds_before_its_cursor_moves_is_pending_again(rig):
    """Handing a message to a binding is not handling it. The cursor is the one durable
    "handled?" fact, and a session that unbinds before it moves leaves the message
    waiting in #names exactly as before -- a re-bind's replay would deliver it again --
    so the pending view must say so without waiting for a rescan."""
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    msg = rig.post_from_slot(6, ["reviewer@proj-a"], "handed, never handled")
    assert wait_until(lambda: msg.id in rig.daemon.bindings["reviewer@proj-a"].delivered)
    assert queued(rig.daemon, "reviewer@proj-a") == set(), \
        "while bound and in the adapter's hands it is not waiting for a bind"

    b.stop()                                          # no turn boundary: cursor unmoved
    assert wait_until(lambda: "reviewer@proj-a" not in rig.daemon.bindings)
    assert cursors.read("reviewer@proj-a") is None
    assert queued(rig.daemon, "reviewer@proj-a") == {msg.id}

    code, out = rig.cli(["agents", "--seats"], slot=1)
    assert code == 0
    assert "waiting (" in out and "reviewer@proj-a" in out and "1 message(s)" in out, \
        "and comms agents --seats says so"

    # ...and a re-bind's release hands it, which is what the view promised; the row
    # goes when the turn holding it completes (§3.3.3), not at the hand-off
    again = rig.shim(slot=2, name="reviewer", scope="proj-a")
    assert wait_until(lambda: any(q["message_id"] == msg.id for q in again.queue))
    assert wait_until(lambda: again.replaying is False)
    again.on_turn_boundary()                          # inject
    again.on_turn_boundary()                          # complete: `handled` names it
    assert wait_until(lambda: queued(rig.daemon, "reviewer@proj-a") == set()), \
        "handed again and handled, so not waiting again"


def test_mail_a_session_handled_before_unbinding_stays_handled(rig):
    """The other half: the cursor passed it, so it is not pending after the unbind."""
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    msg = rig.post_from_slot(6, ["reviewer@proj-a"], "handled")
    assert wait_until(lambda: msg.id in rig.daemon.bindings["reviewer@proj-a"].delivered)
    cursors.advance("reviewer@proj-a", msg.id)         # the turn boundary's act

    b.stop()
    assert wait_until(lambda: "reviewer@proj-a" not in rig.daemon.bindings)
    assert queued(rig.daemon, "reviewer@proj-a") == set()


def test_a_partial_cursor_re_pends_only_what_it_has_not_passed(rig):
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    first = rig.post_from_slot(6, ["reviewer@proj-a"], "handled")
    second = rig.post_from_slot(6, ["reviewer@proj-a"], "handed only")
    assert wait_until(lambda: second.id in rig.daemon.bindings["reviewer@proj-a"].delivered)
    cursors.advance("reviewer@proj-a", first.id)

    b.stop()
    assert wait_until(lambda: "reviewer@proj-a" not in rig.daemon.bindings)
    assert queued(rig.daemon, "reviewer@proj-a") == {second.id}
