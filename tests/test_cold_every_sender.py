"""Cold sends v0.6 ([B1] comms@cold-every-sender): THE RULE, from every sender.

No agent may send another agent a message live while the recipient's cache is expired,
and every sender counts as an agent. A message reaches a bound agent live only when comms has read its cache as warm, or as
fresh on positive evidence. Everything else is held (a claude-shim, for its own next
turn) or refused (an adapter that takes no delayed mail), whoever sent it and by
whatever path. v0.6 refused `--cold` from every sender.

cold-hold ([B1], 2026-10-10) changed what the verb does with a held claude-shim
recipient: it posts the message, exit 0, and the daemon's gate holds it for that
session's own next turn, and accepted and ignored `--cold` with a NOTE. comms/refine
§6.4 removed the flag: it is argparse's exit 2 from every sender now. A recipient the
daemon will not hold (a codex or an opencode agent, no verdict, a failed query) still refuses the post, exit 6, nothing posted, "Nothing was sent." first and
last. The tests below assert that where v0.6 asserted exit 6 for a claude-shim.

comms/refine §3 (the one queue) changed THE RULE's second half again. A cold recipient
of every adapter is posted and held, a row in the one queue, never refused ([F5]); a
read comms cannot make (unknown, a name that unbound mid-check, a failed `session`
query) refuses the post at the send, exit 6, nothing posted ([F7]); an adapter with no
clock reads `no-cache` and goes live ([F6]). Past the verb, the daemon's `unknown` read
is decided by the send's `[cold-at:…]` on line zero: `-` or a time still ahead
delivers, a time reached or no token at all makes a row ([C12], [R8], [R72]). A row goes
out only at a release point (§3.5): a `turn` carrying `handled` on a passing read, a
`ready` with a source, a register (unflagged rows) on a passing read. Never with another
message ([F3]: no piggyback). The gate file, `cold_refusals` and `never_held` are gone.

Sections follow design v0.6 §11.2: (a) `clock_state`, (b) the verb with no exemption,
(c) `--cold`, (d) the daemon's gate, past the verb, (e) the race, (f) [R16] and [R17],
(g) `never_held` retired, (h) `moxi comms cache`, (i) the wrong root holds, (j) the shim's
evidence, and (k) the five items of the v0.6 re-check the build took.

Every rig test runs on both backends (BOTH_BACKENDS), never Discord (RULE 8). The cache
clock reads fixture transcripts under the rig's tmp projects root (`CLAUDE_CONFIG_DIR`, set by
`conftest.env`). The `only_tmp_paths` guard, carried
from the cold-lock branch, fails a test whose comms paths leave tmp. Nothing here calls
`monkeypatch.undo()`.
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (assistant_line, cache_absent, cache_api, projects_root, record_rows,
                      send_turn, wait_for_queue, wait_until, write_transcript)

from moxi_comms import cache, cli, cursors, machine_line, paths, sending
from moxi_comms.adapters.claude_shim import ClaudeShim
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import Daemon
from moxi_comms.paths import daemon_socket
from moxi_comms.protocol import CLI_PROTOCOL, LineConn
from moxi_comms.provision import NAMES
from moxi_comms.sender import SenderIdentity

# Fixtures shared with the v0.4 suite. Imported, so pytest finds them here too.
from test_cold_sends import bind, reboot  # noqa: F401

import shim_drive

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
REVIEWER_Q = "reviewer@proj-a"
HELPER_Q = "helper@proj-a"            # a bare claude-shim binding (`bare_shim`)
CODER_Q = "coder@proj-a"              # a codex agent

SESS_A = "aaaaaaaa-1111-4111-8111-111111111111"
SESS_B = "bbbbbbbb-2222-4222-8222-222222222222"
SESS_C = "cccccccc-3333-4333-8333-333333333333"
SESS_D = "dddddddd-4444-4444-8444-444444444444"
SESS_BLANK = "eeeeeeee-5555-4555-8555-555555555555"

HOUR = 3600

AS_AGENT = dict(slot=PLANNER["slot"], name=PLANNER["name"], scope=PLANNER["scope"],
                env=None)
AS_OPERATOR = dict(slot=None, name=None, scope=None, env={"MOXI_COMMS_OPERATOR": "900001"})
AS_CONSOLE = dict(slot=None, name="console", scope="acme", env=None)
LANES = [AS_AGENT, AS_OPERATOR, AS_CONSOLE]
LANE_IDS = ["agent", "operator", "console"]


def held_last(q=REVIEWER_Q):
    """A held send's last line for one chosen name (comms/refine §3.8.2)."""
    return (f"Sent and held: {q} reads this after its own next turn, or when its session "
            "is cleared, compacted or restarted. Nothing wakes it for this message alone.")


HELD_LAST = held_last()


# ------------------------------------------------------------------------ the guard

def _live_paths():
    return {
        "socket": paths.daemon_socket(),
        "record": paths.record_db(),
        "directory": paths.directory_db(),
        "tokens": paths.tokens_dir(),
        "cursors": paths.cursors_dir(),
        "cold gate": paths.cold_gate(),
        "claude projects": Path(cache_api().projects_root(None)),
        "shim root": Path(cache_api().projects_root(os.environ)),
    }


def assert_all_under(paths, roots, when):
    """Fails unless every path in `paths` (a name -> path mapping) resolves under one of
    `roots`. The guard's check, kept apart from the fixture so a test can drive it
    (build-cleanup design §2.1: a test-temp guard accepts the run's own pytest base temp
    or `tempfile.gettempdir()`, and nothing else)."""
    roots = [Path(root).resolve() for root in roots]
    for what, path in paths.items():
        resolved = Path(path).resolve()
        assert any(resolved.is_relative_to(root) for root in roots), (
            f"{when}: the {what} path {resolved} is under neither "
            f"{' nor '.join(str(root) for root in roots)}; this test could reach live "
            "comms state")


@pytest.fixture(autouse=True)
def only_tmp_paths(tmp_path_factory):
    """Fails the test if any path comms resolves -- the socket, STATE, the record, the
    gate file, the transcripts the clock reads -- is not test temp, before the test and
    again after it (cold-lock's guard: a mid-test `monkeypatch.undo()` once put the live
    paths back and bound a name on the live daemon). Test temp is the run's own pytest
    base temp, which `PYTEST_DEBUG_TEMPROOT` may put outside /tmp, or
    `tempfile.gettempdir()`, where the suites' short socket dirs stay (build-cleanup
    design §2.1)."""
    roots = (tmp_path_factory.getbasetemp(), tempfile.gettempdir())
    assert_all_under(_live_paths(), roots, "before the test")
    yield
    assert_all_under(_live_paths(), roots, "after the test")


def test_the_guard_passes_a_path_under_either_root(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    gettemp = Path(tempfile.gettempdir())
    assert_all_under({"base": base / "x", "gettempdir": gettemp / "x"},
                     (base, gettemp), "probe")


def test_the_guard_fails_a_path_outside_both_roots_and_names_both(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    gettemp = Path(tempfile.gettempdir()).resolve()
    outside = Path("/proc/self/x")       # under neither root, whatever $HOME or TMPDIR is
    with pytest.raises(AssertionError) as raised:
        assert_all_under({"socket": outside}, (base, gettemp), "probe")
    said = str(raised.value)
    assert "probe: the socket path" in said
    assert str(base) in said and str(gettemp) in said


# --------------------------------------------------------------------------- helpers

def posted(rig):
    return rig.gateway.history(rig.names_channel)


def held_of(rig, q=REVIEWER_Q):
    """The ids waiting in `q`'s queue: one queue holds offline, cold and flagged mail
    alike (design §3.2)."""
    with rig.daemon._lock:
        return set(rig.daemon.queue.get(q, {}))


def queued_ids(shim_or_queue):
    queue = getattr(shim_or_queue, "queue", shim_or_queue)
    return [p["message_id"] for p in queue]


def nudge(rig):
    if hasattr(rig.gateway, "nudge"):
        rig.gateway.nudge()


def post(rig, recipients, text="past the verb", *, next_turn=False, slot=6,
         display="planner@proj-a", cold_at=None):
    """A post straight to the record through a slot's webhook, past the verb, then a
    nudge: what a process that skips `comms send` does (§6.8 P13). Line zero carries no
    `[cold-at:…]` token unless `cold_at` gives its values (comms/refine §3.8.6)."""
    from moxi_comms import directory
    from moxi_comms.mock_gateway import NO_MENTIONS
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(paths.directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(slot, display, "global"))
    finally:
        db.close()
    msg = rig.gateway.execute_webhook(
        cred, machine_line.compose(list(recipients), text, next_turn=next_turn,
                                   cold_at=cold_at),
        username=display, allowed_mentions=NO_MENTIONS)
    nudge(rig)
    return msg


def raw_activity(pushes, q, **fields):
    """An `activity` frame on a bare binding's own connection (a bare claude-shim, a codex or an
    opencode agent bound by `adapters`): the release points of comms/refine §3.5 ride
    these frames, whichever adapter code sends them."""
    pushes.client.notify("activity", identity=q, **fields)


def raw_turn(pushes, q, handled=(), unhandled=()):
    """`activity kind=turn` with `handled` and `unhandled`, as this round's adapters send
    it at every turn end (§3.3.3, §3.4)."""
    raw_activity(pushes, q, kind="turn", turned=True, handled=[str(i) for i in handled],
                 unhandled=[str(i) for i in unhandled])


def raw_ready(pushes, q, source):
    raw_activity(pushes, q, kind="ready", source=source)


def send(rig, to, text="hello", *flags, who=AS_AGENT):
    return rig.cli(["send", "--posture", "none", to, text, *flags], slot=who["slot"], name=who["name"],
                   scope=who["scope"], env=who["env"])


class Pushes:
    """A bare adapter binding that records what the daemon pushes to it."""

    def __init__(self):
        self.frames = []

    def __call__(self, frame):
        self.frames.append(frame)

    def delivered(self):
        return [f["payload"] for f in self.frames if f.get("op") == "deliver"]

    def ids(self):
        return [p["message_id"] for p in self.delivered()]


@pytest.fixture
def adapters():
    """Bare bindings (a claude-shim, a codex agent, the operator) that record
    deliveries. Closed at the end of the test."""
    made = []

    def _bind(**kw):
        pushes = Pushes()
        c = DaemonClient(daemon_socket(), on_push=pushes).connect()
        c.register(**kw)
        made.append(c)
        pushes.client = c
        return pushes

    yield _bind
    for c in made:
        c.close()


def bare_shim(adapters, name="helper", sid=SESS_D, age=None, **kw):
    """`helper@proj-a` held by a bare claude-shim binding on session `sid` (`""`: a
    session that has made no request yet, `fresh`), with a transcript `age` seconds old
    when given. The bare binding reports its own root, so its mail is read by the same
    clock as a shim's, and records what the daemon pushes."""
    if sid and age is not None:
        write_transcript(sid, age)
    return adapters(slot=kw.get("slot", 3), name=name, scope="proj-a", adapter="claude-shim",
                    session_id=sid or SESS_BLANK, projects_root=str(projects_root()),
                    turned=bool(sid))


def shim(rig, sid=SESS_A, *, age="none", **kw):
    """reviewer's shim with session `sid`; `age` a number writes a transcript whose last
    request is that old, None writes one with no assistant line, "none" writes nothing
    (a session that has not been prompted)."""
    if age is None:
        write_transcript(sid, None)
    elif age != "none":
        write_transcript(sid, age)
    return rig.shim(**{**REVIEWER, **kw}, session_id=sid)


# ============================================================ (a) clock_state
# The transcript reader itself is the cache module's, and its pure reads are tested in
# its suite (design §6.9). What stays is comms' seam: what reads without the module, and
# that a read never raises.

@pytest.mark.parametrize("adapter", ["something-new", None, "driver", "speech",
                                     "send-only"])
def test_an_adapter_with_no_clock_reads_no_cache(adapter):
    """No clock, live ([F6], comms/refine §3.6): an adapter comms has no clock for reads
    `no-cache`, a passing read, with the reason naming the adapter; never `unknown`, so
    its mail is never refused nor queued for lack of a clock. (codex and opencode have a
    clock of their own, `test_cache_clock.py`; a module's kind, `test_cache_seam.py`.)"""
    clock = cache.clock_state(adapter, SESS_A)
    assert clock.kind == "no-cache" and clock.kind in cache.PASSING
    assert clock.code == cache.NO_CLOCK
    assert clock.reason == f"comms has no cache clock for {adapter or 'unknown'} agents"


def test_a_shim_with_no_id_is_unknown():
    cache_api()
    clock = cache.clock_state("claude-shim", None, projects_root="/tmp", turned=False)
    assert (clock.kind, clock.code) == ("unknown", "no-session-yet")


def test_a_warm_and_a_cold_clock_through_the_seam(tmp_path):
    cache_api()
    write_transcript(SESS_A, 30)
    write_transcript(SESS_B, 2 * HOUR)
    root = str(projects_root())
    assert cache.clock_state("claude-shim", SESS_A, projects_root=root).kind == "warm"
    cold = cache.clock_state("claude-shim", SESS_B, projects_root=root)
    assert (cold.kind, cold.ttl_seconds) == ("cold", HOUR)
    fresh = cache.clock_state("claude-shim", SESS_C, projects_root=root, turned=False)
    assert fresh.kind == "fresh", "no transcript, on the session's own word"


def test_clock_state_never_raises(monkeypatch):
    monkeypatch.setattr(cache_api(), "read", lambda *a, **k: 1 / 0)
    clock = cache.clock_state("claude-shim", SESS_A, projects_root="/tmp", turned=False)
    assert clock.kind == "unknown" and "ZeroDivisionError" in clock.reason
    assert clock.code == cache.READ_FAILED


# ================================================= (b) the verb, with no exemption

def _hold_state(rig, adapters, state, tmp_path=None):
    """Bind the recipient for one state that does not pass; returns the tag to send
    to."""
    if state in ("codex cold", "opencode cold"):
        kind = state.split()[0]
        adapters(slot=5, name="coder", scope="proj-a", adapter=kind,
                 report=cold_harness_report(kind, tmp_path))
        return CODER_Q
    if state == "cold":
        shim(rig, SESS_A, age=2 * HOUR)
        return "reviewer@proj-a"
    if state == "no session id":
        rig.shim(**REVIEWER, session_id=None)
        return "reviewer@proj-a"
    if state == "unreadable":
        path = write_transcript(SESS_A, 60)
        path.chmod(0)
        shim(rig, SESS_A)
        return "reviewer@proj-a"
    if state == "no evidence":
        s = shim(rig, SESS_A)               # no transcript ...
        s.note_turned()                     # ... and a turn already seen
        s.notify_activity(None)
        wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].turned)
        return "reviewer@proj-a"
    if state == "bare cold":
        bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
        return HELPER_Q
    if state in ("codex", "opencode"):
        adapters(slot=5, name="coder", scope="proj-a", adapter=state)
        return CODER_Q
    raise AssertionError(state)


#: A cold read, on every adapter: posted and held, a row ([F5], comms/refine §3.8.2).
HELD_STATES = ["cold", "bare cold", "codex cold", "opencode cold"]
#: A read comms cannot make, on every adapter: refused, exit 6, nothing posted ([F7],
#: §3.8.3). A codex or opencode binding that reported no session reads unknown.
REFUSED_STATES = ["no session id", "unreadable", "no evidence", "codex", "opencode"]


def _bind_lane(bind, who):
    """The console's lane holds a binding of its own, as a send-only console does, so a
    post that goes has a credential to go with."""
    if who is AS_CONSOLE:
        bind(slot=6, name="console", scope="acme")


@pytest.mark.parametrize("state", HELD_STATES + REFUSED_STATES)
@pytest.mark.parametrize("who", LANES, ids=LANE_IDS)
def test_every_sender_is_held_or_refused_in_every_state_that_does_not_pass(
        rig, adapters, bind, tmp_path, who, state):
    """[C14], [C15]: the operator's lane and the console are checked like any agent, for every state that is not warm, fresh or no-cache. A cold recipient of any
    adapter is posted and held, exit 0, a row and no delivery ([F5]); a read comms cannot
    make refuses the post, exit 6, nothing posted, whatever the adapter ([F7])."""
    if who is AS_OPERATOR and state in HELD_STATES and rig.backend != "local":
        pytest.skip("a held post on the operator's lane needs local's operator credential")
    if state in HELD_STATES:
        _bind_lane(bind, who)
    to = _hold_state(rig, adapters, state, tmp_path)
    try:
        code, out = send(rig, to, "may this go?", who=who)
    finally:
        for p in projects_root().rglob("*.jsonl"):
            p.chmod(0o600)
    lines = out.splitlines()
    if state in HELD_STATES:
        assert code == 0, out
        (msg,) = posted(rig)
        assert lines[0].startswith(f"sent {msg.id} ") and lines[1].startswith("held: ")
        assert lines[-1].startswith(held_last(to)), out
        assert wait_until(lambda: msg.id in held_of(rig, to))
        time.sleep(0.1)
        assert msg.id not in rig.daemon.bindings[to].delivered, "held, not delivered"
    else:
        assert code == cli.EXIT_COLD == 6, out
        assert lines[0].startswith(f"Nothing was sent: comms could not read {to}'s cache ("), out
        assert lines[1:] == ["Send it again in a moment; a read that succeeds holds or "
                             "delivers it.", "Nothing was sent."], out
        assert posted(rig) == [] and held_of(rig, to) == set()
    assert "--cold" not in out


def test_warm_and_fresh_are_sent(rig, adapters):
    """(A free model's `no-cache` goes live too: `test_cache_clock.py`.)"""
    shim(rig, SESS_A, age=60)
    assert send(rig, REVIEWER_Q)[0] == 0
    bare_shim(adapters, sid="")                       # no request yet: fresh
    assert send(rig, HELPER_Q)[0] == 0


def test_a_never_prompted_session_reads_fresh_and_is_sent(rig):
    s = shim(rig, SESS_B)                     # no transcript, own root, no prompt
    code, out = send(rig, REVIEWER_Q, "first task")
    assert code == 0, out
    wait_for_queue(s, 1)


def test_a_small_cold_context_is_held(rig):
    shim(rig, SESS_A, age=HOUR + 12 * 60)
    write_transcript(SESS_A, HOUR + 12 * 60)
    code, out = send(rig, REVIEWER_Q)
    assert code == 0 and len(posted(rig)) == 1
    assert out.splitlines()[1].startswith("held: reviewer@proj-a's cache went cold 12m ago")


def test_a_failed_session_query_refuses_the_post(rig, monkeypatch):
    """A `session` query that fails refuses the post, exit 6 ([F7], §3.8.3)."""
    shim(rig, SESS_A, age=60)
    real = DaemonClient.query

    def broken(self, kind, **kw):
        if kind == "session":
            from moxi_comms.client import DaemonError
            raise DaemonError("the daemon fell over")
        return real(self, kind, **kw)

    monkeypatch.setattr(DaemonClient, "query", broken)
    code, out = send(rig, REVIEWER_Q)
    assert code == cli.EXIT_COLD and posted(rig) == []
    assert out.splitlines()[0] == ("Nothing was sent: comms could not ask the daemon about "
                                   "reviewer@proj-a (the session query failed (the daemon "
                                   "fell over)).")
    assert out.splitlines()[-1] == "Nothing was sent."


@pytest.mark.parametrize("older", ["no session query", "no verdict"])
def test_a_session_answer_with_no_verdict_refuses_the_post(rig, monkeypatch, older):
    """The old-daemon reason is gone with the version check (comms/refine §3.10, items 4
    and 5): a `session` query refused as an unknown kind is a failed query, and an
    answer with no verdict reads `unknown`. Each refuses the post, exit 6, as a read
    comms could not make (§3.8.3)."""
    from moxi_comms.daemon import Refusal
    shim(rig, SESS_A, age=60)
    real = rig.daemon._session_query

    def old(frame):
        if older == "no session query":
            raise Refusal("unknown query kind 'session'")
        answer = real(frame)
        for key in ("cache_kind", "age_seconds", "cache_reason"):
            answer.pop(key)
        return answer

    monkeypatch.setattr(rig.daemon, "_session_query", old)
    code, out = send(rig, REVIEWER_Q)
    assert code == cli.EXIT_COLD and posted(rig) == []
    assert out.splitlines()[0] == {
        "no session query": ("Nothing was sent: comms could not ask the daemon about "
                             "reviewer@proj-a (the session query failed (unknown query "
                             "kind 'session'))."),
        "no verdict": ("Nothing was sent: comms could not read reviewer@proj-a's cache "
                       "(no reason given)."),
    }[older]
    assert "older than this comms" not in out
    assert out.splitlines()[-1] == "Nothing was sent."


def test_an_unbound_recipient_waits_and_is_told_what_releases_it(rig):
    """Not bound: posted, a row, and the NOTE says §3.5's releases for a name that is
    not bound (comms/refine §3.8.2)."""
    code, out = send(rig, "nobody-yet", "hello", "--new-name")
    assert code == 0 and len(posted(rig)) == 1
    (msg,) = posted(rig)
    (q,) = [line.split()[1] for line in out.splitlines() if line.startswith("NOTE: ")]
    assert out.splitlines()[-1] == cli.NOT_BOUND_NOTE.format(name=q)
    assert wait_until(lambda: msg.id in held_of(rig, q))


def test_held_reads_the_cache_and_a_cold_recipient_waits_as_flagged(rig):
    """`--held` reads the recipient's cache like any send (§3.8.1): a cold read posts
    and the message waits as flagged, with the `--held` NOTE and no `held:` line (the
    flag, not the read, makes the row), so no usage run."""
    s = shim(rig, SESS_A, age=9 * HOUR)
    code, out = send(rig, REVIEWER_Q, "later", "--held")
    assert code == 0, out
    (msg,) = posted(rig)
    assert cli.HELD_NOTE.format(name=REVIEWER_Q) in out.splitlines()
    assert not any(line.startswith("held: ") for line in out.splitlines())
    assert wait_until(lambda: msg.id in held_of(rig))
    time.sleep(0.1)
    assert s.queue == []


def test_held_to_a_recipient_comms_cannot_read_is_refused(rig):
    """[F7]: an unknown read refuses a flagged send too; it is not held."""
    rig.shim(**REVIEWER, session_id=None)
    code, out = send(rig, REVIEWER_Q, "later", "--held")
    assert code == cli.EXIT_COLD and posted(rig) == [], out
    assert out.splitlines()[0] == ("Nothing was sent: comms could not read "
                                   "reviewer@proj-a's cache (no session id reported yet).")
    assert held_of(rig) == set()


def test_the_hold_text_for_an_agent(rig):
    shim(rig, SESS_A, age=2 * HOUR + 600)
    write_transcript(SESS_A, 2 * HOUR + 600)
    code, out = send(rig, REVIEWER_Q)
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("sent ")
    # "went cold" is the age less the lifetime: last turn 2h10m ago, cold for 1h10m.
    assert lines[1].startswith("held: reviewer@proj-a's cache went cold 1h10m ago (last "
                               "turn ")
    assert lines[1].endswith(")."), lines[1]       # the line ends after its cold sentence
    assert lines[2:] == [HELD_LAST]


def test_the_hold_text_on_the_operators_lane(rig):
    if rig.backend != "local":
        pytest.skip("the credentialled operator lane is local's; the hold comes first, "
                    "but the lane needs the store to be the lane at all")
    shim(rig, SESS_A, age=2 * HOUR)
    code, out = send(rig, REVIEWER_Q, who=AS_OPERATOR)
    assert code == 0, out
    assert out.splitlines()[-1] == (HELD_LAST + " To reach it now, type into its session "
                                    "yourself.")


def test_the_refusal_text_for_an_unreadable_codex_recipient(rig, adapters):
    """comms/refine §3.8.3: a codex agent comms cannot read refuses the whole post, the
    cold reviewer beside it included; "Nothing was sent." first and last, the way to
    reach the others, the retry, and no `held:` line and no re-run line."""
    shim(rig, SESS_A, age=2 * HOUR)
    adapters(slot=5, name="coder", scope="proj-a", adapter="codex")
    code, out = send(rig, "reviewer@proj-a,coder@proj-a")
    assert code == cli.EXIT_COLD and posted(rig) == []
    assert out.splitlines() == [
        "Nothing was sent: comms could not read coder@proj-a's cache (no thread id "
        "reported yet).",
        "To reach the other recipients now, send it again without coder@proj-a.",
        "Send it again in a moment; a read that succeeds holds or delivers it.",
        "Nothing was sent.",
    ]
    assert held_of(rig) == set() and held_of(rig, CODER_Q) == set()


def test_a_caller_past_the_verb_is_still_held(rig):
    """In-process, as voice calls the verb (from inside a session holding the sender,
    through its claim socket): `_send` is held -- posted, exit 0, and kept by the daemon
    for the recipient's own next turn."""
    shim(rig, SESS_A, age=2 * HOUR)
    args = SimpleNamespace(to=REVIEWER_Q, text="round the verb",
                           held=False, standing=False, audience=None,
                           posture="none", new=False, reply_to=None)
    out = io.StringIO()
    planner = SenderIdentity.for_slot(1, "planner@proj-a", "proj-a", name="planner")
    with rig.session_holding(planner) as claim_socket:
        ctx = cli.Context(gateway=rig.gateway, db_path=paths.directory_db(),
                          socket_path=paths.daemon_socket(),
                          env={"MOXI_COMMS_PROJECT": "proj-a",
                               "MOXI_COMMS_CLAIM_SOCKET": claim_socket}, out=out)
        code = cli._send(args, ctx, tags=[REVIEWER_Q], reply_to=None, channel_room=NAMES)
    assert code == 0, out.getvalue()
    assert out.getvalue().splitlines()[-1] == HELD_LAST
    (msg,) = posted(rig)
    assert wait_until(lambda: msg.id in held_of(rig))


# ================================================================== (c) --cold

def _lane_posts(rig, who):
    if who is AS_OPERATOR and rig.backend != "local":
        pytest.skip("a post on the operator's lane needs local's operator credential")


@pytest.mark.parametrize("who", LANES, ids=LANE_IDS)
def test_send_cold_is_argparses_error_from_every_sender(rig, bind, who):
    """comms/refine §6.4: `--cold` is gone, so from every sender it is argparse's
    "unrecognized arguments", exit 2, and nothing is posted."""
    _lane_posts(rig, who)
    _bind_lane(bind, who)
    shim(rig, SESS_A, age=60)
    with pytest.raises(SystemExit) as exc:
        send(rig, REVIEWER_Q, "anyway", "--cold", who=who)
    assert exc.value.code == 2
    assert posted(rig) == []


@pytest.mark.parametrize("who", LANES, ids=LANE_IDS)
def test_reply_cold_is_argparses_error_from_every_sender(rig, bind, who):
    _lane_posts(rig, who)
    _bind_lane(bind, who)
    shim(rig, SESS_A, age=60)
    ask = rig.post_from_slot(6, ["planner@proj-a"], "q?", display=REVIEWER_Q)
    with pytest.raises(SystemExit) as exc:
        rig.cli(["reply", ask.id, "a.", "--cold"], slot=who["slot"],
                name=who["name"], scope=who["scope"], env=who["env"])
    assert exc.value.code == 2
    assert posted(rig) == [ask]


@pytest.mark.parametrize("verb", ["send", "reply"])
def test_help_does_not_teach_cold(verb, capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([verb, "--help"])
    assert "--cold" not in capsys.readouterr().out


def test_the_cure_never_carries_cold():
    """It carries `--held` (every recipient takes it, comms/refine §3.8.1), never
    `--cold`."""
    args = SimpleNamespace(cold=True, held=True, standing=False, posture=None)
    assert cli._cure_flags(args) == " --held"


# ========================================================== (d) the daemon's gate

def test_a_cold_shim_recipient_is_held_then_released_by_its_own_warm_turn(rig):
    """Cold-held (comms/refine §3.5): a row. A turn that ends on a cold read releases
    nothing; the turn that warms the cache does, `held: true`. The row stays until a
    `turn` names the id handled ([R2])."""
    s = shim(rig, SESS_A, age=2 * HOUR)
    msg = post(rig, [REVIEWER_Q])
    wait_until(lambda: msg.id in held_of(rig))
    time.sleep(0.1)
    assert s.queue == [] and held_of(rig) == {msg.id}
    send_turn(s)                               # a turn end while still cold
    time.sleep(0.2)
    assert s.queue == []
    write_transcript(SESS_A, 5)                # its own turn: the clock is warm again
    send_turn(s)                               # the Stop of that turn
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == msg.id and payload["held"] is True
    assert held_of(rig) == {msg.id}, "the row stays until the adapter names it handled"
    send_turn(s, handled=[msg.id])
    assert wait_until(lambda: held_of(rig) == set())
    assert wait_until(lambda: record_rows(rig.gateway, REVIEWER_Q) == set())


def test_cold_held_mail_waits_while_newer_mail_goes_once_warm(rig):
    """No piggyback ([F3]): once the cache is warm again, a newer message goes alone;
    the cold-held row waits for the name's next release point, its own turn."""
    s = shim(rig, SESS_A, age=2 * HOUR)
    first = post(rig, [REVIEWER_Q], "first")
    wait_until(lambda: first.id in held_of(rig))
    write_transcript(SESS_A, 5)
    second = post(rig, [REVIEWER_Q], "second")
    wait_for_queue(s, 1)
    time.sleep(0.2)
    assert queued_ids(s) == [second.id] and held_of(rig) == {first.id}
    send_turn(s)
    assert queued_ids(wait_for_queue(s, 2)) == [second.id, first.id]


def test_a_cold_bare_binding_is_held_and_released_by_its_own_warm_turn(rig, adapters):
    """[F8]: a bare binding is no branch. Cold, its mail is a row, never refused. A
    newer message on a warm read goes alone ([F3]), a rescan hands nothing more, and the
    binding's next turn on the warm read releases the row."""
    helper = bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    msg = post(rig, [HELPER_Q])
    assert wait_until(lambda: msg.id in held_of(rig, HELPER_Q))
    write_transcript(SESS_D, 5)                # warm now
    newer = post(rig, [HELPER_Q], "newer")
    wait_until(lambda: helper.ids())
    rig.daemon.rescan()
    time.sleep(0.1)
    assert helper.ids() == [newer.id], "the row waits for a release point, not other mail"
    raw_turn(helper, HELPER_Q)
    wait_until(lambda: len(helper.ids()) == 2)
    assert helper.ids() == [newer.id, msg.id]
    assert helper.delivered()[1]["held"] is True
    raw_turn(helper, HELPER_Q, handled=[newer.id, msg.id])
    assert wait_until(lambda: held_of(rig, HELPER_Q) == set())


def cold_harness_report(kind, tmp_path) -> dict:
    """A codex or opencode binding's report over a cache that reads cold: a rollout
    whose last request was 3 hours ago, an opencode session whose last request on a
    5-minute row was 10 minutes ago (`test_cache_clock.py` builds both)."""
    from test_cache_clock import (BUILD_NULL, SID, TID, real_oc, real_rollout,
                                  sessions_root)
    if kind == "codex":
        root = sessions_root(tmp_path)
        real_rollout(root, 3 * HOUR)
        return {"session_id": TID, "cache_source": str(root), "turned": True}
    db = real_oc(tmp_path, 600)
    return {"session_id": SID, "cache_source": str(db.path), "turned": True,
            "agent_model": BUILD_NULL}


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_a_codex_or_opencode_recipient_is_held(rig, adapters, kind, tmp_path):
    """[F5]: a cold codex or opencode agent is held like any, a row, not refused. Its
    compaction (`ready source=compact`) releases the row with no read (§3.5)."""
    coder = adapters(slot=5, name="coder", scope="proj-a", adapter=kind,
                     report=cold_harness_report(kind, tmp_path))
    assert rig.daemon._read(rig.daemon.bindings[CODER_Q]).kind == "cold"
    msg = post(rig, [CODER_Q])
    assert wait_until(lambda: msg.id in held_of(rig, CODER_Q))
    time.sleep(0.1)
    assert coder.ids() == []
    raw_ready(coder, CODER_Q, "compact")
    wait_until(lambda: coder.ids())
    assert coder.ids() == [msg.id] and coder.delivered()[0]["held"] is True


def test_a_model_with_no_lifetime_row_reads_no_cache_and_goes_live(rig, adapters,
                                                                    tmp_path):
    """No lifetime row, live ([I3], [C4], §3.6): an opencode model comms knows no
    lifetime for reads `no-cache`, with the reason saying so, and its mail is delivered
    live, never queued for the missing row."""
    from test_cache_clock import BUILD_NULL, SID, real_oc
    db = real_oc(tmp_path, 3 * HOUR, provider="nowhere", model="unlisted-model")
    coder = adapters(slot=5, name="coder", scope="proj-a", adapter="opencode",
                     report={"session_id": SID, "cache_source": str(db.path),
                             "turned": True, "agent_model": BUILD_NULL})
    clock = rig.daemon._read(rig.daemon.bindings[CODER_Q])
    assert (clock.kind, clock.code) == ("no-cache", cache.NO_ROW), clock
    msg = post(rig, [CODER_Q])
    assert wait_until(lambda: coder.ids() == [msg.id])
    assert held_of(rig, CODER_Q) == set()


def test_deferred_mail_at_a_bind_into_a_cold_resumed_session_is_held(rig):
    """Offline mail is a row (§3.3.2); a register into a cold resumed session releases
    nothing ([F10]), and the first turn on a warm read does."""
    msg = post(rig, [REVIEWER_Q], "while you were away")
    assert wait_until(lambda: msg.id in held_of(rig))
    s = shim(rig, SESS_A, age=5 * HOUR)        # `--resume`: the old transcript, cold
    wait_until(lambda: not s.replaying)
    time.sleep(0.1)
    assert s.queue == [] and held_of(rig) == {msg.id}
    write_transcript(SESS_A, 5)
    send_turn(s)
    (payload,) = wait_for_queue(s, 1)
    assert payload["message_id"] == msg.id and payload["held"] is True


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_m7_held_mail_at_a_codex_bind_waits_for_a_real_boundary(rig, adapters, kind,
                                                                tmp_path):
    """Every adapter takes held mail ([F5]): a flagged row waits through a codex or
    opencode register ([R7]), never refused, and goes at its session start."""
    held = post(rig, [CODER_Q], "later", next_turn=True)
    assert wait_until(lambda: held.id in held_of(rig, CODER_Q))
    coder = adapters(slot=5, name="coder", scope="proj-a", adapter=kind,
                     report=cold_harness_report(kind, tmp_path))
    time.sleep(0.2)
    assert coder.ids() == [] and held_of(rig, CODER_Q) == {held.id}
    raw_ready(coder, CODER_Q, "startup")
    wait_until(lambda: coder.ids())
    assert coder.ids() == [held.id] and coder.delivered()[0]["held"] is True


@pytest.mark.parametrize("anchor,released", [("", True), ("cold", False)],
                         ids=["fresh", "cold"])
def test_m7_held_mail_at_a_bare_binding_bind(rig, adapters, anchor, released):
    """A flagged row waits through a bare binding's register on any read ([R7]); its
    next turn releases it on a passing read (a transcript with no request reads fresh)
    and not on a cold one ([F8])."""
    held = post(rig, [HELPER_Q], "later", next_turn=True)
    assert wait_until(lambda: held.id in held_of(rig, HELPER_Q))
    helper = bare_shim(adapters, sid="" if anchor == "" else SESS_D,
                    age=None if anchor == "" else 2 * HOUR)
    wait_until(lambda: HELPER_Q in rig.daemon.bindings)
    time.sleep(0.2)
    assert helper.ids() == [] and held_of(rig, HELPER_Q) == {held.id}
    if anchor == "":
        write_transcript(SESS_BLANK, None)    # a turn, and still no request: fresh
    raw_turn(helper, HELPER_Q)
    if released:
        assert wait_until(lambda: helper.ids() == [held.id])
        assert helper.delivered()[0]["held"] is True
    else:
        time.sleep(0.2)
        assert helper.ids() == [] and held_of(rig, HELPER_Q) == {held.id}


def test_replays_cursor_never_passes_a_cold_held_id(rig):
    cursors.advance(REVIEWER_Q, "1")
    before = post(rig, [REVIEWER_Q], "while unbound")
    assert wait_until(lambda: before.id in held_of(rig))
    write_transcript(SESS_A, 3 * HOUR)
    pushes = Pushes()
    c = DaemonClient(daemon_socket(), on_push=pushes).connect()
    try:
        c.register(slot=2, name="reviewer", scope="proj-a", adapter="claude-shim",
                   session_id=SESS_A, projects_root=str(projects_root()), turned=True)
        wait_until(lambda: any(f.get("op") == "replay_complete" for f in pushes.frames))
        (done,) = [f for f in pushes.frames if f.get("op") == "replay_complete"]
        assert pushes.delivered() == []
        assert done["cursor"] in (None, "1") and int(done["cursor"]) < int(before.id)
        assert held_of(rig) == {before.id}
    finally:
        c.close()


def test_a_cold_row_survives_a_restart_and_a_shim_that_registers_at_once(rig, reboot):
    """The row is in the record (§3.3.1), not a gate file: the restarted daemon loads it
    before the socket listens, and the cold shim's reconnect hands nothing."""
    s = shim(rig, SESS_A, age=2 * HOUR, autoreconnect=True,
             backoff=iter(lambda: 0.02, None))
    msg = post(rig, [REVIEWER_Q])
    assert wait_until(lambda: msg.id in held_of(rig))
    assert wait_until(lambda: record_rows(rig.gateway, REVIEWER_Q) == {msg.id})
    assert not paths.cold_gate().exists()
    daemon = reboot(rig)
    assert held_of(rig) == {msg.id}, "loaded before the socket listened"
    wait_until(lambda: REVIEWER_Q in daemon.bindings and not s.offline)
    wait_until(lambda: not s.replaying)
    time.sleep(0.2)
    assert s.queue == [] and held_of(rig) == {msg.id}


def test_row_ids_are_skipped_by_the_rescan(rig, adapters):
    """[R61]: only a release point hands a row id; a rescan on a warm read skips it."""
    helper = bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    msg = post(rig, [HELPER_Q])
    assert wait_until(lambda: msg.id in held_of(rig, HELPER_Q))
    write_transcript(SESS_D, 5)
    for _ in range(3):
        rig.daemon.rescan()
    time.sleep(0.1)
    assert helper.ids() == [] and held_of(rig, HELPER_Q) == {msg.id}


@pytest.mark.parametrize("anchor", [SESS_D, ""], ids=["warm", "blank"])
def test_a_cold_bare_bindings_row_survives_its_unbind_and_a_restart_and_goes_at_the_next_bind(
        rig, adapters, reboot, anchor):
    """[R2]: a row is never deleted at unbind nor by a restart. The next binding's
    register releases it (an unflagged row, on a passing read, [R7]), `held: true`,
    whether the session now reads warm or has made no request."""
    helper = bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    msg = post(rig, [HELPER_Q])
    assert wait_until(lambda: msg.id in held_of(rig, HELPER_Q))
    helper.client.close()
    wait_until(lambda: HELPER_Q not in rig.daemon.bindings)
    assert held_of(rig, HELPER_Q) == {msg.id}
    write_transcript(SESS_D, 5)                       # warm, for the SESS_D round
    reboot(rig)
    assert held_of(rig, HELPER_Q) == {msg.id}
    again = bare_shim(adapters, sid=anchor, slot=3)
    assert wait_until(lambda: again.ids() == [msg.id]), anchor
    assert again.delivered()[0]["held"] is True


def test_the_daemon_refuses_to_start_when_the_queue_load_raises(rig, monkeypatch):
    """A daemon without its rows would hand nothing at any release point and write rows
    over an empty memory: a queue read that raises stops the start (the queue slice's
    departure 7)."""
    rig.daemon.stop()

    def broken():
        raise RuntimeError("the record's queue could not be read")

    monkeypatch.setattr(rig.gateway, "queue_rows", broken)
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    with pytest.raises(RuntimeError, match="queue could not be read"):
        daemon.start()
    assert not daemon.bound and not daemon_socket().exists()


def test_the_trim_keeps_a_cold_bare_bindings_row_and_takes_the_message_once_handled(
        rig, adapters):
    """[R9]: a row keeps its message past retention; once the adapter names it handled
    the row goes, and the trim takes the message like any other."""
    if rig.backend != "local":
        pytest.skip("only the local record trims")
    helper = bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    msg = post(rig, [HELPER_Q])
    assert wait_until(lambda: record_rows(rig.gateway, HELPER_Q) == {msg.id})
    rig.age_message(msg.id, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(rig.clock() - 200 * 86400)))
    rig.daemon.trim_record()
    assert rig.gateway.get_message(msg.id) is not None
    write_transcript(SESS_D, 5)
    raw_turn(helper, HELPER_Q)
    assert wait_until(lambda: helper.ids() == [msg.id])
    raw_turn(helper, HELPER_Q, handled=[msg.id])
    assert wait_until(lambda: record_rows(rig.gateway, HELPER_Q) == set())
    rig.daemon.trim_record()
    assert rig.gateway.get_message(msg.id) is None


def test_hazard_1_a_cursor_past_a_cold_held_id_leaves_its_row(rig):
    """[R2]: a row is never deleted by comparing its id with the cursor; a rescan keeps
    it, in memory and in the record."""
    shim(rig, SESS_A, age=2 * HOUR)
    msg = post(rig, [REVIEWER_Q])
    wait_until(lambda: msg.id in held_of(rig))
    cursors.advance(REVIEWER_Q, msg.id)
    rig.daemon.rescan()
    assert held_of(rig) == {msg.id}
    assert wait_until(lambda: record_rows(rig.gateway, REVIEWER_Q) == {msg.id})


def test_a_delivered_message_re_read_after_the_recipient_went_cold_writes_no_row(
        rig, adapters):
    """Re-check v0.6 finding 7 (b): a rescan re-reading ordinary mail a binding was
    already handed is no delivery: no row ([R61])."""
    helper = bare_shim(adapters, sid=SESS_D, age=60)
    msg = post(rig, [HELPER_Q])
    wait_until(lambda: helper.ids() == [msg.id])
    write_transcript(SESS_D, 3 * HOUR)                  # it went cold since
    rig.daemon.rescan()
    time.sleep(0.1)
    assert held_of(rig, HELPER_Q) == set()
    assert record_rows(rig.gateway, HELPER_Q) == set()
    assert helper.ids() == [msg.id]


@pytest.mark.parametrize("who", ["shim", "bare"])
def test_hazard_2_a_gate_that_raises_reads_unknown_and_the_sends_cold_at_decides(
        rig, adapters, monkeypatch, who):
    """A read that raises is `unknown` ([R8]); the message is posted and never dropped.
    The send's cold-at decides it ([C12]): `-` or a time still ahead of the daemon's
    clock delivers; a time reached, or no `[cold-at:…]` token at all ([R72]), makes a
    row and no delivery."""
    def broken(*a, **kw):
        raise RuntimeError("the clock fell over")

    monkeypatch.setattr(cache, "clock_state", broken)
    if who == "shim":
        s = shim(rig, SESS_A, age=60)
        q, got = REVIEWER_Q, (lambda: queued_ids(s))
    else:
        helper = bare_shim(adapters, sid=SESS_D, age=60)
        q, got = HELPER_Q, helper.ids
    now = int(rig.clock())
    none = post(rig, [q], "no token")
    reached = post(rig, [q], "reached", cold_at=(now,))
    ahead = post(rig, [q], "ahead", cold_at=(now + HOUR,))
    dash = post(rig, [q], "dash", cold_at=(machine_line.COLD_AT_LIVE,))
    assert wait_until(lambda: held_of(rig, q) == {none.id, reached.id})
    assert wait_until(lambda: len(got()) == 2)
    time.sleep(0.1)
    assert got() == [ahead.id, dash.id]
    assert held_of(rig, q) == {none.id, reached.id}


def test_the_trim_keeps_cold_held_ids(rig):
    if rig.backend != "local":
        pytest.skip("only the local record trims")
    shim(rig, SESS_A, age=2 * HOUR)
    msg = post(rig, [REVIEWER_Q])
    wait_until(lambda: msg.id in held_of(rig))
    rig.age_message(msg.id, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(rig.clock() - 200 * 86400)))
    rig.daemon.trim_record()
    assert rig.gateway.get_message(msg.id) is not None


def test_comms_explain_names_a_cold_shims_and_a_cold_bare_bindings_mail_waiting(rig, adapters):
    """One queue, one case (§3.11): cold-held mail is `waiting`, on every adapter."""
    shim(rig, SESS_A, age=2 * HOUR)
    held = post(rig, [REVIEWER_Q])
    bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    other = post(rig, [HELPER_Q])
    wait_until(lambda: held.id in held_of(rig) and other.id in held_of(rig, HELPER_Q))
    for name, q, mid in ((REVIEWER_Q, REVIEWER_Q, held.id), (HELPER_Q, HELPER_Q, other.id)):
        code, out = rig.cli(["explain", name, mid], **PLANNER)
        assert code == 0 and out.splitlines()[0].startswith(f"{q}   {cursors.WAITING}"), out
        assert ("This message waits in the queue for the name's next release point"
                in out.replace("\n  ", " "))


def test_comms_agents_counts_cold_held_mail(rig):
    """`agents` answers `waiting: {q: {count, oldest}}` from the queue, and `pending`
    always empty ([R63], §3.11). How the roster renders `waiting` is §7.3's (the roster
    slice)."""
    shim(rig, SESS_A, age=2 * HOUR)
    post(rig, [REVIEWER_Q], "one")
    post(rig, [REVIEWER_Q], "two")
    wait_until(lambda: len(held_of(rig)) == 2)
    c = DaemonClient(daemon_socket()).connect()
    try:
        view = c.query("agents")
    finally:
        c.close()
    assert view["waiting"][REVIEWER_Q]["count"] == 2
    assert view["pending"] == {} and "held" not in view
    code, out = rig.cli(["agents", "--seats"], **PLANNER)
    assert f"  {'reviewer@proj-a':<24} 2 message(s), oldest waiting " in out


# ================================================================ (e) the race

def test_checked_warm_and_cold_at_delivery_is_held(rig, monkeypatch):
    """The verb reads warm; the clock crosses the hour before the daemon routes the
    post. The daemon's gate holds it, after the sender was told `sent` (§6.7, P17)."""
    s = shim(rig, SESS_A, age=60)
    real = sending.post

    def late(*a, **kw):
        write_transcript(SESS_A, HOUR + 5)          # the hour passes in between
        return real(*a, **kw)

    monkeypatch.setattr(sending, "post", late)
    code, out = send(rig, REVIEWER_Q, "just in time?")
    assert code == 0 and out.startswith("sent ")
    mid = out.split()[1]
    wait_until(lambda: mid in held_of(rig))
    time.sleep(0.1)
    assert s.queue == []


# ===================================================== (f) [R16] and [R17]

def test_r16_a_turn_on_a_cold_recipient_releases_nothing(rig):
    """A `turn` releases on a passing read only (§3.5): a forged turn end over a cold
    transcript releases nothing; once a genuine turn wrote its fresh line, it does.
    (The frames are sent raw: the Claude shim's `handled` is the shim slice's.)"""
    s = shim(rig, SESS_A, age=2 * HOUR)
    held = post(rig, [REVIEWER_Q], "later", next_turn=True)
    wait_until(lambda: held.id in held_of(rig))
    send_turn(s)                                          # a forged Stop: still cold
    time.sleep(0.2)
    assert s.queue == [] and held_of(rig) == {held.id}
    write_transcript(SESS_A, 3)                           # a genuine Stop's fresh line
    send_turn(s)
    assert queued_ids(wait_for_queue(s, 1)) == [held.id]


def test_r16_a_fresh_read_passes_at_a_turn(rig):
    """comms/refine §3.6 replaces [R16]'s "fresh is not warm": a release point's read
    passes on `warm`, `fresh` and `no-cache` alike (`cache.PASSING`), so a `turn` over a
    transcript with no request yet releases the row."""
    s = shim(rig, SESS_A, age=None)            # a transcript with no request yet
    held = post(rig, [REVIEWER_Q], "later", next_turn=True)
    wait_until(lambda: held.id in held_of(rig))
    assert rig.daemon._read(rig.daemon.bindings[REVIEWER_Q]).kind == "fresh"
    send_turn(s)
    assert queued_ids(wait_for_queue(s, 1)) == [held.id]


def test_r17_a_second_binding_reporting_a_held_session_id_is_refused(rig, caplog):
    first = shim(rig, SESS_A, age=60)
    with caplog.at_level("WARNING", logger="comms.daemon"):
        other = rig.shim(slot=4, name="main", scope="proj-a", session_id=SESS_A)
    assert rig.daemon.bindings[REVIEWER_Q].session_id == SESS_A, "the first keeps it"
    assert rig.daemon.bindings["main@proj-a"].session_id is None
    assert "held by the live binding reviewer@proj-a" in caplog.text
    other.note_session(SESS_A)                           # and on an activity too
    other.notify_activity(None)
    time.sleep(0.1)
    assert rig.daemon.bindings["main@proj-a"].session_id is None
    code, out = send(rig, "main@proj-a")
    assert code == cli.EXIT_COLD and posted(rig) == [], "no id: unknown, and refused"
    assert out.splitlines()[0] == ("Nothing was sent: comms could not read main@proj-a's "
                                   "cache (no session id reported yet).")
    assert first.identity == REVIEWER_Q


# ===================================================== (g) never_held, retired

def _policy_table(body):
    from moxi_comms import config
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[policy]\n" + body)


def test_comms_doctor_prints_no_never_held_line(rig):
    """[R62]: no doctor row for the `[policy]` table (comms/refine §3.8.5). A retired
    key, such as `never_held`, now fails the load (`test_cold_sends.py`)."""
    _policy_table('cache_callers = ["x@y"]\n')
    code, out = rig.cli(["doctor"])
    assert not [x for x in out.splitlines() if x.startswith("policy")]


# =========================================================== (h) moxi comms cache

def test_comms_cache_reads_its_kind_from_the_daemon(rig, monkeypatch):
    shim(rig, SESS_A, age=60)                 # the transcript is warm ...
    real = rig.daemon._session_query

    def says_cold(frame):
        return {**real(frame), "cache_kind": "cold", "age_seconds": 3 * HOUR}

    monkeypatch.setattr(rig.daemon, "_session_query", says_cold)   # ... the daemon's not
    code, out = rig.cli(["cache", "reviewer@proj-a"], env={"MOXI_COMMS_OPERATOR": "900001"})
    assert code == 0 and out.startswith("reviewer@proj-a: cache expired 2 hours ago."), out


def test_comms_cache_reads_a_raw_id_in_process(rig):
    write_transcript(SESS_B, 120)
    code, out = rig.cli(["cache", SESS_B, SESS_C], env={"MOXI_COMMS_OPERATOR": "900001"})
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith(f"{SESS_B}: cache warm, last turn 2 minutes")
    assert lines[1].startswith(f"{SESS_C}: cache state not known (no transcript for "
                               f"session {SESS_C}")


# ====================================================== (i) the wrong root holds

def _old_shim_register(session_id, **extra):
    """A register as a shim built before v0.6 sends it: a session id, no root."""
    pushes = Pushes()
    c = DaemonClient(daemon_socket(), on_push=pushes).connect()
    c.register(slot=2, name="reviewer", scope="proj-a", adapter="claude-shim",
               session_id=session_id, **extra)
    pushes.client = c
    return pushes


@pytest.mark.parametrize("daemon_root", ["empty", "missing"])
def test_an_older_shims_binding_under_a_wrong_daemon_root_is_held(rig, monkeypatch,
                                                                  reboot, tmp_path,
                                                                  daemon_root):
    elsewhere = tmp_path / "elsewhere"
    write_transcript(SESS_A, 3 * HOUR, root=elsewhere)
    cfg = tmp_path / "daemon-claude"              # the daemon's own CLAUDE_CONFIG_DIR
    if daemon_root == "empty":
        (cfg / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    for _ in range(2):
        old = _old_shim_register(SESS_A)
        msg = post(rig, [REVIEWER_Q])
        wait_until(lambda: msg.id in held_of(rig))
        time.sleep(0.1)
        assert old.ids() == []
        old.client.close()
        wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
        reboot(rig)                                       # and again after a restart


def test_a_v06_shim_whose_reported_root_lacks_its_transcript_is_held_after_a_prompt(
        rig, tmp_path):
    write_transcript(SESS_A, 3 * HOUR, root=tmp_path / "real")   # where it really is
    s = rig.shim(**REVIEWER, session_id=SESS_A)                  # reports the rig root
    assert rig.daemon._read(rig.daemon.bindings[REVIEWER_Q]).kind == "fresh"
    shim_drive.turn_started(s)
    # The prompt alone reaches the daemon: no Stop, no idle tick, no other frame.
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].turned)
    msg = post(rig, [REVIEWER_Q])
    wait_until(lambda: msg.id in held_of(rig))
    assert s.queue == []


def test_a_prompt_alone_tells_the_daemon_and_releases_nothing(rig, monkeypatch):
    """S2: a turn's start sends an `activity` with no `kind`, so the daemon's
    `turned` is set at the prompt and held next-turn mail stays held until the turn
    ends."""
    s = shim(rig, SESS_B)                     # never prompted: fresh
    held = post(rig, [REVIEWER_Q], "after your turn", next_turn=True)
    wait_until(lambda: held.id in held_of(rig))
    frames = []
    real_notify = s._client.notify
    monkeypatch.setattr(s._client, "notify",
                        lambda op, **kw: frames.append((op, kw)) or real_notify(op, **kw))
    shim_drive.turn_started(s)
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].turned)
    assert rig.daemon._read(rig.daemon.bindings[REVIEWER_Q]).kind == "unknown"
    (activity,) = [kw for op, kw in frames if op == "activity"]
    assert "kind" not in activity and activity["turned"] is True
    time.sleep(0.1)
    assert held.id in held_of(rig) and s.queue == []


def test_a_never_prompted_v06_shim_is_delivered_and_again_after_a_restart(rig, reboot):
    s = shim(rig, SESS_B, autoreconnect=True, backoff=iter(lambda: 0.02, None))
    first = post(rig, [REVIEWER_Q], "first task")
    wait_for_queue(s, 1)
    daemon = reboot(rig)
    wait_until(lambda: REVIEWER_Q in daemon.bindings and not s.offline)
    second = post(rig, [REVIEWER_Q], "second")
    assert queued_ids(wait_for_queue(s, 2)) == [first.id, second.id]


# ===================================================== (j) the shim's evidence

def test_projects_root_comes_from_the_shims_own_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    s = ClaudeShim(name="x", scope="y", session_id=SESS_A)
    assert s.projects_root == str(tmp_path / "cfg" / "projects")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cache.projects_root(os.environ) == str(tmp_path / "home" / ".claude" / "projects")


def test_with_cache_absent_the_shim_states_no_root(monkeypatch, tmp_path):
    # Nothing would read it: with no cache module every agent reads `no-cache`.
    cache_absent(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert ClaudeShim(name="x", scope="y", session_id=SESS_A).projects_root is None


def test_turned_is_false_at_spawn_and_set_by_a_prompt_and_a_turn_end(rig):
    s = shim(rig, SESS_A, age=60)
    assert s.turned is False and rig.daemon.bindings[REVIEWER_Q].turned is False
    for op in (shim_drive.PROMPT, shim_drive.TURN_BOUNDARY, shim_drive.TURN_FAILED):
        with s._lock:
            s.turned = False
        shim_drive.drive(s, op)
        assert s.turned is True, op


def test_the_idle_tick_sets_no_turned_before_any_prompt(rig):
    """Re-check finding 3: the idle tick injecting mail into a never-prompted session is
    not a turn. Its notify carries no `kind`, so the daemon reads no `turned` from it."""
    s = shim(rig, SESS_B)                                   # never prompted, no file
    msg = post(rig, [REVIEWER_Q], "first task")
    wait_for_queue(s, 1)
    deadline = time.monotonic() + 3
    while not s.injected and time.monotonic() < deadline:
        shim_drive.idle(s)
        time.sleep(0.05)
    assert s.injected and msg.id in s.injected[0]
    time.sleep(0.1)
    assert s.turned is False and rig.daemon.bindings[REVIEWER_Q].turned is False
    second = post(rig, [REVIEWER_Q], "second, before the injected turn writes anything")
    assert second.id in queued_ids(wait_for_queue(s, 1)), "still fresh, so delivered"


def test_a_clear_naming_a_new_id_lowers_turned(rig):
    s = shim(rig, SESS_A, age=60)
    s.note_turned()
    shim_drive.ready(s, "clear", SESS_B)
    assert (s.session_id, s.turned) == (SESS_B, False)
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].session_id == SESS_B)
    assert rig.daemon.bindings[REVIEWER_Q].turned is False


@pytest.mark.parametrize("source", ["fork", "resume", "startup", None])
def test_a_new_id_that_is_not_a_clear_keeps_turned_raised(rig, source):
    """Re-check finding 4: a new id minted by a fork (or `/branch`) carries its parent's
    context, so it must never read `fresh` before its first prompt. Only a `/clear`
    starts empty; any other new id sets `turned`."""
    s = shim(rig, SESS_A, age=60)
    s.note_session(SESS_C, source)
    assert s.turned is True
    s.notify_activity("ready")
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].session_id == SESS_C)
    assert rig.daemon.bindings[REVIEWER_Q].turned is True
    msg = post(rig, [REVIEWER_Q])                      # no transcript for SESS_C yet
    wait_until(lambda: msg.id in held_of(rig))


def test_both_fields_ride_register_and_every_activity(rig):
    s = shim(rig, SESS_A, age=60)
    b = rig.daemon.bindings[REVIEWER_Q]
    assert b.projects_root == str(projects_root()) and b.turned is False
    with s._lock:
        s.projects_root, s.turned = "/tmp/elsewhere", True
    s.notify_activity("ready")
    wait_until(lambda: b.projects_root == "/tmp/elsewhere" and b.turned)


def test_the_daemon_takes_them_only_from_the_bindings_own_connection(rig):
    shim(rig, SESS_A, age=60)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(daemon_socket()))
    conn = LineConn(s)
    try:
        conn.send({"op": "activity", "identity": REVIEWER_Q, "session_id": SESS_A,
                   "projects_root": "/tmp/forged", "turned": True, "kind": "turn",
                   "seq": 1})
        conn.send({"id": 1, "op": "ping"})
        assert conn.recv() == {"id": 1, "ok": True, "cli_protocol": CLI_PROTOCOL}
    finally:
        conn.close()
    b = rig.daemon.bindings[REVIEWER_Q]
    assert b.projects_root == str(projects_root()) and b.turned is False


def test_a_kind_turn_sets_turned_whatever_the_frame_says(rig):
    s = shim(rig, SESS_A, age=60)
    s._client.notify("activity", identity=REVIEWER_Q, session_id=SESS_A, turned=False,
                     kind="turn")
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].turned)


def test_a_frame_with_neither_field_leaves_projects_root_unset(rig):
    old = _old_shim_register(SESS_A)
    wait_until(lambda: REVIEWER_Q in rig.daemon.bindings)
    assert rig.daemon.bindings[REVIEWER_Q].projects_root is None
    old.client.notify("activity", identity=REVIEWER_Q, session_id=SESS_A)
    time.sleep(0.1)
    assert rig.daemon.bindings[REVIEWER_Q].projects_root is None
    old.client.close()


# ============================================== (k) the v0.6 re-check, finding 1

def test_the_daemon_never_lowers_turned_for_a_session_it_has_seen_turn(rig):
    """Re-check finding 1: a shim respawned in a session that has had turns starts
    `turned` false. The daemon keeps it raised for that id, so a transcript its root
    does not hold reads `unknown` (held), not `fresh`."""
    first = shim(rig, SESS_A)                         # never prompted, no file yet
    first.note_turned()
    first.notify_activity("turn")
    wait_until(lambda: rig.daemon.bindings[REVIEWER_Q].turned)
    first.stop()
    wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
    respawned = shim(rig, SESS_A)                     # an MCP reconnect: turned false
    assert respawned.turned is False
    assert rig.daemon.bindings[REVIEWER_Q].turned is True
    msg = post(rig, [REVIEWER_Q])
    wait_until(lambda: msg.id in held_of(rig))
    assert respawned.queue == []
