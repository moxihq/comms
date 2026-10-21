"""The comms daemon: one gateway connection, one routing table, in memory only.

Contract anchors:
  * routing lives in daemon MEMORY ONLY, rebuilt from live connections;
  * addressability = something that can deliver is connected right now;
  * the connection IS the liveness signal (close == deregistration);
  * the `block` setting is enforced HERE, symmetric, read at use;
  * catch-up replays past the cursor, bounded when the cursor is absent/stale, then
    `replay_complete`;
  * a gateway reconnect that could not RESUME triggers the #names rescan;
  * the queue: every message to a name is delivered live or waits
    in the name's queue, one table in the record (`queue`) mirrored in `queue` here.
    Live means: the name is bound, its cache reads `warm`, `fresh` or `no-cache` (or
    `unknown` while the send's `[cold-at:…]` for it is still ahead, or is `-`), and the
    message is not flagged `[held]`. Anything else waits: offline, cold, or flagged. A
    queued message goes out only at the name's next release point (a `ready` with a
    `source`, a `turn` carrying `handled`, or a register on a passing read), never
    because another message to the name went out. A turn end whose read does not pass
    stays open for a settle window (RULE H1, `RELEASE_SETTLE_STEPS`), read again until
    a read passes or the window ends. A row is deleted only when the adapter names its
    id `handled` on a `turn` frame from the binding that was handed it; never by cursor
    and never at unbind. An `unhandled` id, an id handed and not past the cursor at
    unbind, and a hand-off whose socket send failed get a row. Rows are never trimmed
    by age;
  * the audience mark: an absent audience on a reply is
    filled in at `_deliver` from an in-memory index of effective audiences, and what
    the index cannot answer is delivered as `unknown`, never fetched. The index holds
    three values: a tag, known none, or unknown, and unknown never decays
    into known none down a chain or on a re-index;
  * the ask index: `asks` holds each verified
    `[reply-once]` message this daemon has routed, with the names it still binds. It
    NOMINATES candidates for the send verb, which decides on its own read of the record;
    the daemon refuses, holds and rewrites nothing over a posture. Fed in
    `_route` only, forward-only, bounded to the newest `RESCAN_LIMIT` entries, and a
    missing entry only ever lets a send through.
  * the sent index: `sent` holds, per author,
    every `#names` message of the last `OWED_TTL` with its recipient tags as written, its
    posture, its `re:`, its time, whether the operator's session was up when it was
    routed, and its attribution. It is rebuilt at boot from one read of
    the record and fed in `_route` after the recipients are claimed, and it answers the
    shim's `query kind="owed"` at a Stop: whether each debt is paid, what each kept
    `re:` points at, who the session is waiting on, and whether mail is on its way. The
    daemon decides nothing from it; the shim does. Memory only, forward-only, a derived
    index of the record.
  * the hand-over index: `handed_at` holds, per (name, message id), when `_deliver`
    handed the message to that name's binding, in memory, trimmed past `THREAD_IDLE`.
    With `started_at` and the held and pending sets it answers the send verb's
    `query kind="readable"`, which the verb reads to tell when a thread went idle for
    the replier. It records delivery and decides nothing.
  * the unanswered record: a shim that drops a debt
    whose reminder was ignored says so with an `unanswered` notify from its binding's
    own connection. The daemon keeps these in memory until the owing name's next
    payment to that author lands in the sent index, the message passes `OWED_TTL`, or
    the owing binding ends; `moxi comms agents` lists them and the author's next prompt delta
    shows the ones naming it. No timer runs anywhere.

Routing stays memory-only. What the daemon also does is JOURNAL custody
(`bindings.py`): the two moments it alone witnesses -- the bind, and the
connection-close release -- are appended to `DATA/comms/bindings.db` so that replay
attribution can run the live match-gate against the past. The journal is evidence and
never a routing input: it is read for whether an identity has existed, by the send's
never-bound check only, and no lookup in it can make anything addressable. A journal that
will not open costs verification and that check, and nothing else.

The daemon holds no posting credential: it posts nothing to any room.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import math
import os
import signal
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import (attribution, bindings, cache, config, constants, cursors, directory,
               protocol)
from .attribution import Author
from .machine_line import (COLD_AT_LIVE, HELD_TOKEN, NEXT_TURN_TOKEN, POSTURE_NONE,
                           POSTURE_ONCE, parse as parse_machine_line)
from .gateway import Message
from . import kinds as kinds_mod
from .paths import cold_gate, daemon_socket, directory_db, slot_keeps
from .protocol import CLI_PROTOCOL, PROTOCOL_VERSION, LineConn
from .provision import NAMES
from .scoping import GLOBAL, NAME_RE, is_auto_name, qualify, split_tag, tag_matches

log = logging.getLogger("comms.daemon")

TIER_EPHEMERAL = attribution.TIER_EPHEMERAL
TIER_CHOSEN = attribution.TIER_CHOSEN
TIER_STANDING = attribution.TIER_STANDING

#: comms' own adapter kinds (`kinds.BUILT_IN`). A module adds kinds through the
#: `moxi.comms.kinds` group; the daemon accepts these and those (`Daemon.kinds`).
ADAPTER_KINDS = tuple(kinds_mod.BUILT_IN)

#: The kind a send-only identity binds with: a module's own process holding a name that
#: sends and receives nothing. The daemon takes it only for a directory row of kind
#: `send-only`, and takes such a row only with it.
SEND_ONLY_ADAPTER = "send-only"

#: The sent index is trimmed by age once per this many entries fed, so the cut is paid
#: rarely rather than once per message.
SENT_TRIM_EVERY = 200

#: Where a delivered payload's `audience` came from. The fourth state is None: no
#: audience on line zero and the message is not a reply.
AUDIENCE_WRITTEN = "written"        # the token was on this message's line zero
AUDIENCE_INHERITED = "inherited"    # filled in from the referent's effective audience
AUDIENCE_UNKNOWN = "unknown"        # a reply whose referent this daemon cannot answer for


class _UnknownAudience:
    """The index's third value: this message's effective
    audience could not be worked out -- a reply whose referent was not indexed, or was
    itself unknown. Distinct from None (known to carry no audience), so an unknown is
    delivered as `unknown` at every later hop instead of reading as "no mark"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AUDIENCE_INDEX_UNKNOWN"


#: The sentinel the audience index stores for an unknown effective audience.
AUDIENCE_INDEX_UNKNOWN = _UnknownAudience()

#: How far past its window the audience index may grow before it is cut back to it. The
#: cut sorts, so it is paid once per this many messages rather than once per message.
AUDIENCE_INDEX_SLACK = constants.RESCAN_LIMIT // 10

#: The ask index's slack past its window, for the same reason as the audience index's.
ASK_INDEX_SLACK = constants.RESCAN_LIMIT // 10

#: How often the daemon trims the record past retention, once past boot. Daily, checked
#: on the trim loop's tick.
TRIM_INTERVAL = 24 * 3600
#: How often the trim loop wakes to check `TRIM_INTERVAL`.
TRIM_TICK = 5.0

# `moxi comms explain`'s four cases, classified on demand
CASE_HANDLED = "handled"
CASE_NOT_ADDRESSABLE = "not-addressable"
CASE_IGNORED = "delivered-and-ignored"
CASE_TURN_INCOMPLETE = "delivered-but-the-turn-never-completed"
#: Not one of the four: the message waits in the name's queue, so nothing has handed it
#: out since it was queued. `moxi comms explain` checks it before the cursor, so a queued id
#: never reads `cursor-behind-it`.
CASE_WAITING = cursors.WAITING

#: The `kind` of an `activity` notify for a turn that ran and ended. With `handled` on
#: it, it is a release point and settles the ids it names.
ACTIVITY_TURN = "turn"
#: The `kind` of an `activity` notify for a session start, clear, compaction or resume.
#: With a `source` on it, it is a release point.
ACTIVITY_READY = "ready"
#: The `kind` of an `activity` notify for a Claude adapter's first look at its session's
#: turns endpoint while the session is idle: a release point, on a passing read, that
#: settles nothing and is no evidence of a turn (`turned` is left as the frame says).
ACTIVITY_LOOK = "look"
#: A `ready` source that starts a context with nothing cached to lose: it releases every
#: row without reading the cache. Any other source (`resume`, Claude Code's `fork`, a
#: value this daemon does not know) reads the cache first.
SOURCES_UNREAD = ("startup", "clear", "compact")
#: RULE H1's settle window: the seconds after a `turn` frame at which the daemon reads
#: the binding's cache again when the frame's own read did not pass. The harness can
#: write the turn's record after its end is reported (Claude Code drains its transcript
#: writes on a 100 ms timer), so the read at the frame can judge the cache as it was
#: before the turn. 8 s, the last step, is eighty times that timer and leaves room for a
#: loaded machine; a window costs at most this many reads.
RELEASE_SETTLE_STEPS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
#: The operator's identity. He has no session to hold mail for, so `[held]` is read as
#: absent for him: his mail is live when a session of his is bound and waits as offline
#: mail otherwise.
OPERATOR_NAME = "operator"

#: The record's meta key that says the queue's one-time migration has run.
META_QUEUE_MIGRATED = "queue_migrated"

#: The key a routing pass keeps in its memo beside the cursors: the cache's one read per
#: binding per pass.
_GATE_MEMO = "\0gate"

#: What each case means, for the person reading `moxi comms explain` because an agent has
#: gone quiet. These live beside the constants so there is one wording and it cannot
#: drift from the four.
CASE_REASONS = {
    CASE_HANDLED: (
        "The cursor is past this message. The turn completed and comms counts it "
        "handled."),
    CASE_NOT_ADDRESSABLE: (
        "Nothing holds this name now. There is no adapter left to have handled the "
        "message, and it waits in the record until a matching identity binds."),
    CASE_IGNORED: (
        "comms handed the message to a live adapter and the adapter never reported it "
        "reaching the model. Either the injection never happened or its ping was lost."),
    CASE_TURN_INCOMPLETE: (
        "The adapter reported the message reaching the model and no turn has completed "
        "since. The cursor moves on the adapter's turn boundary and on nothing else, "
        "so a session that died mid-turn stops exactly here."),
}


@dataclass
class SentEntry:
    """One `#names` message in the sent index.

    `tags` are the recipient tags exactly as written on line zero: the index keys on
    nothing `resolve` computes, because a bare tag qualifies differently live and at boot.
    """

    id: str
    author: str
    tags: tuple
    posture: str
    reply_to: str | None
    at: float
    verified: bool


@dataclass(eq=False)
class SettleWindow:
    """One open settle window (RULE H1): the binding whose `turn` frame read a cache
    that did not pass, when the frame arrived (`time.monotonic`), the steps it reads
    at, the ids the frame reported `unhandled` (never handed by its release) and how
    many reads it has made. `closed` is set once, by its pass, its end or whatever
    closed it early."""

    binding: "Binding"
    opened: float
    steps: tuple
    exclude: frozenset
    taken: int = 0
    closed: bool = False

    def due(self) -> float:
        return self.opened + self.steps[self.taken]


@dataclass
class Binding:
    conn: LineConn
    qualified: str
    name: str
    scope: str
    tier: str
    adapter: str
    slot_id: int | None
    bound_at: float
    replay_hwm: str = "0"     # the record's high-water mark when this binding was made
    delivered: set = field(default_factory=set)
    journal_id: int | None = None   # the custody interval to close at deregistration
    #: The live Claude session behind this binding, as its own adapter last reported it,
    #: and when. Written only from the connection that holds this binding: an
    #: `activity` frame's identity is not authenticated, and a forged id would point
    #: the send check at the wrong transcript. None until reported; an adapter built
    #: before this never does.
    session_id: str | None = None
    session_seen_at: float | None = None
    #: The evidence a missing transcript needs to read `fresh` (cold-sends): the root
    #: the session's own Claude Code writes under, as its shim reported it, and whether a
    #: prompt or a turn end has been seen in `session_id`. Taken only from this binding's
    #: own connection. A shim built before them reports neither, and its
    #: `projects_root` stays None, so its missing transcript reads `unknown`.
    projects_root: str | None = None
    turned: bool = False
    #: A codex or opencode binding's evidence for its cache clock, taken only from this
    #: binding's own connection: where its harness keeps the record the clock reads (the
    #: Codex sessions root, the OpenCode database), the launcher's word on `register`
    #: that its own attempt found no record and starts a new thread or session (cleared
    #: once an id is held), and the opencode default agent and its model, `MISSING`
    #: until reported and kept apart from a reported agent with no model.
    cache_source: str | None = None
    new_session: bool = False
    agent_model: object = cache.MISSING
    #: The newest id CLAIMED for this binding since it registered (what the `owed`
    #: query reads as undelivered mail). The ids a shim listed in `holding` at register
    #: are not claims, so a reconnect with no `injected_through` yet reads no mail waiting.
    handed_max: str = "0"
    #: The ids this binding's adapter named `handled` on a `turn` frame. Their rows are
    #: gone, and an unbind does not queue them again: a queued id can be handled while
    #: the cursor is still below it.
    handled: set = field(default_factory=set)
    #: Whether the binding's session has a turns endpoint its adapter can deliver
    #: through, as the adapter last reported it on its own connection, and since when
    #: (this daemon's clock). None when the adapter reports no such state (a Codex or
    #: OpenCode host is its own endpoint; a hand-run shim delivers by its own seam). A
    #: bound name whose session has no endpoint reads `waiting` to a sender: its adapter
    #: holds the mail until it finds one.
    endpoint: bool | None = None
    endpoint_since: float | None = None
    #: How many releases have begun for this binding, counted under the lock before a
    #: release reads the queue. A send's gate read notes it as the read begins: a send
    #: that would queue a row after a release that began since reads once more
    #: (`_route_recipients`), so a read made in the record's lag never queues a row
    #: behind the release that read past it.
    releases_begun: int = 0


@dataclass
class Keep:
    """A slot held for a name until an expiry, so a restart gives it back.

    `expires` is wall-clock epoch seconds, the value the keep mark carries across boots.
    `deadline` is the same moment on this process's monotonic clock, so a wall clock that
    steps BACKWARD mid-life cannot stretch a keep: it ends at whichever comes first. A
    forward step ends it early. Either way a clock jump only shortens a keep. `carried`
    says the keep came from an earlier boot's unclaimed keep rather than a binding that
    was live at the stop, for the boot line only.
    """
    qualified: str
    expires: float
    deadline: float
    carried: bool = False


@dataclass
class Resolution:
    tag: str
    name: str
    scope: str
    qualified: str
    tier: str
    binding: Binding | None
    resolvable: bool = True
    deferred: bool = False
    hint: str | None = None       # why it is unresolvable, in the sender's terms
    #: the directory row at (name, scope) is a send-only identity: never a recipient,
    #: bound or not
    send_only: bool = False


def _config_kept(exc) -> None:
    log.error("%s; keeping the last good settings until the file is fixed", exc)


class Daemon:
    def __init__(self, gateway, *, db_path=None, socket_path=None, clock=time.time,
                 start_trim_thread: bool = True, journal_path=None,
                 keeps_path=None, monotonic=time.monotonic,
                 first_start=None, gate_path=None, kinds=None, settings=None,
                 cache_store=cache.MISSING):
        self.gateway = gateway
        #: The adapter kinds a register may name: the built-in four and each kind the
        #: `moxi.comms.kinds` group declares (`kinds.load`), read once at construction.
        self.kinds = kinds if kinds is not None else kinds_mod.load()
        #: comms' settings, read at use with the last good kept (`config.Config`): a
        #: bad edit to the config file mid-run leaves `block` and `[replay]` as they
        #: were, and the error is logged once per change of the file. `main` starts it
        #: before anything else, so a file that is bad at start refuses the start.
        if settings is None:
            settings = config.Config(report=_config_kept)
            settings.start()
        self.settings = settings
        #: A step `start()` runs after the single-instance probe and before anything
        #: else, given (gateway, db_path): a daemon on the local backend provisions its
        #: directory here (`provision.first_start`). None elsewhere.
        self.first_start = first_start
        self.db_path = Path(db_path or directory_db())
        self.socket_path = Path(socket_path or daemon_socket())
        self.clock = clock
        #: The keep mark. `monotonic` is injectable so a test can step the wall clock
        #: without the monotonic one and show that a backward jump does not stretch a
        #: keep.
        self.keeps_path = Path(keeps_path or slot_keeps())
        #: The cold-cache gate file an older daemon kept (`paths.cold_gate`): read once,
        #: by the queue's migration, and then deleted.
        self.gate_path = Path(gate_path or cold_gate())
        #: cache's drift store (`cache.open_store(learn=True)`), opened at `start()`:
        #: the daemon is the learner in practice, and cache writes a lowering before the
        #: read that found it returns. None while not started, and when cache is not
        #: present. A test may hand one in (`cache_store=`), which `start()` keeps and
        #: `stop()` leaves open.
        self.cache_store = None if cache_store is cache.MISSING else cache_store
        self._own_store = cache_store is cache.MISSING
        #: What the drift log lines have said already (a lowering, a held record), so
        #: each is said once.
        self._drift_said: set = set()
        self._drift_lock = threading.Lock()
        self.monotonic = monotonic
        self._start_trim_thread = start_trim_thread
        # custody, journalled: the daemon is the only writer, and the only witness
        self.journal = bindings.Journal(journal_path, clock=clock)

        self._lock = threading.RLock()
        self.bindings: dict[str, Binding] = {}
        #: The newest #names id the ROUTER has taken responsibility for. It is what makes
        #: the catch-up boundary exact now that the boundary's gateway read happens
        #: OUTSIDE the lock: see `_register`.
        self._seen_max = "0"
        self.by_slot: dict[int, str] = {}
        #: slot -> the Keep held on it for a name that has not come back yet. Read and
        #: changed under `_lock`, like `by_slot`. A keep
        #: refuses nobody: it only steers which slot `next-free` picks.
        self.keeps: dict[int, Keep] = {}
        #: What `_load_keeps` read at boot, before the socket listens, waiting for the
        #: listen that starts their clock: a list of
        #: (kind, slot, qualified, expires-or-None).
        self._loaded_keeps: list = []
        #: May this daemon write the mark? Only once it is LISTENING. A daemon whose start
        #: was refused, or never called, must not write over a live daemon's mark from its
        #: `stop()`.
        self._keeps_armed = False
        #: A write that failed: the file is older than memory, so the next pick tries
        #: again, and every successful write rewrites the whole file from memory.
        self._keeps_stale = False
        #: Set under `_lock` by `stop()` before anything closes. From then on
        #: deregisters leave the mark alone and a register is answered with a close.
        self._stopping = False
        self._auto_seq = 0
        self.auto_windows: dict[str, list] = {}        # agent-N -> [start, end|None]
        #: The queue: qualified -> {message id: the message's epoch, or None until it is
        #: read}. Every message waiting for a name, offline, cold or flagged alike; the
        #: record's `queue` table is the durable copy, loaded at boot before the socket
        #: listens, and memory is the authority for the running daemon.
        self.queue: dict[str, dict[str, float | None]] = {}
        #: (qualified, message id) -> True (add) or False (remove): queue changes the
        #: record has not taken yet. Written at the end of the change's pass and retried
        #: at every later one; the last change to a row wins.
        self._queue_unwritten: dict[tuple[str, str], bool] = {}
        #: One writer of the queue's record rows at a time, so an older change never
        #: lands after a newer one.
        self._queue_write_lock = threading.Lock()
        #: message ids a record line addressed to a send-only identity, logged once
        #: each so a rescan does not say it again. Bounded (`_note_send_only`).
        self.send_only_seen: dict[str, None] = {}
        #: (qualified, message id) of rows whose message the record does not have (or
        #: whose line zero does not parse), logged once each: such a row is skipped at
        #: every release and kept, since only a `handled` report deletes a row.
        self._row_unreadable_logged: set[tuple[str, str]] = set()
        #: One session id, one live binding: session id -> the qualified name whose live
        #: binding holds it.
        self.session_owner: dict[str, str] = {}
        #: Session ids this daemon has seen a prompt or a turn end in. `turned` is never
        #: lowered for one of them for the life of the daemon, so a shim respawned in a
        #: session that has had turns cannot read `fresh`.
        self.turned_ids: set = set()
        #: One release at a time PER NAME, so a name's queued mail goes out in one piece.
        #: Per name, not global: a delivery blocks in `sendall`, and a shim that stops
        #: reading must stall only its own name.
        self._release_locks: dict[str, threading.RLock] = {}
        #: RULE H1's open settle windows by qualified name: one binding holds a name, so
        #: one window per name. Changed under `_window_cond`, which the window worker
        #: waits on; `_lock` may be held when it is taken, never the other way round. A
        #: window lives in memory only: a restart loses it, and `[held]` rows then wait
        #: for the next turn end.
        self._windows: dict[str, SettleWindow] = {}
        self._window_cond = threading.Condition()
        #: Has the queue been loaded from the record in this daemon's life? The retention
        #: trim keeps every queued message, so it does not run until it has.
        self._queue_loaded = False
        #: message id -> its EFFECTIVE audience, None where it has none. A derived index:
        #: fed at parse by live routing, the rescan and register replay, rebuilt by the
        #: boot rescan, bounded to the rescan window, and never fetched into on demand --
        #: the fill-in reads this and nothing else Three values: a tag; None, known to
        #: carry no audience; or AUDIENCE_INDEX_UNKNOWN. An absent id answers the same as
        #: the sentinel.
        self.audiences: dict[str, str | None | _UnknownAudience] = {}
        #: ask id -> {"asker": qualified, "at": the ask's epoch, "open": set of the
        #: qualified names it still binds}. A
        #: derived index, never authoritative: it only nominates asks for the send verb,
        #: which decides on a fresh read of the record. It records which messages a name
        #: has authored in the record, never what anyone was delivered or read. In memory,
        #: under `_lock`, fed in `_route` alone, and it only moves forward: an entry is
        #: created once and a name that leaves `open` never returns.
        self.asks: dict[str, dict] = {}
        #: The sent index: author qualified
        #: name -> its `#names` messages of the last `OWED_TTL`, ascending by id, and the
        #: same entries by id. Rebuilt at boot from the record (`rebuild_sent`), fed in
        #: `_route` alone after the claims, forward-only, trimmed by age. `sent_floor` is
        #: the epoch the index is complete from: a debt older than it is undecidable.
        self.sent: dict[str, list[SentEntry]] = {}
        self.sent_by_id: dict[str, SentEntry] = {}
        self.sent_floor: float | None = None
        self._sent_feeds = 0
        #: (owing qualified name, message id) -> {"owing", "author", "message_id", "at"}:
        #: debts a shim dropped with their reminder ignored.
        #: It names no standing: it says which message went unanswered, by whom.
        self.unanswered: dict[tuple, dict] = {}
        self.injected_through: dict[str, str] = {}
        #: The LAST DIGEST TEXT each live identity was shown.
        #: Carried on the `injected` ping and readable back out with
        #: `query kind="injected"`, so the explainer can show the input and not only
        #: the verdict. Keyed and cleared exactly like `injected_through`: it lives
        #: as long as the binding does and no longer. Nothing routes off it.
        self.injected_digest: dict[str, str] = {}
        self.activity: dict[str, float] = {}
        #: Last ping `seq` accepted per SOURCE, where a source is one (connection,
        #: identity) pair -- see `_ping_ordered`. Dropped with the connection.
        self._ping_seq: dict[object, dict[str, int]] = {}
        self.rescans = 0
        self._last_trim = 0.0
        #: (qualified, message id) -> when `_deliver` handed that message to that name's
        #: binding, by this daemon's clock. Memory only, under `_lock`; the trim drops
        #: entries older than `THREAD_IDLE`. It answers `query kind="readable"`.
        self.handed_at: dict[tuple[str, str], float] = {}
        #: When this daemon started: the earliest a message it never handed over can
        #: have become readable since (`query kind="readable"`).
        self.started_at = self.clock()

        self._server: socket.socket | None = None
        #: Did THIS daemon actually bind the path? Only a daemon that did may unlink it.
        #: An old daemon's `stop()` used to delete whatever file was at the path, which
        #: could be a NEWER daemon's live socket -- and a listening fd cannot be
        #: re-linked to a name, so the only recovery was a restart.
        self.bound = False
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._sub = None

    # ------------------------------------------------------------------ directory
    @contextlib.contextmanager
    def _db(self):
        """Per-claim directory read -- never a boot-time cache."""
        conn = directory.connect(self.db_path, read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def _names_channel(self) -> str:
        with self._db() as conn:
            row = directory.room_by_name(conn, NAMES)
            if row is None:
                raise RuntimeError(f"{NAMES} is not provisioned")
            return row["channel_id"]

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "Daemon":
        # `[cache] required`: a box that says its cold-send hold must hold refuses to
        # start without the cache module, before anything is probed or written.
        refusal = cache.required_refusal(self.settings.use("cache").required)
        if refusal:
            raise RuntimeError(refusal)
        # The PROBE comes first, before anything is written or fenced or unlinked: it is
        # the one question the rest of boot assumes the answer to. Ask it by knocking.
        self._probe_or_refuse()
        self.started_at = self.clock()
        # First-start provisioning, when this daemon has it, runs only now: the probe
        # has shown that no other daemon holds the socket, so two daemons starting on
        # one empty home cannot both provision it. It may refuse (RuntimeError).
        if self.first_start is not None:
            self.first_start(self.gateway, self.db_path)
        # The fence comes after the probe and still before the bind. Its premise -- that
        # nothing else holds a seat -- is exactly what the probe just checked, and the
        # premise is load-bearing: `fence()` is an unscoped `UPDATE ... WHERE released_at
        # IS NULL`, so run by an intruder it closes a LIVE daemon's open intervals, turns
        # that daemon's own later release into a silent no-op, and stamps the row
        # `fenced=1`. `holder_at` then returns None for everything that seat posts
        # afterwards -- the blanket-unverified attribution the journal exists to prevent,
        # permanently. Before the bind, because while nothing is bound nothing can open a
        # new interval, and an interval still open belongs to a daemon that died.
        if self.journal.open():
            fenced = self.journal.fence()
            if fenced:
                log.warning("boot fence: closed %d custody interval(s) left open by a "
                            "daemon that did not shut down cleanly", fenced)
        # boot: the keep mark, read after the probe and beside the fence, and only READ
        # here: a start the probe refused has returned above and has written nothing.
        # The clock of each keep starts at the listen below.
        self._load_keeps()
        # boot: the queue, BEFORE the socket listens, so no register or routing pass can
        # hand a queued id as live mail. The first boot of this daemon moves the mail an
        # older daemon held into it first (`_migrate_queue`). A read that RAISES stops
        # the start here: routing without the queue would hand queued mail as if it
        # were new.
        self._load_queue()
        # boot: cache's drift store, opened to learn; every lowering is written by it.
        if self._own_store:
            self.cache_store = cache.open_store(learn=True)
        # boot: the sent index, also before the socket listens, so no `owed` query is
        # ever answered from a half-read record.
        self._rebuild_sent_logged()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)          # RUNTIME dir posture
        self.socket_path.unlink(missing_ok=True)          # the probe said: stale
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        # Before the chmod, not after the listen: from the instant `bind()` returns, the
        # file at that path is this daemon's, and a failure in chmod or listen must still
        # leave a daemon that knows to take it away again.
        self.bound = True
        os.chmod(self.socket_path, 0o600)                  # 0600 in a 0700 dir
        self._server.listen(64)
        self._server.settimeout(0.2)
        # The keep clock starts HERE, when this daemon is listening, and before the
        # accept loop can hand any register a slot. A boot that died before this point
        # wrote nothing, so the next boot still finds the old daemon's `live` entries
        # and starts their window afresh.
        self._arm_keeps()
        self._spawn(self._window_loop)
        self._spawn(self._accept_loop)

        self._sub = self.gateway.subscribe()
        self._spawn(self._event_loop)
        self.trim_record()                                 # boot: retention
        try:
            self.rescan()                                  # boot: queue what waits
        except Exception:
            # The rescan queues mail posted to unbound names while no daemon ran. A
            # daemon that cannot do it is still a daemon that can deliver, and refusing
            # to boot would take live routing down with it -- so this is
            # log-and-continue, exactly as the event loop's rescan already is. Replay
            # past each name's cursor still finds that mail at its next bind.
            log.exception("boot rescan failed; replay at each name's next bind finds "
                          "what it would have queued")
        if self._start_trim_thread:
            self._spawn(self._trim_loop)
        return self

    def _probe_or_refuse(self) -> None:
        """Knock before taking the path away from whatever is behind it.

        A file at `socket_path` used to be treated as debris and unlinked on sight. It
        may be a LIVE daemon's socket, and unlinking it leaves that daemon LISTENing on
        an inode with no name: `moxi comms send` gets ENOENT while `ss -xlp` shows the daemon
        alive, and nothing short of a restart can re-link a listening fd to a name.

        Only two answers let boot continue, and neither of them is silence.
        """
        if not self.socket_path.exists():
            return
        verdict, error = protocol.probe(self.socket_path)
        if verdict == protocol.PROBE_LIVE:
            raise RuntimeError(
                f"a live comms daemon is listening on {self.socket_path}; refusing to "
                f"take the path from it. Stop the running daemon first -- "
                f"`ss -xlp | grep comms` names its pid.")
        if verdict != protocol.PROBE_STALE:
            raise RuntimeError(
                f"{self.socket_path} exists and could not be probed "
                f"({type(error).__name__}, errno {error.errno}); refusing to unlink a "
                f"path this daemon cannot show is dead. Find out what holds it -- "
                f"`ss -xlp` lists unix listeners -- and clear the path by hand.")

    def _spawn(self, target) -> None:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        # The freeze, before `_stop` and before anything closes: the mark is written one
        # last time with every binding still live in it, and from here on the stop's own
        # connection closes cannot empty it. Once only: a second `stop()` would write
        # the bindings the first one's closes removed.
        with self._lock:
            if not self._stopping:
                self._stopping = True
                self._write_keeps(freeze=True)
        self._stop.set()
        with self._window_cond:
            self._window_cond.notify_all()
        if self._server:
            with contextlib.suppress(OSError):
                self._server.close()
        if self._sub is not None:
            self.gateway.unsubscribe(self._sub)
        with self._lock:
            for b in list(self.bindings.values()):
                b.conn.close()
        for t in self._threads:
            t.join(timeout=1.0)
        # after the serve threads have had their chance to journal their releases; a
        # write that lands after this is a no-op, and the next boot's fence closes it
        self.journal.close()
        if self._own_store and self.cache_store is not None:
            cache.close_store(self.cache_store)      # the learning lock goes with it
            self.cache_store = None
        # You only remove the socket you put there. A daemon whose `start()` refused, or
        # was never called, removes nothing -- the file at that path is someone else's.
        if self.bound:
            with contextlib.suppress(OSError):
                self.socket_path.unlink(missing_ok=True)
            self.bound = False

    # ------------------------------------------------------------------ connections
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(lambda s=sock: self._serve(LineConn(s)))

    def _serve(self, conn: LineConn) -> None:
        try:
            while not self._stop.is_set():
                frame = conn.recv()
                if frame is None:
                    break
                self._handle(conn, frame)
        except Exception:
            log.exception("connection handler failed")
        finally:
            self._deregister(conn)
            conn.close()

    def _handle(self, conn: LineConn, frame: dict) -> None:
        op = frame.get("op")
        rid = frame.get("id")
        try:
            if op == "register":
                result = self._register(conn, frame)
                conn.send({"id": rid, "ok": True, **result})
                bound = self.bindings[result["identity"]["qualified"]]
                # Register runs replay first, then its release: the unflagged rows, on a
                # passing read. Flagged rows wait for a real boundary.
                self._replay(bound)
                self._on_register(bound)
            elif op == "query":
                conn.send({"id": rid, "ok": True, **self._query(frame)})
            elif op == "injected":
                if self._ping_ordered(conn, frame):
                    with self._lock:
                        self.injected_through[frame["identity"]] = str(frame["through"])
                        digest = frame.get("digest")
                        if digest is not None:
                            self.injected_digest[frame["identity"]] = str(digest)
            elif op == "activity":
                if self._ping_ordered(conn, frame):
                    kind = frame.get("kind")
                    turn = kind == ACTIVITY_TURN
                    with self._lock:
                        self.activity[frame["identity"]] = self.clock()
                        # The session id rides the notify, and is taken only
                        # from the connection that holds this binding: the frame's
                        # identity is a claim, the connection is not. So are
                        # `projects_root` and `turned`, and a `kind: turn` sets
                        # `turned` whatever the frame says. So are `source`,
                        # `handled` and `unhandled`: a forged frame releases and
                        # settles nothing.
                        b = self.bindings.get(frame["identity"])
                        own = b is not None and b.conn is conn
                        if own:
                            self._take_session(b, frame, at=self.clock(), turn=turn)
                    if own and kind == ACTIVITY_READY:
                        self._on_ready(b, frame.get("source"))
                    elif own and kind == ACTIVITY_LOOK:
                        self._on_look(b)
                    elif own and turn:
                        self._on_turn(b, frame)
            elif op == "unanswered":
                # Reply enforcement: a shim dropped a debt whose reminder was ignored.
                # A notify, no reply; accepted only from the connection that holds the
                # owing binding, since the frame's identity is a claim.
                self._note_unanswered(conn, frame)
            elif op == "ping":
                # The verbs' version check (`protocol.CLI_PROTOCOL`).
                conn.send({"id": rid, "ok": True, "cli_protocol": CLI_PROTOCOL})
            elif op == "nudge":
                # Fanout layer 2.
                # A CLI process writes the local record itself, so nothing in here knows
                # the row landed; this frame says so and the pump fans out at once
                # instead of at the next poll. NO REPLY: it is fire-and-forget
                # (`DaemonClient.notify`), nobody waits for one, and a frame back would
                # arrive at a socket already closed.
                # Only the local gateway has a `nudge`; the mock fans out in-process, so
                # on the mock this op does nothing.
                if hasattr(self.gateway, "nudge"):
                    self.gateway.nudge()
            else:
                conn.send({"id": rid, "ok": False, "error": f"unknown op {op!r}"})
        except DaemonStopping:
            # The freeze turned this register away: close, and say nothing
            # on the wire. The client reads the close as `DaemonOffline` and retries.
            log.info("register turned away: the daemon is stopping")
            conn.close()
        except Refusal as e:
            # The daemon's own NO: a name or slot somebody else holds, an address that
            # cannot resolve. `refused` is the word a shim may act on for good.
            if op == "register":
                # One journal line per refused attempt, naming what was asked. A waiting
                # shim at the backoff ceiling adds about two a minute; that is the price of
                # the journal showing the attempt at all.
                log.warning("register refused: %s: %s", _asked(frame), e)
            conn.send({"id": rid, "ok": False, "error": str(e), "refused": True,
                       **e.extra})
        except Exception as e:  # never take the daemon down for one bad frame
            # An ACCIDENT, not a refusal: a gateway read that 429'd, a broken socket.
            # Marked `transient` so a shim that has held its name keeps retrying instead
            # of reading it as a rival claim (a restart once had three boundary reads
            # 429 in a burst, and three sessions gave up their names for good).
            log.exception("op %s failed", op)
            conn.send({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}",
                       "transient": True})

    # ------------------------------------------------------------------ register
    def _register(self, conn: LineConn, frame: dict) -> dict:
        adapter = frame.get("adapter") or "claude-shim"
        if adapter not in self.kinds:
            raise Refusal(f"unknown adapter kind {adapter!r}; expected one of "
                          f"{', '.join(self.kinds.names())}")
        # The catch-up boundary is READ BEFORE THE LOCK. It is a gateway round trip -- a
        # file read, local or mock -- and taking it under the daemon's ONE lock made every register
        # wait out every other register's network call, and every router, resolver and
        # query wait with them.
        #
        # The read is deferred-raise rather than plain, so the REFUSAL ORDER is unchanged:
        # a name or slot that is already bound is refused with the daemon's own words even
        # when the gateway is also broken, exactly as when the call sat below those checks.
        hwm, hwm_error = self._high_water_mark()
        with self._lock, self._db() as db:
            if self._stopping:
                # Checked under the lock the freeze took, so a register is either
                # installed before the freeze (and is in the mark) or turned away here.
                raise DaemonStopping()
            name = frame.get("name")
            if not name or name == "auto":
                self._auto_seq += 1                     # monotonic; NEVER reused
                name, scope, tier = f"agent-{self._auto_seq}", GLOBAL, TIER_EPHEMERAL
            else:
                if not NAME_RE.match(name):
                    raise Refusal(f"{name!r} is not a valid name")
                if is_auto_name(name):
                    raise Refusal(
                        f"{name!r} is refused: the agent-N namespace is reserved for the "
                        "auto-namer in every scope")
                # A register that names no scope binds global. The frame's `project` is
                # the session's project, never its scope.
                scope = GLOBAL if frame.get("global") else (frame.get("scope") or GLOBAL)
                if directory.retired(db, name, scope):
                    raise Refusal(directory.RETIRED_BIND.format(q=qualify(name, scope),
                                                                name=name))
                tier = TIER_STANDING if directory.standing_row(db, name, scope) else TIER_CHOSEN
            q = qualify(name, scope)
            # A send-only identity binds only as what it is, and only it binds that way:
            # an agent session that bound its name would be handed mail in its place,
            # and a send-only binding of any other name would take mail nobody reads.
            send_only_row = tier == TIER_STANDING and directory.is_send_only(db, name, scope)
            if adapter == SEND_ONLY_ADAPTER and not send_only_row:
                raise Refusal(
                    f"{q} is not a send-only identity: the {SEND_ONLY_ADAPTER} adapter kind "
                    "binds only a name whose directory row is send-only")
            if send_only_row and adapter != SEND_ONLY_ADAPTER:
                raise Refusal(
                    f"{q} is a send-only identity and receives no mail: it binds only "
                    f"with the {SEND_ONLY_ADAPTER} adapter kind, never as {adapter}")
            # The name is resolved FIRST now, because `next-free` gives a name its kept
            # slot. One consequence: a register wrong in both name and
            # slot hears about the name first. The pick changes no keep.
            requested = frame.get("slot", "next-free")
            if adapter == SEND_ONLY_ADAPTER:
                # A send-only binding holds no slot, at its register, while it is bound,
                # at its unbind and across a restart. It posts only from its own
                # standing bundle and is delivered nothing, so it takes none of the
                # slots, spends no slot keep (`_keep_on_release`, `_keep_entries` pass
                # over a null slot) and no free-slot count sees it: slots stay for
                # agents to take and leave freely.
                if _is_numbered(requested):
                    raise Refusal(
                        f"{q} is a send-only identity and holds no slot: it registers "
                        f"next-free, never on slot {requested}")
                slot_id = None
            else:
                slot_id = self._pick_slot(db, requested, q)
            if q in self.bindings:
                held = self.bindings[q]
                seat = "no slot" if held.slot_id is None else f"slot {held.slot_id}"
                raise Refusal(
                    f"{q} is already bound ({seat}, adapter {held.adapter})",
                    holder={"qualified": q, "slot": held.slot_id, "adapter": held.adapter})
            if slot_id is not None and slot_id in self.by_slot:
                held = self.bindings[self.by_slot[slot_id]]
                raise Refusal(
                    f"slot {slot_id} is already bound by {held.qualified}",
                    holder={"qualified": held.qualified, "slot": slot_id,
                            "adapter": held.adapter})
            # The catch-up boundary, FIXED here -- at the same instant the binding becomes
            # visible, and under the same lock. Everything at or below it is replay's
            # business (bounded, possibly dropped); everything above it is live. Without
            # this line a lagging event loop would re-deliver what the bound just omitted.
            #
            # The gateway's answer above was read before the lock, so it can be stale by
            # the width of the round trip; `_seen_max` is what closes that gap. A message
            # the router handled while this register was in flight was resolved against a
            # table with no binding in it, so live routing will never deliver it -- and if
            # it sat ABOVE the boundary, replay would skip it too and it would wait in
            # `pending` for a rescan. Raising the boundary to include it hands it to
            # replay, which reads history AFTER this point and therefore sees it.
            #
            # That is the whole safety argument, and it is an ordering one: `_seen_max` is
            # raised before the router resolves, and the boundary is fixed in the same
            # critical section that installs the binding. So for any message the router
            # resolved unbound, either its id was already in `_seen_max` when the boundary
            # was fixed (replay owns it), or the boundary was fixed first -- in which case
            # the binding was installed first too, and the router could not have seen it
            # unbound. Exactly one of the two owns every message, as before the hoist.
            if hwm_error is not None:
                raise hwm_error
            hwm = max(hwm, self._seen_max, key=int)
            binding = Binding(conn=conn, qualified=q, name=name, scope=scope, tier=tier,
                              adapter=adapter, slot_id=slot_id, bound_at=self.clock(),
                              replay_hwm=hwm)
            # The session behind the name, from the register itself: this frame is the
            # binding's own connection by construction. With it, the shim's evidence for
            # `fresh`; an id another live binding holds is refused.
            self._take_session(binding, frame, at=binding.bound_at, register=True)
            # `holding`: ids the adapter already has queued or injected. Claimed for
            # this binding at once, so neither replay nor a release hands them again and
            # a reconnect whose replay finds only these sets nothing off. An id with a
            # row keeps it: the row goes only when this binding names it `handled`.
            for mid in frame.get("holding") or ():
                mid = str(mid)
                if not mid.isdigit():
                    continue
                binding.delivered.add(mid)
            if adapter == SEND_ONLY_ADAPTER:
                # Anything queued for the name from before its row was send-only is for
                # nobody now: a send-only binding is never handed mail.
                dropped = sorted(self.queue.get(q, {}), key=_as_id)
                if dropped:
                    self._queue_drop(q, dropped)
                    log.info("bind %s: dropped %d message(s) queued for it from before its "
                             "row was send-only, delivered to nobody: %s", q,
                             len(dropped), ", ".join(dropped))
            self.bindings[q] = binding
            if slot_id is not None:               # a send-only binding is on no slot
                self.by_slot[slot_id] = q
            if tier == TIER_EPHEMERAL:
                self.auto_windows[name] = [binding.bound_at, None]
            # custody, journalled at the moment only the daemon sees it. An `agent-N` is
            # a binding like any other and is journalled like any other; a failed write
            # returns None and the binding proceeds regardless (evidence, not product).
            binding.journal_id = self.journal.bind(
                qualified=q, name=name, scope=scope, tier=tier, slot_id=slot_id,
                adapter=adapter, at=binding.bound_at)
            log.info("bind %s tier=%s slot=%s adapter=%s", q, tier, slot_id, adapter)
            # Keeps end only now, with the binding installed: a register
            # refused anywhere above has left every keep as it was.
            self._end_keeps_on_install(q, slot_id, numbered=_is_numbered(requested))
            self._write_keeps()
        # `hwm`, the catch-up boundary: the shim's registration
        # high-water id. A `re:` below it was sent before that shim could see payments.
        # `protocol`, this daemon's claims-protocol version: a plugin shim ships its own
        # copy of the source and checks it on connect.
        return {"identity": {"name": name, "scope": scope, "tier": tier, "qualified": q},
                "slot": slot_id, "hwm": hwm, "protocol": PROTOCOL_VERSION}

    def _high_water_mark(self):
        """(hwm, error): the record's high id for a register's catch-up boundary.

        One read per register, concurrent with every other register's: no register
        waits out another's round trip. A failed read fails the register `transient`, and
        the shim's backoff retries it a moment later. See `_handle`.
        """
        try:
            return self.gateway.max_message_id(self._names_channel()) or "0", None
        except Exception as e:            # re-raised by the caller, in the old call's place
            return "0", e

    def _pick_slot(self, db, requested, q=None):
        """The slot a register gets.

        A number is taken as asked when it is provisioned; a keep never refuses one.
        `next-free`, in order: the slot kept for `q`, if it is provisioned and not
        bound; the lowest slot neither bound nor kept; the lowest slot not bound though
        it is kept (the caller logs the give-up once the binding is installed); else
        "no free slot". Keeps whose time is up are dropped first. Nothing else here
        changes a keep: a register refused after the pick leaves them all in place.
        """
        available = directory.slot_ids(db)
        self._sweep_keeps()
        if not _is_numbered(requested):
            free = [s for s in available if s not in self.by_slot]
            for s in free:
                keep = self.keeps.get(s)
                if keep is not None and q is not None and keep.qualified == q:
                    return s                                      # 1. its own keep
            for s in free:
                if s not in self.keeps:
                    return s                                      # 2. unbound, unkept
            if free:
                return free[0]                                    # 3. a give-up
            raise Refusal("no free slot")
        slot_id = int(requested)
        if slot_id not in available:
            raise Refusal(f"slot {slot_id} is not provisioned")
        return slot_id

    # ------------------------------------------------------------------ slot keeps
    def _load_keeps(self) -> None:
        """Read the keep mark, before the socket listens.

        Only READS. What it finds waits in `_loaded_keeps` for `_arm_keeps`, which runs
        at the listen. Everything that can go wrong here only removes keeps, which
        relaxes slot choice to what it was before keeps existed: a missing file, one
        that will not parse or has the wrong shape, an entry that is malformed, names an
        `agent-N`, names a slot no longer provisioned, or carries an expiry that is not a
        finite number, and every entry of a pair that conflicts (two for one slot, or two
        for one name). Each drop is logged once.
        """
        self._loaded_keeps = []
        try:
            raw = self.keeps_path.read_text()
        except FileNotFoundError:
            log.info("slot keeps: no mark at %s; no slot is kept this boot",
                     self.keeps_path)
            return
        except OSError as e:
            log.warning("slot keeps: the mark at %s could not be read (%s); no slot is "
                        "kept this boot", self.keeps_path, e)
            return
        try:
            entries = json.loads(raw)
        except ValueError as e:
            log.warning("slot keeps: the mark at %s does not parse (%s); no slot is kept "
                        "this boot", self.keeps_path, e)
            return
        if not isinstance(entries, list):
            log.warning("slot keeps: the mark at %s is not a list of entries; no slot is "
                        "kept this boot", self.keeps_path)
            return
        try:
            with self._db() as db:
                provisioned = set(directory.slot_ids(db))
        except Exception as e:                        # noqa: BLE001 -- only removes keeps
            log.warning("slot keeps: the directory could not be read (%s); no slot is "
                        "kept this boot", e)
            return
        good = []
        for entry in entries:
            parsed = _parse_keep_entry(entry)
            if isinstance(parsed, str):
                log.warning("slot keeps: dropped an entry (%s): %r", parsed, entry)
                continue
            kind, slot, q, expires = parsed
            if slot not in provisioned:
                log.warning("slot keeps: dropped %s on slot %s: the slot is no longer "
                            "provisioned", q, slot)
                continue
            good.append(parsed)
        slots = [p[1] for p in good]
        names = [p[2] for p in good]
        for parsed in good:
            kind, slot, q, expires = parsed
            if slots.count(slot) > 1 or names.count(q) > 1:
                log.warning("slot keeps: dropped %s on slot %s: it conflicts with another "
                            "entry for the same %s", q, slot,
                            "slot" if slots.count(slot) > 1 else "name")
                continue
            self._loaded_keeps.append(parsed)

    def _arm_keeps(self) -> None:
        """Start the keep clock, at the listen, and write the mark.

        A `live` entry becomes a keep until now + SLOT_KEEP. A carried `keep` entry
        keeps its own expiry if that is still ahead, clamped to now + SLOT_KEEP so no
        value in the file (a clock that was wrong at boot, a hand edit) can hold a slot
        longer than one window. The mark is then rewritten, so the file reflects the
        loaded keeps and no live bindings.
        """
        with self._lock:
            boot, mono = self.clock(), self.monotonic()
            window = constants.SLOT_KEEP
            self.keeps = {}
            said = []
            for kind, slot, q, expires in self._loaded_keeps:
                carried = kind == "keep"
                until = boot + window if not carried else min(expires, boot + window)
                if until <= boot:
                    log.info("slot keeps: %s on slot %s had already lapsed at boot", q, slot)
                    continue
                self.keeps[slot] = Keep(qualified=q, expires=until,
                                        deadline=mono + (until - boot), carried=carried)
                said.append(f"{slot} {q} ({int(round(until - boot))}s"
                            f"{', carried' if carried else ''})")
            self._loaded_keeps = []
            self._keeps_armed = True
            if said:
                log.info("boot: keeping %d slot(s): %s", len(said), ", ".join(said))
            self._write_keeps()

    def _keep_lapsed(self, keep: Keep, now: float, mono: float) -> bool:
        return now >= keep.expires or mono >= keep.deadline

    def _sweep_keeps(self) -> None:
        """Drop the keeps whose time is up, in one journal line, and rewrite the mark."""
        now, mono = self.clock(), self.monotonic()
        lapsed = sorted(s for s, k in self.keeps.items() if self._keep_lapsed(k, now, mono))
        if lapsed:
            gone = [f"{s} {self.keeps.pop(s).qualified}" for s in lapsed]
            log.info("slot keep(s) lapsed unclaimed: %s", ", ".join(gone))
            self._write_keeps()
        elif self._keeps_stale:
            self._write_keeps()

    def _end_keeps_on_install(self, q: str, slot_id: int | None, *,
                              numbered: bool) -> None:
        """A binding was installed: end its name's keep (claim) and any keep on its slot
        (a numbered register's takeover, or `next-free`'s give-up at step 3). A binding
        with no slot (send-only) ends only its own name's keeps."""
        for s, keep in list(self.keeps.items()):
            if keep.qualified == q:
                del self.keeps[s]
                log.info("slot keep for %s on slot %s ended: claimed", q, s)
        keep = self.keeps.pop(slot_id, None)
        if keep is not None:
            how = "taken by" if numbered else "given up to"
            tail = "" if numbered else " (no other slot free)"
            log.warning("slot keep for %s on slot %s ended: %s %s%s",
                        keep.qualified, slot_id, how, q, tail)

    def _keep_on_release(self, b: Binding) -> None:
        """A released slot waits for its name: when a chosen or standing binding ends,
        its slot is kept for that name for `SLOT_KEEP`, the same keep the daemon writes
        for every live binding at boot, so a launch registering `next-free` in the gap
        takes another slot while one is free. Not once the daemon is stopping: the
        freeze writes its own mark. Called under `_lock`; the caller writes the mark."""
        if (self._stopping or not self._keeps_armed or b.slot_id is None
                or b.tier not in (TIER_CHOSEN, TIER_STANDING)):
            return
        if b.slot_id in self.by_slot:
            return                            # the slot was taken already
        for s, keep in list(self.keeps.items()):
            if keep.qualified == b.qualified:
                del self.keeps[s]             # a name keeps one slot, its last
        now, mono = self.clock(), self.monotonic()
        window = constants.SLOT_KEEP
        self.keeps[b.slot_id] = Keep(qualified=b.qualified, expires=now + window,
                                     deadline=mono + window)
        log.info("slot %s kept for %s for %ss after its unbind", b.slot_id, b.qualified,
                 int(window))

    def _keep_entries(self) -> list:
        """The mark's contents, from memory: live chosen and standing bindings, and
        unclaimed keeps. `agent-N` never enters it: an auto name is never reused, so a
        keep for one could never be claimed."""
        entries = [{"kind": "live", "slot": b.slot_id, "qualified": b.qualified}
                   for b in sorted(self.bindings.values(), key=lambda x: x.qualified)
                   if b.tier in (TIER_CHOSEN, TIER_STANDING) and b.slot_id is not None]
        entries += [{"kind": "keep", "slot": s, "qualified": k.qualified,
                     "expires": k.expires} for s, k in sorted(self.keeps.items())]
        return entries

    def _write_keeps(self, *, freeze: bool = False) -> None:
        """Rewrite the whole mark from memory: a temporary file, then a rename. Called
        under `_lock`. After the freeze only the freeze itself writes; before the listen
        nothing does. A failed write is logged, the daemon goes on (the journal's own
        posture), and the file counts as stale: the next pick tries again. Every failure
        direction relaxes: a lost install costs that name its keep at the next boot, and
        a stale `live` entry is a false keep for at most one window."""
        if not self._keeps_armed or (self._stopping and not freeze):
            return
        text = json.dumps(self._keep_entries(), indent=1) + "\n"
        tmp = None
        try:
            self.keeps_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".slot-keeps.", suffix=".tmp",
                                       dir=self.keeps_path.parent)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, self.keeps_path)
            tmp = None
            self._keeps_stale = False
        except OSError as e:
            self._keeps_stale = True
            log.warning("slot keeps: the mark at %s could not be written (%s); it is "
                        "stale until the next write succeeds", self.keeps_path, e)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def _keeps_view(self) -> dict:
        """`query kind="agents"`'s keeps: slot -> who it is kept for and the
        seconds left. Read-only; a keep past its time is left out, not dropped."""
        now, mono = self.clock(), self.monotonic()
        return {s: {"qualified": k.qualified,
                    "expires_in": max(0.0, min(k.expires - now, k.deadline - mono))}
                for s, k in sorted(self.keeps.items())
                if not self._keep_lapsed(k, now, mono)}

    def _ping_ordered(self, conn: LineConn, frame: dict) -> bool:
        """Is this `activity` / `injected` ping newer than the last from its source?

        The SOURCE is `(connection, identity)` -- one adapter process's attachment to
        this daemon, reporting on one name. That is the grain the sender numbers at
        (`DaemonClient` holds one counter, and every adapter builds a fresh client on
        every reconnect), and it is the grain that survives the right things: a
        reconnect resets both sides together, and a forged ping on some OTHER
        connection cannot run this identity's counter forward and starve its real
        adapter. What it deliberately does NOT do is authenticate -- the frame's
        identity is still not checked against the connection's binding. This is the
        replay half only.

        A frame with no `seq`, or an unreadable one, is accepted unordered and leaves
        the counter alone. These pings are hints, and dropping a real one costs the same as
        losing it.
        """
        raw = frame.get("seq")
        if raw is None:
            return True
        try:
            seq = int(raw)
        except (TypeError, ValueError):
            return True
        identity = frame.get("identity")
        with self._lock:
            seen = self._ping_seq.setdefault(conn, {})
            last = seen.get(identity)
            if last is not None and seq <= last:
                log.debug("dropped a stale %s ping for %s: seq %s is not past %s",
                          frame.get("op"), identity, seq, last)
                return False
            seen[identity] = seq
        return True

    def _deregister(self, conn: LineConn) -> None:
        with self._lock:
            # The ping counters die with the connection that fed them: the next
            # process to hold this name comes in on a new connection and starts its
            # count at 1 again, so a kept number would drop its whole first run.
            self._ping_seq.pop(conn, None)
            gone = [b for b in self.bindings.values() if b.conn is conn]
            for b in gone:
                # queued BEFORE the binding disappears from `bindings`: a reader that
                # sees the unbind must already see the mail it left behind
                self._queue_unbound(b)
                if b.session_id and self.session_owner.get(b.session_id) == b.qualified:
                    del self.session_owner[b.session_id]
                self.bindings.pop(b.qualified, None)
                if b.slot_id in self.by_slot and self.by_slot[b.slot_id] == b.qualified:
                    del self.by_slot[b.slot_id]
                if b.tier == TIER_EPHEMERAL and b.name in self.auto_windows:
                    self.auto_windows[b.name][1] = self.clock()
                self.injected_through.pop(b.qualified, None)
                self.injected_digest.pop(b.qualified, None)
                # Its settle window closes with it (RULE H1).
                self._close_window(b)
                # The unanswered record's third drop: the owing binding ended.
                for key in [k for k in self.unanswered if k[0] == b.qualified]:
                    del self.unanswered[key]
                # the close of the custody interval: the connection IS the liveness
                # signal, so the connection's end is the release, here and nowhere else
                self.journal.release(b.journal_id)
                log.info("unbind %s", b.qualified)
                self._keep_on_release(b)
            if gone:
                # The mark follows memory -- except after the freeze, when the
                # stop's own closes must not empty it (`_write_keeps` says no).
                self._write_keeps()
        if gone:
            self._queue_flush()

    # ------------------------------------------------------------------ query
    def _query(self, frame: dict) -> dict:
        kind = frame.get("kind")
        if kind == "resolve":
            return self._resolve_query(frame)
        if kind == "agents":
            with self._lock:
                bindings = [{"qualified": b.qualified, "name": b.name, "scope": b.scope,
                             "tier": b.tier, "adapter": b.adapter, "slot": b.slot_id,
                             "endpoint": b.endpoint, "endpoint_since": b.endpoint_since}
                            for b in sorted(self.bindings.values(), key=lambda x: x.qualified)]
                now = self.clock()
                waiting = self._waiting_view(now)
                unanswered = self._unanswered_rows(now)
                # Slot keeps: operator-facing, and printed only by
                # `moxi comms agents --seats`.
                keeps = self._keeps_view()
            # `waiting`: what waits in the queue for each name, one count, since one
            # queue holds offline, cold and flagged mail alike and no reason is stored.
            # `pending` stays in the answer, always empty and read by nothing here: an
            # adapter started before the queue checks for it before it renders a roster.
            return {"bindings": bindings, "waiting": waiting, "pending": {},
                    "unanswered": unanswered, "keeps": keeps}
        if kind == "injected":
            # The LAST DIGEST a live identity was shown. The read surface
            # `moxi comms explain` reads to show the INPUT and not only the
            # verdict. `identity` is a qualified name, exactly as `kind="agents"`
            # prints it -- nothing is resolved here, because an explainer asking about
            # a name is asking about the binding it just listed.
            #
            #     -> {"op": "query", "kind": "injected", "identity": "planner@proj-a"}
            #     <- {"ok": true, "identity": "planner@proj-a", "bound": true,
            #         "through": "1552...", "digest": "<the text>"}
            #
            # `digest` is None when nothing has been injected into this binding yet,
            # and the whole row is empty once the binding ends: this is runtime state
            # for a LIVE identity, not a history. It is never a refusal to ask about a
            # name that is not bound -- `bound: false` is the answer.
            identity = str(frame.get("identity") or "")
            with self._lock:
                return {"identity": identity,
                        "bound": identity in self.bindings,
                        "through": self.injected_through.get(identity),
                        "digest": self.injected_digest.get(identity)}
        if kind == "session":
            return self._session_query(frame)
        if kind == "owner":
            return self._owner_query(frame)
        if kind == "asks":
            return self._asks_query(frame)
        if kind == "owed":
            return self._owed_query(frame)
        if kind == "readable":
            return self._readable_query(frame)
        if kind == "explain":
            return self._explain_query(frame)
        if kind == "binding_window":
            with self._lock:
                window = self.auto_windows.get(frame.get("name"))
            return {"window": window}
        # --- record reads, for callers outside comms --------------------------------
        # Comms' own CLI opens the record directly. Other modules (`record_history`,
        # `attributed_message`) read it through these queries.
        if kind == "record_message":
            msg = self.gateway.get_message(str(frame.get("message_id")))
            return {"message": msg.to_dict() if msg else None}
        if kind == "attributed_message":
            return self._attributed_message_query(frame)
        if kind == "record_history":
            channel_id = frame.get("channel_id") or self._names_channel()
            msgs = self.gateway.history(str(channel_id), after=frame.get("after"),
                                        limit=frame.get("limit"))
            return {"messages": [m.to_dict() for m in msgs]}
        if kind == "record_max_id":
            channel_id = frame.get("channel_id")
            return {"message_id": self.gateway.max_message_id(
                str(channel_id) if channel_id else None)}
        raise Refusal(f"unknown query kind {kind!r}")

    def _session_query(self, frame: dict) -> dict:
        """Which live session sits behind one bound name, and since when.

            -> {"op": "query", "kind": "session", "identity": "planner@proj-a"}
            <- {"ok": true, "identity": "planner@proj-a", "bound": true,
                "adapter": "claude-shim", "session_id": "3d19…", "session_seen_at": …,
                "bound_at": …, "bound_for": 41.2}

        It is NOT resolution: `identity` is a qualified name taken literally, and the
        answer says nothing about where mail goes. `cache_kind` (`warm`, `cold`, `fresh`,
        `no-cache` or `unknown`), `age_seconds` and `cache_reason` are the daemon's own
        read of the bound session's cache, the one its gate delivers by, with
        `cache_ttl` (the lifetime that read used, None when it used none), `cache_key`
        (what the lifetime was looked up by, in words), `cache_code` (the read's code,
        for a `no-cache` or `unknown`) and `cache_source` (the root a
        codex or opencode binding reported); an unbound name answers them as None. `bound_for` is `bound_at` read against
        this daemon's own clock, so a caller on another clock can still tell a binding
        that has not had its first look or turn event yet from one that never will.
        `endpoint` is whether the bound session has a turns endpoint its adapter delivers
        through (None when its adapter reports no such state), and `endpoint_for` how
        long, on this daemon's clock, it has had it or lacked it. An unbound name answers `bound:
        false` with every other field None; never a refusal.
        """
        identity = str(frame.get("identity") or "")
        now = self.clock()
        with self._lock:
            b = self.bindings.get(identity)
            if b is None:
                return {"identity": identity, "bound": False, "adapter": None,
                        "session_id": None, "session_seen_at": None, "bound_at": None,
                        "bound_for": None, "cache_kind": None, "age_seconds": None,
                        "cache_reason": None, "cache_ttl": None, "cache_key": None,
                        "cache_code": None, "cache_source": None, "endpoint": None,
                        "endpoint_for": None}
            answer = {"identity": identity, "bound": True, "adapter": b.adapter,
                      "session_id": b.session_id, "session_seen_at": b.session_seen_at,
                      "bound_at": b.bound_at, "bound_for": now - b.bound_at,
                      "cache_source": b.cache_source, "endpoint": b.endpoint,
                      "endpoint_for": (None if b.endpoint_since is None
                                       else now - b.endpoint_since)}
        # The daemon's own read of the bound session's cache, by the function the gate
        # delivers by: the verb's hold and `moxi comms cache` read these.
        clock = self._read(b)
        answer.update(cache_kind=clock.kind, age_seconds=clock.age_seconds,
                      cache_reason=clock.reason, cache_ttl=clock.ttl_seconds,
                      cache_key=clock.key, cache_code=clock.code)
        return answer

    def _owner_query(self, frame: dict) -> dict:
        """Which live binding owns one session id now, and whom that identity awaits.
        `session`'s question asked the other way round.

            -> {"op": "query", "kind": "owner", "session_id": "3d19…"}
            <- {"ok": true, "session_id": "3d19…", "owner": "lead",
                "adapter": "claude-shim", "awaiting": ["planner@proj-a"]}

        `owner` is the qualified name of the live binding `session_owner` maps the id to,
        exactly as `kind="agents"` prints it (a global identity bare). It is null when no
        live binding holds the id: a session this daemon never heard of and one whose
        binding has ended both answer the same, since the daemon keeps nothing for an
        id once its binding lets go.
        `adapter` is that binding's kind, null with no owner; a `send-only` binding
        answers here only if its own register carried a session id. `awaiting` is the
        owner's list as `owed` computes it (`_awaiting`, the same call under the same
        lock), empty with no owner.

        A READ. It changes no hold, pend, delivery or ask state. Asked by the launch
        console's keep-warm verb through `moxi comms owner --json`. An empty `session_id` is
        refused.
        """
        sid = frame.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise Refusal("owner: `session_id` must be a non-empty session id")
        now = self.clock()
        with self._lock:
            owner = self.session_owner.get(sid)
            b = self.bindings.get(owner) if owner is not None else None
            if b is None or b.session_id != sid:
                return {"session_id": sid, "owner": None, "adapter": None,
                        "awaiting": []}
            awaiting = self._awaiting(owner, list(self.sent.get(owner, ())), now)
            return {"session_id": sid, "owner": owner, "adapter": b.adapter,
                    "awaiting": awaiting}

    def _attributed_message_query(self, frame: dict) -> dict:
        """One message from the record, with its author as the daemon attributes it.

            -> {"op": "query", "kind": "attributed_message",
                "message_id": "4611686018427387905"}
            <- {"ok": true, "message_id": "4611686018427387905", "found": true,
                "room": "#names",
                "author": {"name": "planner@proj-a", "tier": "chosen",
                           "attribution": "verified"},
                "recipients": ["operator"], "resolved": ["operator"],
                "text": "the body, line zero removed",
                "timestamp": "2026-01-01T12:00:00Z"}

        `author` is `attribute(msg, live=False)`: the record and the custody journal,
        the function replay delivers by and `moxi comms history` and `moxi comms reply` print. It
        answers for who held the posting seat at the message's own timestamp, never for
        whoever holds it now, so it can never say `verified` where live delivery said
        `labeled`. `attribution` is the wire value (`verified` or `labeled`), as in a
        delivered payload. `recipients` are the tags line zero names, as a delivered
        payload carries them; `resolved` is each tag as the daemon resolves it now,
        by the one rule (null for a tag that does not resolve). A message with
        no machine line has no recipients, and its `text` is the whole body. `room` is the
        room's name; only `#names` is routed.

        An id the record does not hold answers `found: false` with every other field
        null or empty; never a refusal. An id that is not a message id is refused.

        A READ. It changes no hold, pend, delivery, cursor or ask state. Who may ask:
        any process that can open this socket, as for `record_message`, `session` and
        `owner`. It answers nothing those reads, `moxi comms history` and `moxi comms reply` do
        not already show any process of this user (the body, its author with tier and
        verified or unverified, its recipients), so no caller learns more through it.
        """
        raw = frame.get("message_id")
        mid = str(raw).strip() if isinstance(raw, (str, int)) else ""
        if not mid.isdigit():
            raise Refusal("attributed_message: `message_id` must be a message id")
        msg = self.gateway.get_message(mid)
        if msg is None:
            return {"message_id": mid, "found": False, "room": None, "author": None,
                    "recipients": [], "resolved": [], "text": None, "timestamp": None}
        author = self.attribute(msg, live=False)
        ml = parse_machine_line(msg.content)
        tags = list(ml.recipients) if ml is not None else []
        resolved = []
        for tag in tags:
            r = self.resolve(tag)
            resolved.append(r.qualified if r.resolvable else None)
        return {"message_id": msg.id, "found": True,
                "room": self.gateway.channel_name(msg.channel_id) or msg.channel_id,
                "author": {"name": author.qualified, "tier": author.tier,
                           "attribution": author.attribution},
                "recipients": tags, "resolved": resolved,
                "text": ml.text if ml is not None else msg.content,
                "timestamp": msg.timestamp}

    def _explain_query(self, frame: dict) -> dict:
        """Every live fact behind one identity's classification.

        Five questions decide whether mail landed, and each has one place its ground
        truth lives. Four of those five live in this process's memory and the fifth is a
        file on this box, so the whole table can be answered in one pass under one lock,
        and `moxi comms explain` renders what comes back.

        Three things this deliberately is not:

        * **Not a second opinion.** The verdict comes from `self.classify`, the one
          classifier, over the hand-over times in `handed_at`.
        * **Not a history.** Everything here is runtime state for a LIVE identity apart
          from the cursor, which is the one durable fact. When the binding ends the
          binding facts, the injection and the digest go with it. What survives is the
          cursor and the hand-over times `handed_at` still holds, and that is what makes
          the not-addressable case answerable at all.
        * **Not a ledger.** Nothing is recorded because this was asked. Asking costs two
          cursor read (`moxi comms explain` is not on any hot path).

        `identity` is a qualified name, taken literally, exactly as `kind="agents"`
        prints it. `message_id` names the message to explain; without one the daemon
        picks the newest it can see for this identity, which is what a person wants when
        they ask why an agent has gone quiet.
        """
        identity = str(frame.get("identity") or "")
        asked = frame.get("message_id")
        now = self.clock()
        with self._lock:
            b = self.bindings.get(identity)
            binding = None if b is None else {
                "name": b.name, "scope": b.scope, "tier": b.tier, "adapter": b.adapter,
                "slot": b.slot_id, "bound_for": now - b.bound_at,
                "handed": len(b.delivered)}
            # Everything this daemon has seen delivered to the name: what the live
            # binding was handed, plus every hand-over `handed_at` still holds. The two
            # differ after an unbind, and that gap is the whole not-addressable case.
            seen = set(b.delivered) if b is not None else set()
            handed = {mid: at for (q, mid), at in self.handed_at.items() if q == identity}
            seen |= set(handed)
            waiting = sorted(self.queue.get(identity, {}), key=_as_id)
            through = self.injected_through.get(identity)
            digest = self.injected_digest.get(identity)
            activity = self.activity.get(identity)
        # With no id asked, the newest message this daemon handed the name or holds
        # queued for it.
        known = seen | set(waiting)
        message_id = str(asked) if asked is not None else (
            max(known, key=_as_id) if known else None)
        handed_at = handed.get(message_id)
        delivery = None if message_id is None else {
            "message_id": message_id,
            # The trim drops a hand-over past `THREAD_IDLE`, so a message it has dropped
            # is still explainable and its delivery age is not. Said, not guessed at.
            "delivered_for": None if handed_at is None else now - handed_at,
            "asked_for": asked is not None}
        cursor = cursors.explain(identity, message_id)
        case = case_error = None
        if message_id is not None and message_id in waiting:
            # Queued BEFORE the cursor: a queued id is not `cursor-behind-it`, whose
            # sentence says a turn that would advance it has not completed -- not why
            # it waits.
            case = CASE_WAITING
            cursor = {**cursor, "reason": cursors.WAITING,
                      "sentence": cursors.REASONS[cursors.WAITING]}
        elif message_id is not None:
            try:
                case = self.classify(identity, message_id)
            except (TypeError, ValueError) as e:
                # A cursor that is not a message id reaches `classify`'s `int()` and
                # raises. `cursor["reason"]` already names that, so the command has
                # something true to print; what it must not do is answer the whole
                # query with a traceback.
                case_error = f"{type(e).__name__}: {e}"
        return {"identity": identity,
                "bound": b is not None,
                "binding": binding,
                "delivery": delivery,
                "deliveries": len(seen),
                "waiting": waiting,
                "injected_through": through,
                "digest": digest,
                "activity_for": None if activity is None else now - activity,
                "cursor": cursor,
                "case": case,
                "case_error": case_error}

    def _resolve_query(self, frame: dict) -> dict:
        """Tier/route RESOLUTION for the send verb -- never a liveness check."""
        # The tag alone decides: the frame's `scope` and `project`, if an older verb
        # sends them, are not read.
        tag = frame.get("name") or ""
        r = self.resolve(tag)
        if not r.resolvable:
            raise Refusal(r.hint or (
                f"{tag}: no such ephemeral identity. agent-N names are never reused and a "
                "retired one is nobody -- there is nothing to deliver to."))
        bound = r.binding is not None
        ever = True if bound else self.journal.ever_bound(r.name, r.scope)
        answer = {"tier": r.tier, "route": NAMES, "qualified": r.qualified,
                  "bound": bound, "deferred": r.deferred,
                  "send_only": r.send_only,
                  # the directory fact, read at the SAME (name, scope) the send resolved
                  # to -- not the live binding's tier, which can predate a row being added
                  "standing_row": self.has_standing_row(r.name, r.scope),
                  # has this exact identity ever been bound (the custody journal)? None
                  # when the journal cannot say; the send's never-bound check reads it
                  "ever_bound": ever}
        if ever is False:
            # The identities of this name that were bound, for that refusal to name:
            # never the one asked about, never a retired one. A hint; it resolves
            # nothing.
            answer["seen_as"] = self._seen_as(r.name, r.scope)
        return answer

    def _seen_as(self, name: str, scope: str) -> list:
        """The other identities of `name` the journal has seen bound, most recent first,
        leaving out retired and send-only ones."""
        scopes = [sc for sc in self.journal.known_scopes(name) if sc != scope]
        if not scopes:
            return []
        with self._db() as db:
            return [qualify(name, sc) for sc in scopes
                    if not directory.retired(db, name, sc)
                    and not directory.is_send_only(db, name, sc)]

    def has_standing_row(self, name: str, scope: str) -> bool:
        """Is there an active standing row at this exact (name, scope)? Read per call."""
        with self._db() as db:
            return directory.standing_row(db, name, scope) is not None

    def resolve(self, tag: str) -> Resolution:
        """The identity `tag` names, by the one rule (`scoping.identity_of`): no scope
        of anyone's is read. Then whether it is bound, its tier, and whether it is
        send-only or retired."""
        with self._lock, self._db() as db:
            r = self._resolve(db, tag)
            # The row decides, bound or not: a send-only identity is never a recipient.
            if r.resolvable and r.tier == TIER_STANDING:
                r.send_only = directory.is_send_only(db, r.name, r.scope)
            return r

    def _resolve(self, db, tag: str) -> Resolution:
        """`resolve`'s body, under the lock with the directory open."""
        name, scope = split_tag(tag)
        if is_auto_name(name):
            b = self.bindings.get(name)      # auto names are global and unscoped
            return Resolution(tag, name, GLOBAL, name, TIER_EPHEMERAL, b,
                              resolvable=b is not None)
        scope = scope or GLOBAL              # a bare name is the global identity
        q = qualify(name, scope)
        if directory.retired(db, name, scope):
            # Nobody: no reader routes, holds or queues for it, and the verbs refuse it.
            return Resolution(tag, name, scope, q, TIER_CHOSEN, None, resolvable=False,
                              hint=directory.RETIRED_SEND.format(q=q, name=name))
        b = self.bindings.get(q)
        tier = TIER_STANDING if directory.standing_row(db, name, scope) else TIER_CHOSEN
        return Resolution(tag, name, scope, q, tier, b, deferred=b is None)

    # ------------------------------------------------------------------ gateway
    def _event_loop(self) -> None:
        while not self._stop.is_set():
            event = self._sub.next_event(timeout=0.2)
            if event is None:
                continue
            try:
                if event["t"] == "MESSAGE_CREATE":
                    self._route(Message.from_dict(event["d"]), backlog=False)
                elif event["t"] == "READY" and not event.get("resumed"):
                    # fresh IDENTIFY: events in the gap are gone from the socket. The
                    # record still has them -- re-derive rather than continue blind.
                    log.warning("gateway reconnected without RESUME; rescanning %s", NAMES)
                    self.rescan()
            except Exception:
                log.exception("event handling failed")

    def rescan(self) -> None:
        """Route the record's newest `RESCAN_LIMIT` messages again: mail to an unbound
        name gets a row, and mail a bound name has not been handed is routed as live."""
        channel_id = self._names_channel()
        msgs = self.gateway.history(channel_id, limit=constants.RESCAN_LIMIT)
        with self._lock:
            self.rescans += 1
        memo: dict[str, str | None] = {}   # one cursor read per name for THIS pass only
        try:
            for m in msgs:
                try:
                    self._route(m, backlog=True, cursor_memo=memo)
                except Exception:
                    # One poison message must not cost the rest of the pass. The live
                    # event loop already absorbs a bad message this way; a rescan
                    # re-reads the same record and has the same reason to.
                    log.exception("rescan: routing message %s failed",
                                  getattr(m, "id", "?"))
        finally:
            self._end_pass(memo)

    def _route_recipients(self, msg, ml, author: Author, memo, opens, backlog) -> None:
        """The per-recipient half of `_route`: resolve, index the ask, queue or claim,
        and deliver. Split out so `_route` can feed the sent index after it.

        The rules every reader of the record keeps (this, a rescan, and replay): an id
        with a row for the name is handed only by a release point, never here; a
        flagged message, or one to a name that is unbound or whose cache does not pass,
        becomes a row, never a delivery; and no row is written for an id at or below
        the name's cursor. A row that exists is never re-created or dropped here."""
        opening = None          # decided at the first recipient: does this pass create it?
        for position, tag in enumerate(ml.recipients):
            r = self.resolve(tag)
            if opens or ml.reply_to is not None:
                opening = self._index_ask(msg, ml, author, r, opens=opens, opening=opening)
            q = r.qualified
            if r.send_only:
                # A record line that names a send-only identity (typed by hand, or from
                # an older client): delivered to nobody, queued for nobody. The verbs
                # refuse such a send; this is the record's side.
                self._note_send_only(q, msg.id)
                continue
            if r.binding is None:
                # An unbound chosen or standing name: the message waits in its queue,
                # unless the name already handled it before it unbound (past its
                # cursor). A dead agent-N is inert, not misdelivered.
                if r.resolvable and r.tier in (TIER_CHOSEN, TIER_STANDING):
                    if not self._past_cursor(q, msg.id, memo):
                        with self._lock:
                            self._queue_add(q, msg.id, _epoch(msg.timestamp))
                continue
            if backlog:
                # one guarded reading of "handled?" for bound and unbound alike
                if self._past_cursor(q, msg.id, memo):
                    continue
            elif int(msg.id) <= int(r.binding.replay_hwm):
                continue                              # catch-up owns everything below
            with self._lock:
                if msg.id in self.queue.get(q, {}) or msg.id in r.binding.delivered:
                    # A row waits for a release point; a handed id is not handed again.
                    continue
            flagged = _flagged(ml, q)
            clock = now = begun = None
            for attempt in range(2):
                if not flagged:
                    clock, now, begun = self._gate_read(r.binding, memo, fresh=attempt > 0)
                with self._lock:
                    passes = not flagged and _passes(ml, position, clock, now)
                    if (not passes and not flagged and attempt == 0
                            and r.binding.releases_begun != begun):
                        # A release of the name began after this read did, and the row
                        # would be queued behind it: the read may have met the record's
                        # lag that release read past (RULE H1). One more read decides.
                        continue
                    if not passes:
                        if not flagged:
                            log.info("queued msg=%s for %s: cache %s at delivery", msg.id,
                                     q, self._describe(clock))
                        if backlog or not self._past_cursor(q, msg.id, memo):
                            self._queue_add(q, msg.id, _epoch(msg.timestamp))
                        live = False
                    else:
                        live = self._claim(r.binding, msg.id)
                break
            if not live:
                continue
            # Live delivery: no queued mail goes with it. A queued message goes out only
            # at a release point of its own name, never because this one went.
            with self._name_lock(q):
                self._deliver(r.binding, msg, ml, author, backlog=backlog)

    # ------------------------------------------------------------------ routing
    def _past_cursor(self, qualified: str, message_id: str, memo: dict) -> bool:
        """Has this identity's cursor already passed this message? The cursor is the one
        durable "handled?" fact and the daemon only ever READS it -- `memo` is scoped
        to a single routing pass so a rescan does not re-read one file per message, and
        nothing survives the pass to go stale."""
        if qualified not in memo:
            memo[qualified] = cursors.read(qualified)
        cursor = memo[qualified]
        if cursor is None:
            return False
        try:
            return int(message_id) <= int(cursor)
        except (TypeError, ValueError):
            # A cursor file whose `cursor` is not a number is unreadable, not authority.
            # Treating it as no-cursor degrades toward replay and pending -- the daemon
            # would rather show a message twice than let a corrupt file swallow it, and
            # it must never be a reason boot cannot finish.
            log.warning("cursor for %s is not a message id (%r); treating it as unset",
                        qualified, cursor)
            return False

    def _route(self, msg, *, backlog: bool, cursor_memo: dict | None = None) -> None:
        if msg.channel_id != self._names_channel():
            return          # only #names routes
        # Before the resolve, always: a register whose boundary is fixed after this point
        # must count this message as one the router has already taken responsibility for,
        # whether or not it turns out to be routable (a non-routable id can only raise the
        # boundary to a value the record itself would have answered with).
        #
        # The conversion is guarded because it now runs for EVERY #names message, ahead of
        # the machine-line parse: an id that is not a number is not one `max_message_id`
        # could ever answer with, so it can never be needed as a boundary -- and reading
        # the boundary must not be the thing that takes down a message the router would
        # otherwise have shrugged off.
        try:
            seen = int(msg.id)
        except (TypeError, ValueError):
            seen = None
        if seen is not None:
            with self._lock:
                if seen > int(self._seen_max):
                    self._seen_max = msg.id
        ml = parse_machine_line(msg.content)
        if ml is None:
            return                                    # no machine line at position zero
        self._index_audience(msg, ml)                 # at parse, before any claim
        if self.settings.use("block"):
            # symmetric: a blocked identity is neither delivered to nor routed from.
            # The setting is a bare bool, read at use, so it blocks all routing.
            log.info("block on: not routing message %s", msg.id)
            return
        author = self.attribute(msg, live=not backlog)
        memo = {} if cursor_memo is None else cursor_memo
        # The ask index: whether this message can open an ask is a per-message fact; the
        # index is fed per resolved recipient, right after the resolve and before any of
        # the loop's `continue`s, so every recipient is seen.
        opens = ml.posture == POSTURE_ONCE and author.attribution == attribution.VERIFIED
        try:
            self._route_recipients(msg, ml, author, memo, opens, backlog)
        finally:
            # The sent index is fed AFTER the recipients are claimed (the claim that
            # hands a message to a recipient's binding precedes the lock section that
            # feeds the index), so no `owed` query can see an answer in the
            # index that is not yet counted as handed. In `finally`: a recipient that
            # raised costs that recipient, never the index's view of the record.
            self._index_sent(msg, ml, author)
            if cursor_memo is None:
                # A live message is a pass of its own; a rescan ends its pass itself.
                self._end_pass(memo)

    def _index_ask(self, msg, ml, author: Author, r: Resolution, *, opens: bool,
                   opening: bool | None) -> bool | None:
        """Feed the ask index from one resolved recipient of one routed message.

        **Open.** A verified part-1 `[reply-once]` message (`opens`) gets an entry the
        first time any pass reaches it, and that pass alone adds each resolved recipient
        other than the asker to its `open` set. `opening` carries that decision from the
        first recipient to the rest; a re-parse (a rescan, a boot race) finds the entry
        held and changes nothing.

        **Close.** A message with `re:N`, where N is held and this recipient is N's asker,
        takes its author out of N's `open`. Attribution is not asked for: closing only
        relaxes, and the verb re-checks on the record anyway.

        Returns `opening` for the next recipient. Under the lock, because live routing
        and the boot rescan route concurrently.
        """
        with self._lock:
            if opens:
                if opening is None:
                    opening = msg.id not in self.asks
                    if opening:
                        self.asks[msg.id] = {"asker": author.qualified,
                                             "at": _epoch(msg.timestamp), "open": set()}
                        self._trim_asks()
                entry = self.asks.get(msg.id) if opening else None
                if (entry is not None and r.resolvable and not r.send_only
                        and r.qualified != author.qualified):
                    entry["open"].add(r.qualified)
            if ml.reply_to is not None:
                referent = self.asks.get(ml.reply_to)
                if referent is not None and r.qualified == referent["asker"]:
                    referent["open"].discard(author.qualified)
        return opening

    def _trim_asks(self) -> None:
        """Keep the newest `RESCAN_LIMIT` asks by id, cut with slack. Under the
        lock. Asks are sparse, so a live daemon may hold asks older than the rescan
        window; a restart rebuilds fewer, and a missing entry lets a send through."""
        if len(self.asks) > constants.RESCAN_LIMIT + ASK_INDEX_SLACK:
            keep = sorted(self.asks, key=_as_id)[-constants.RESCAN_LIMIT:]
            self.asks = {k: self.asks[k] for k in keep}

    def _asks_query(self, frame: dict) -> dict:
        """The asks this daemon holds open to `sender` from any of `recipients`.

            -> {"op": "query", "kind": "asks", "sender": "reviewer@proj-a",
                "recipients": ["planner@proj-a"]}
            <- {"ok": true, "asks": ["4611686018427999001"]}

        Held entries whose asker is among `recipients` (qualified names), whose `open`
        holds `sender`, whose `at` is within `ASK_TTL` of this daemon's clock, and which
        this daemon is not still holding back from `sender` (a queued ask waits in
        `sender`'s queue until a release point hands it, and an ask binds only once it
        has been handed over); newest first, at most `ASK_QUERY_LIMIT`. A NOMINATION: the verb
        reads each one back from the record and decides. Leaving an id out only relaxes.
        Nothing is resolved here.
        """
        sender = str(frame.get("sender") or "")
        recipients = {str(q) for q in (frame.get("recipients") or ())}
        now = self.clock()
        with self._lock:
            b = self.bindings.get(sender)
            withheld = {mid for mid in self.queue.get(sender, {})
                        if b is None or mid not in b.delivered}
            ids = [mid for mid, e in self.asks.items()
                   if e["asker"] in recipients and sender in e["open"]
                   and now - e["at"] <= constants.ASK_TTL and mid not in withheld]
        ids.sort(key=_as_id, reverse=True)
        return {"asks": ids[:constants.ASK_QUERY_LIMIT]}

    # ------------------------------------------------------------------ the sent index
    def _index_sent(self, msg, ml, author: Author) -> None:
        """Feed one parsed `#names` message into the sent index, once.

        Forward-only: an id already held adds nothing, so a rescan or a replay that
        re-parses a message changes nothing. A message older than `OWED_TTL` by this
        daemon's clock is not added (nothing can owe on it, and trimming would take it
        out again). Attribution is not required: an entry only
        relaxes a debt, except as an ask, which the query checks `verified` for.
        """
        epoch = _epoch(msg.timestamp)
        with self._lock:
            if msg.id in self.sent_by_id:
                return
            if self.clock() - epoch > constants.OWED_TTL:
                return
            entry = SentEntry(id=msg.id, author=author.qualified,
                              tags=tuple(ml.recipients), posture=ml.posture,
                              reply_to=ml.reply_to, at=epoch,
                              verified=author.attribution == attribution.VERIFIED)
            entries = self.sent.setdefault(author.qualified, [])
            if entries and _as_id(entries[-1].id) > _as_id(msg.id):
                bisect.insort(entries, entry, key=lambda e: _as_id(e.id))
            else:
                entries.append(entry)
            self.sent_by_id[msg.id] = entry
            self._sent_feeds += 1
            if self._sent_feeds % SENT_TRIM_EVERY == 0:
                self._trim_sent()
            # The unanswered record's first drop: the owing name's payment landed.
            for key, row in list(self.unanswered.items()):
                if self._pays(entry, row["owing"], row["author"], row["message_id"]):
                    del self.unanswered[key]

    def _pays(self, entry: SentEntry, owing: str, author: str, message_id: str) -> bool:
        """Is `entry` `owing`'s payment of `author`'s message `message_id`? Its
        own message to the author, after the message, not asking back with `once`."""
        if entry.author != owing or entry.posture == POSTURE_ONCE:
            return False
        if _as_id(entry.id) <= _as_id(message_id):
            return False
        return any(tag_matches(t, author) for t in entry.tags)

    def _note_unanswered(self, conn, frame: dict) -> None:
        identity = str(frame.get("identity") or "")
        message_id = str(frame.get("message_id") or "")
        author = str(frame.get("author") or "")
        if not (identity and message_id.isdigit() and author):
            return
        try:
            at = float(frame.get("at"))
        except (TypeError, ValueError):
            at = self.clock()
        with self._lock:
            b = self.bindings.get(identity)
            if b is None or b.conn is not conn:
                log.info("dropped an unanswered notify for %s from a connection that does "
                         "not hold it", identity)
                return
            if any(self._pays(e, identity, author, message_id)
                   for e in self.sent.get(identity, ())):
                return          # paid already, between the shim's query and this notify
            self.unanswered[(identity, message_id)] = {
                "owing": identity, "author": author, "message_id": message_id, "at": at}
        log.info("unanswered: %s ended a turn without answering %s's msg %s", identity,
                 author, message_id)

    def _unanswered_rows(self, now: float) -> list[dict]:
        """The record as `moxi comms agents` reads it, oldest first, after the second drop
: a message past `OWED_TTL` leaves. Under the lock."""
        for key in [k for k, row in self.unanswered.items()
                    if now - row["at"] > constants.OWED_TTL]:
            del self.unanswered[key]
        rows = sorted(self.unanswered.values(), key=lambda r: _as_id(r["message_id"]))
        return [{"owing": r["owing"], "author": r["author"],
                 "message_id": r["message_id"], "for": now - r["at"]} for r in rows]

    def _trim_sent(self) -> None:
        """Drop entries older than `OWED_TTL`. Under the lock."""
        cutoff = self.clock() - constants.OWED_TTL
        for author in list(self.sent):
            entries = self.sent[author]
            keep = [e for e in entries if e.at >= cutoff]
            for e in entries:
                if e.at < cutoff:
                    self.sent_by_id.pop(e.id, None)
            if keep:
                self.sent[author] = keep
            else:
                del self.sent[author]

    def _rebuild_sent_logged(self) -> None:
        try:
            count = self.rebuild_sent()
            if count:
                log.info("boot: %d message(s) of the last day read into the sent index",
                         count)
        except Exception:
            # The index starts empty and its floor is now: every debt older than boot is
            # undecidable and lets its Stop through.
            with self._lock:
                self.sent_floor = self.clock()
            log.exception("boot read of the sent index failed; debts older than this boot "
                          "are undecidable and never block a Stop")

    def rebuild_sent(self) -> int:
        """Boot: one read of every `#names` message of the last `OWED_TTL`, unbounded,
        through the gateway's `since`. The floor is that window's start.
        """
        now = self.clock()
        since = self.gateway.since
        floor = now - constants.OWED_TTL
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(floor))
        count = 0
        for m in since(self._names_channel(), stamp):
            ml = parse_machine_line(m.content)
            if ml is None:
                continue
            author = self.attribute(m, live=False)
            self._index_sent(m, ml, author)
            count += 1
        with self._lock:
            self.sent_floor = floor
        return count

    def _owed_query(self, frame: dict) -> dict:
        """What the shim needs to settle its ledger at a Stop.

            -> {"op": "query", "kind": "owed", "identity": "reviewer@proj-a",
                "authors": {"lead": "4611…600"}, "referents": ["4611…580"]}
            <- {"ok": true, "sent": {"lead": [{"id", "posture"}]},
                "referents": {"4611…580": {"own": true, "posture": "open"} | null},
                "awaiting": ["worker@proj-a"], "undelivered": false, "floor": 1759…}

        `sent`: for each author, `identity`'s entries addressed to it after that id, tags matched literally in `identity`'s scope.
        `referents`: for each id, whether it is `identity`'s own message and its posture,
        or null when the index does not hold it. `awaiting`: the bound names `identity`
        has a verified part-1 `[reply-once]` ask to, within `ASK_TTL`, that have not yet
        answered it (a message threaded to the ask, or the first message after it not
        marked `[no reply]`). `undelivered`: whether a message was claimed for
        `identity`'s binding above its `injected_through`, counting only claims since it
        registered. `awaiting` and `undelivered` are read under one lock. The query names
        no standing; the shim decides everything else.
        """
        identity = str(frame.get("identity") or "")
        raw = frame.get("authors") or {}
        if not isinstance(raw, dict):
            raise Refusal("owed: `authors` must map each author to its oldest unpaid id")
        authors = {str(a): str(i) for a, i in raw.items()}
        referents = [str(r) for r in (frame.get("referents") or ())]
        now = self.clock()
        with self._lock:
            mine = list(self.sent.get(identity, ()))
            sent = {}
            for author, after in authors.items():
                sent[author] = [
                    {"id": e.id, "posture": e.posture}
                    for e in mine if _as_id(e.id) > _as_id(after)
                    and any(tag_matches(t, author) for t in e.tags)]
            refs = {}
            for rid in referents:
                e = self.sent_by_id.get(rid)
                refs[rid] = None if e is None else {"own": e.author == identity,
                                                     "posture": e.posture}
            awaiting = self._awaiting(identity, mine, now)
            b = self.bindings.get(identity)
            through = self.injected_through.get(identity) or "0"
            undelivered = b is not None and _as_id(b.handed_max) > _as_id(through)
            floor = self.sent_floor
        return {"sent": sent, "referents": refs, "awaiting": awaiting,
                "undelivered": undelivered, "floor": floor}

    def _readable_query(self, frame: dict) -> dict:
        """When `identity` could first read each of `ids`, as far as delivery knows.

            -> {"op": "query", "kind": "readable", "identity": "reviewer@proj-a",
                "ids": ["4611…580", "4611…600"]}
            <- {"ok": true, "since": <started_at>, "handed": {"4611…580": <epoch>},
                "waiting": ["4611…600"]}

        `handed`: the asked ids this daemon handed to `identity`'s binding, with when.
        `waiting`: the asked ids that wait in `identity`'s queue. `since`: when
        this daemon started. The send verb reads these to tell when a thread went idle
        for the replier; the daemon decides nothing from them. Under the lock.
        """
        identity = str(frame.get("identity") or "")
        ids = [str(i) for i in (frame.get("ids") or ())]
        with self._lock:
            rows = self.queue.get(identity, {})
            handed = {mid: self.handed_at[(identity, mid)] for mid in ids
                      if (identity, mid) in self.handed_at}
            waiting = [mid for mid in ids if mid in rows]
            since = self.started_at
        return {"since": since, "handed": handed, "waiting": waiting}

    def _trim_handed(self) -> None:
        """Drop hand-over times older than `THREAD_IDLE`: a thread idle that long since
        a hand-over is idle by its post time too, which is older still."""
        now = self.clock()
        with self._lock:
            for key in [k for k, at in self.handed_at.items()
                        if now - at >= constants.THREAD_IDLE]:
                del self.handed_at[key]

    def _awaiting(self, identity: str, mine, now: float) -> list[str]:
        """The names `identity` awaits. Under the lock.

        An ask is `identity`'s own `[reply-once]` entry, verified, within `ASK_TTL`;
        nothing else is. For each of its tags, the live binding the tag
        names (by the one rule) is awaited until it answers: its entry to
        `identity` threaded to the ask, of any posture, or its first entry to `identity`
        after the ask not marked `[no reply]`. A name that is not bound is not awaited.
        """
        names: list[str] = []
        for ask in mine:
            if (ask.posture != POSTURE_ONCE or not ask.verified
                    or now - ask.at > constants.ASK_TTL):
                continue
            for tag in ask.tags:
                for b in self.bindings.values():
                    q = b.qualified
                    if q == identity or q in names or not tag_matches(tag, q):
                        continue
                    if not self._answered(ask, q, identity):
                        names.append(q)
        return sorted(names)

    def _answered(self, ask: SentEntry, answerer: str, asker: str) -> bool:
        """Has `answerer` answered `ask` (by `asker`) in the index? Under the lock."""
        for e in self.sent.get(answerer, ()):
            if _as_id(e.id) <= _as_id(ask.id):
                continue
            if not any(tag_matches(t, asker) for t in e.tags):
                continue
            if e.reply_to == ask.id or e.posture != POSTURE_NONE:
                return True
        return False

    def _queue_unbound(self, b: Binding) -> None:
        """At unbind: every id this binding was handed that has no row, that its adapter
        did not name `handled`, and that its cursor has not passed gets a row, so the
        name's next binding gets it at a release point. Rows are never deleted here. An
        `agent-N` gets no deferred mail: its rows go with its binding, since a later
        session numbered the same is someone else. Called under the lock."""
        if b.tier == TIER_EPHEMERAL:
            dropped = sorted(self.queue.get(b.qualified, {}), key=_as_id)
            if dropped:
                self._queue_drop(b.qualified, dropped)
            return
        memo: dict = {}
        back = [mid for mid in b.delivered
                if mid not in b.handled and mid not in self.queue.get(b.qualified, {})
                and not self._past_cursor(b.qualified, mid, memo)]
        # Written here, not through `_queue_add`: the binding is still in `bindings`
        # (the caller queues before it drops it), and `_queue_add` refuses every id the
        # live binding was handed -- which is every id here. Epochs fill in at the flush.
        for mid in back:
            self.queue.setdefault(b.qualified, {})[mid] = None
            self._queue_unwritten[(b.qualified, mid)] = True
        if back:
            log.info("unbind %s: %d handed message(s) not yet handled are queued again",
                     b.qualified, len(back))

    def _claim(self, binding: Binding, message_id: str) -> bool:
        """Atomically claim a message for a binding. Live routing, catch-up and a release
        race by construction; the claim is what makes at-least-once not mean
        twice-in-one-breath."""
        with self._lock:
            if message_id in binding.delivered:
                return False
            binding.delivered.add(message_id)
            if _as_id(message_id) > _as_id(binding.handed_max):
                binding.handed_max = message_id      # the `owed` query's `undelivered`
            return True

    # ------------------------------------------------------------------ the queue
    def _queue_add(self, qualified: str, message_id: str, epoch: float | None) -> None:
        """One row: `message_id` waits for `qualified`. A row that exists is left as it
        is. An id the name's live binding was handed gets none (a reader that raced the
        hand-over). An `agent-N` gets a row only while it is bound. Under the lock; the
        record write follows at `_queue_flush`."""
        rows = self.queue.get(qualified)
        if rows is not None and message_id in rows:
            return
        b = self.bindings.get(qualified)
        if b is not None and message_id in b.delivered:
            return
        if b is None and is_auto_name(split_tag(qualified)[0]):
            return
        self.queue.setdefault(qualified, {})[message_id] = epoch
        self._queue_unwritten[(qualified, message_id)] = True

    def _queue_drop(self, qualified: str, ids) -> None:
        """Delete rows. Only a `handled` report deletes a row of a name that can still
        be handed it; the other callers drop rows nobody can take (an `agent-N`'s at its
        unbind or at boot, a send-only name's). Under the lock."""
        rows = self.queue.get(qualified, {})
        for mid in ids:
            rows.pop(mid, None)
            self._queue_unwritten[(qualified, mid)] = False
        if not rows:
            self.queue.pop(qualified, None)

    def _queue_flush(self) -> None:
        """Write the queue's changes to the record, then fill in the epoch of any row
        added without one. Never raises: a write that fails is logged, kept, and tried
        again at the next pass, and memory stays the authority meanwhile."""
        with self._queue_write_lock:
            with self._lock:
                todo = dict(self._queue_unwritten)
            if todo:
                adds, removes = {}, {}
                for (q, mid), add in todo.items():
                    (adds if add else removes).setdefault(q, []).append(mid)
                try:
                    for q, ids in removes.items():
                        self.gateway.queue_remove(q, ids)
                    for q, ids in adds.items():
                        self.gateway.queue_add(q, ids)
                except Exception:                                  # noqa: BLE001
                    log.exception("the record write of %d queue change(s) failed; kept in "
                                  "memory and tried again at the next pass", len(todo))
                else:
                    with self._lock:
                        for key, add in todo.items():
                            if self._queue_unwritten.get(key) is add:
                                del self._queue_unwritten[key]
        with self._lock:
            unknown = [(q, mid) for q, rows in self.queue.items()
                       for mid, at in rows.items() if at is None]
        for q, mid in unknown:
            try:
                msg = self.gateway.get_message(mid)
            except Exception:                                      # noqa: BLE001
                continue
            if msg is None:
                continue
            with self._lock:
                rows = self.queue.get(q)
                if rows is not None and rows.get(mid, 0) is None:
                    rows[mid] = _epoch(msg.timestamp)

    def _waiting_view(self, now: float) -> dict:
        """`agents`' `waiting`: qualified -> {"count", "oldest"}, the oldest row's message
        time (epoch seconds; a row whose time is not read yet counts as now). Under the
        lock."""
        view = {}
        for q, rows in sorted(self.queue.items()):
            if not rows:
                continue
            times = [at for at in rows.values() if at is not None]
            view[q] = {"count": len(rows), "oldest": min(times) if times else now}
        return view

    def queued_ids(self) -> set:
        """Every id with a row for any name: what the retention trim keeps."""
        with self._lock:
            return {mid for rows in self.queue.values() for mid in rows}

    # ------------------------------------------------------------------ send-only lines
    def _note_send_only(self, qualified: str, message_id: str) -> None:
        """One log line per message id that names a send-only identity on its machine
        line; a re-read of the same line (a rescan, the boot read) says nothing."""
        with self._lock:
            if message_id in self.send_only_seen:
                return
            self.send_only_seen[message_id] = None
            while len(self.send_only_seen) > 4096:
                self.send_only_seen.pop(next(iter(self.send_only_seen)))
        log.info("msg=%s names %s, a send-only identity: delivered to nobody, held for "
                 "nobody", message_id, qualified)

    # ------------------------------------------------------------------ the cache read
    def _take_session(self, b: Binding, frame: dict, *, at: float, turn: bool = False,
                      register: bool = False) -> None:
        """A frame from `b`'s own connection: its session id, `projects_root` and
        `turned`. Called under the lock.

        One session id, one live binding: an id another live binding holds is refused
        and logged, and `b` keeps the id it had (a new binding has none, so it reads
        `unknown` and is held until the old connection closes). A new id starts `turned`
        false; the frame's `turned` and a `kind: turn` raise it. `turned` is never
        lowered for a session id this daemon has seen turn, so a respawned shim, which
        starts `turned` false, cannot make a session that has had turns read `fresh`. A
        frame with neither field leaves `projects_root` as it was.
        """
        sid = frame.get("session_id")
        if isinstance(sid, str) and sid and sid != b.session_id:
            owner = self.session_owner.get(sid)
            holder = self.bindings.get(owner) if owner is not None else None
            if holder is not None and holder is not b and holder.session_id == sid:
                log.warning("session id %s reported by %s is held by the live binding "
                            "%s; refused (one session id, one binding), %s keeps %s",
                            sid, b.qualified, owner, b.qualified, b.session_id or "none")
            else:
                if b.session_id and self.session_owner.get(b.session_id) == b.qualified:
                    del self.session_owner[b.session_id]
                b.session_id = sid
                b.turned = False
                self.session_owner[sid] = b.qualified
        if isinstance(sid, str) and sid and sid == b.session_id:
            b.session_seen_at = at
            if frame.get("turned") is True:
                b.turned = True
        root = frame.get("projects_root")
        if isinstance(root, str) and root:
            b.projects_root = root
        source = frame.get("cache_source")
        if isinstance(source, str) and source:
            b.cache_source = source
        if "agent_model" in frame:
            # A launcher that re-read its config and could not name the default agent
            # sends null: the report it made before may be stale, so it is withdrawn.
            reported = frame.get("agent_model")
            b.agent_model = dict(reported) if isinstance(reported, dict) else cache.MISSING
        endpoint = frame.get("endpoint")
        if isinstance(endpoint, bool) and endpoint is not b.endpoint:
            b.endpoint, b.endpoint_since = endpoint, at
        if register:
            b.new_session = frame.get("new_session") is True
        if b.session_id:
            b.new_session = False
        if turn:
            b.turned = True
        if b.session_id is not None:
            if b.session_id in self.turned_ids:
                b.turned = True
            elif b.turned:
                self.turned_ids.add(b.session_id)

    def _read(self, binding: Binding, *, quiet: bool = False):
        """The daemon's read of one binding's cache: `cache.clock_state`, comms' one
        seam to cache, on the binding's kind and session (the id, root and `turned` its
        adapter reported), with the daemon's drift store. It passes on `warm`, `fresh`
        and `no-cache`, for every adapter alike. An exception anywhere in here reads
        `unknown`, with one log line; it never falls through to a delivery on its own.
        `quiet` (a settle window's reads) leaves out the read's journal notes, which the
        read at the window's frame logged already."""
        try:
            with self._lock:
                adapter, sid = binding.adapter, binding.session_id
                root, turned = binding.projects_root, binding.turned
                source, new_session = binding.cache_source, binding.new_session
                agent_model, bound_at = binding.agent_model, binding.bound_at
            clock = cache.clock_state(adapter, sid, projects_root=root, turned=turned,
                                      name=binding.qualified, cache_source=source,
                                      new_session=new_session, agent_model=agent_model,
                                      bound_at=bound_at, store=self.cache_store,
                                      kinds=self.kinds)
            self._drift_lines(binding, clock, quiet=quiet)
        except Exception as e:                                     # noqa: BLE001
            log.exception("the cache read failed for %s; it reads unknown",
                          getattr(binding, "qualified", "?"))
            clock = cache.Clock(cache.UNKNOWN,
                                reason=f"the daemon's check failed ({type(e).__name__})",
                                code=cache.READ_FAILED)
        return clock

    def _read_memo(self, binding: Binding, memo: dict | None):
        """(the read, the daemon's clock at it), read once per binding per routing pass
        and kept in `memo` for the rest of it."""
        clock, now, _ = self._gate_read(binding, memo)
        return clock, now

    def _gate_read(self, binding: Binding, memo: dict | None, *, fresh: bool = False):
        """(the read, the daemon's clock at it, the binding's `releases_begun` as the read
        began), read once per binding per routing pass and kept in `memo` for the rest
        of it. `fresh` reads again and keeps the new read."""
        cache_of = memo.setdefault(_GATE_MEMO, {}) if memo is not None else None
        if cache_of is not None and not fresh:
            kept = cache_of.get(binding.qualified)
            if kept is not None and kept[0] is binding:
                return kept[1], kept[2], kept[3]
        with self._lock:
            begun = binding.releases_begun
        clock = self._read(binding)
        now = self.clock()
        if cache_of is not None:
            cache_of[binding.qualified] = (binding, clock, now, begun)
        return clock, now, begun

    @staticmethod
    def _describe(clock) -> str:
        """For the journal and the log, with the hold's one meaning of a cold
        duration: `cold for 12m (last request 42m ago)` for a read with an age and a
        lifetime, `cold (<reason>; last request 3m ago)` for one without a lifetime, and
        `unreadable (<reason>)`."""
        if clock is not None and clock.kind == cache.COLD:
            age, ttl = clock.age_seconds, getattr(clock, "ttl_seconds", None)
            if age is not None and ttl is not None:
                return f"cold for {cache.span(age - ttl)} (last request {cache.span(age)} ago)"
            reason = clock.reason or "no cache lifetime to count it by"
            if age is not None:
                return f"cold ({reason}; last request {cache.span(age)} ago)"
            return f"cold ({reason})"
        reason = getattr(clock, "reason", None) or "no reason given"
        return f"unreadable ({reason})"

    # ------------------------------------------------------------------ the drift record
    def _drift_lines(self, binding: Binding, clock, *, quiet: bool = False) -> None:
        """comms' words about a Codex read's drift results: the journal notes (not on a
        `quiet` read), a lowering cache's store wrote (said once per model and
        lifetime), and a measured lifetime held because the drift record cannot be read
        or accepted (said once per reason). cache owns the record and decides every
        verdict; this only logs."""
        for note in () if quiet else clock.notes or ():
            log.info("%s", note)
        lowered = clock.lowering
        if lowered and clock.kind != cache.UNKNOWN and getattr(
                self.cache_store, "learning", False):
            key = ("lowering", lowered.get("harness"), lowered.get("provider"),
                   lowered.get("model"), lowered.get("lifetime"))
            with self._drift_lock:
                fresh = key not in self._drift_said
                self._drift_said.add(key)
            if fresh:
                self._say(self._lowering_text(binding, lowered))
        if clock.code == cache.DRIFT_HELD and clock.reason:
            key = ("held", clock.reason)
            with self._drift_lock:
                fresh = key not in self._drift_said
                self._drift_said.add(key)
            if fresh:
                where = getattr(self.cache_store, "path", None)
                self._say(f"cache clock: {clock.reason}"
                          + (f" ({where})" if where else "")
                          + ". Every codex read on a measured cache lifetime is held until "
                          "it is fixed or the record is removed; removing it restores the "
                          "lifetimes the table ships.")

    @staticmethod
    def _lowering_text(binding: Binding, lowered: dict) -> str:
        at = lowered.get("at")
        when = time.strftime("%H:%M", time.localtime(at)) if isinstance(at, (int, float)) \
            else "?"
        cached, inp = lowered.get("cached"), lowered.get("input")
        tokens = (f"cached {cached:,} of {inp:,} tokens" if isinstance(cached, int)
                  and isinstance(inp, int) else "cached tokens not known")
        return (f"cache clock: {lowered['model']} on {lowered['harness']} now counts "
                f"{cache.span(lowered['lifetime'])}, down from {cache.span(lowered['from'])}: "
                f"{binding.qualified}'s request at {when} (thread {lowered.get('thread')}) "
                f"found its cache cold {cache.span(lowered.get('gap') or 0)} after the one "
                f"before ({tokens}). Raising it takes a new probe or the operator's word, "
                "written as a change to the lifetime table.")

    def _say(self, text: str) -> None:
        """One daemon log line; never raises."""
        log.warning("%s", text)

    # ------------------------------------------------------------------ release points
    def _on_ready(self, b: Binding, source) -> None:
        """`activity kind=ready` from the binding's own connection. `startup`, `clear`
        and `compact` start a context with nothing cached to lose, so they release every
        row without a read. Any other source (`resume`, `fork`, a value this daemon does
        not know) releases every row only on a passing read. A `ready` with no `source`
        comes from an adapter started before the queue, and releases nothing."""
        if not isinstance(source, str) or not source:
            return
        if source not in SOURCES_UNREAD and not self._passing(b):
            return
        self._release(b, flagged=True)

    def _on_turn(self, b: Binding, frame: dict) -> None:
        """`activity kind=turn` from the binding's own connection. Carrying `handled`, it
        is a release point that stays open for a settle window (RULE H1), then settles.
        Its read passing, it releases every row at once and opens no window. Its read
        not passing, whatever the queue holds, it opens the binding's window
        (`RELEASE_SETTLE_STEPS`): the harness may not have written the turn yet, and a
        send can queue a row a moment later in the same lag. The window releases every
        row then queued at its first read that passes, except the ids this frame names
        `unhandled`. A newer `turn` of the binding closes the window before its own
        read; a `ready`, `look` or register leaves it open. Without `handled` the frame
        comes from an adapter started before the queue: it releases nothing and deletes
        nothing."""
        opened = time.monotonic()
        self._close_window(b)
        handled = frame.get("handled")
        if not isinstance(handled, list):
            return
        unhandled = frame.get("unhandled")
        unhandled = unhandled if isinstance(unhandled, list) else []
        if self._passing(b):
            self._release(b, flagged=True)
        else:
            self._open_window(b, opened, unhandled)
        self._settle(b, handled, unhandled)

    def _on_look(self, b: Binding) -> None:
        """`activity kind=look` from the binding's own connection: its adapter found the
        session's endpoint with the session idle. It releases every row on a passing
        read, as a turn end does, and settles nothing: a `ready` the session raised before
        its adapter could watch loses no mail. It takes one read and leaves an open
        settle window as it is: a `look` that meets the record's lag does not cut the
        window short."""
        if self._passing(b):
            self._release(b, flagged=True)

    def _on_register(self, b: Binding) -> None:
        """After a register's replay: the rows of unflagged messages, on a passing read.
        A bind is how offline mail reaches a name. Flagged rows wait for a real
        boundary: a reconnect is not a turn. A register opens no settle window."""
        self._queue_flush()
        with self._lock:
            if not self.queue.get(b.qualified):
                return
        if self._passing(b):
            self._release(b, flagged=False)

    def _passing(self, b: Binding) -> bool:
        """A release point's read: it passes or it does not. The send's cold-at is never
        read here: a row already waits, and it waits for a release point that reads a
        passing cache or reads none."""
        clock = self._read(b)
        if clock.kind in cache.PASSING:
            return True
        log.info("release for %s waits: its cache reads %s", b.qualified,
                 self._describe(clock))
        return False

    def _release(self, binding: Binding, *, flagged: bool, exclude=()) -> int:
        """Hand the name's rows this binding was not handed already, oldest first, each
        with `held: true`. `flagged=False` leaves the rows of `[held]` messages; ids in
        `exclude` are left too (a settle window's frame named them `unhandled`). Returns
        how many went out. The rows stay until the adapter names them `handled`.

        A row whose message the record does not have, or whose line zero does not
        parse, is skipped and kept: only a `handled` report deletes a row, and the trim
        keeps every queued message, so only a record damaged by hand gets here. It is
        logged once. A record read that raises leaves the rest for the next release
        point.
        """
        q = binding.qualified
        handed = 0
        with self._name_lock(q):
            with self._lock:
                if self.bindings.get(q) is not binding:
                    return 0
                binding.releases_begun += 1
                order = sorted((mid for mid in self.queue.get(q, {})
                                if mid not in binding.delivered and mid not in exclude),
                               key=_as_id)
            for n, mid in enumerate(order):
                try:
                    msg = self.gateway.get_message(mid)
                except Exception:                                  # noqa: BLE001
                    log.exception("release for %s: reading %s failed; %d row(s) wait for "
                                  "the next release point", q, mid, len(order) - n)
                    break
                ml = parse_machine_line(msg.content) if msg is not None else None
                if ml is None:
                    with self._lock:
                        first = (q, mid) not in self._row_unreadable_logged
                        self._row_unreadable_logged.add((q, mid))
                    if first:
                        log.warning("queued message %s for %s is %s; its row stays and "
                                    "is skipped at every release", mid, q,
                                    "gone from the record" if msg is None
                                    else "not a machine line the daemon can read")
                    continue
                if not flagged and _flagged(ml, q):
                    continue
                with self._lock:
                    if mid not in self.queue.get(q, {}):
                        continue                 # settled meanwhile
                if not self._claim(binding, mid):
                    continue
                self._deliver(binding, msg, ml, self.attribute(msg, live=False),
                              backlog=False, held=True)
                handed += 1
        self._queue_flush()
        if handed:
            log.info("released %d queued message(s) to %s", handed, q)
        return handed

    # ------------------------------------------------------------------ settle windows
    def _open_window(self, b: Binding, opened: float, unhandled) -> None:
        """Open `b`'s settle window (RULE H1) for a `turn` frame that arrived at
        `opened` (`time.monotonic`) and whose read did not pass. Any window the name
        still has is closed first."""
        w = SettleWindow(binding=b, opened=opened, steps=tuple(RELEASE_SETTLE_STEPS),
                         exclude=frozenset(str(mid) for mid in unhandled))
        with self._window_cond:
            old = self._windows.get(b.qualified)
            if old is not None:
                old.closed = True
            self._windows[b.qualified] = w
            self._window_cond.notify_all()

    def _close_window(self, b: Binding) -> None:
        """Close `b`'s open settle window, if it has one: a newer `turn` frame of the
        binding, or the binding's end. Another binding's window under the same name is
        left to find its binding gone."""
        with self._window_cond:
            w = self._windows.get(b.qualified)
            if w is not None and w.binding is b:
                w.closed = True
                del self._windows[b.qualified]

    def _window_loop(self) -> None:
        """The one worker for every settle window: it waits until the next window's
        next read is due and makes it. A frame's handler never sleeps for a window."""
        while not self._stop.is_set():
            with self._window_cond:
                due = [w for w in self._windows.values() if not w.closed]
                if not due:
                    self._window_cond.wait(1.0)
                    continue
                w = min(due, key=SettleWindow.due)
                wait = w.due() - time.monotonic()
                if wait > 0:
                    self._window_cond.wait(wait)
                    continue
            try:
                self._window_read(w)
            except Exception:                                      # noqa: BLE001
                log.exception("settle window for %s failed; its rows wait for the next "
                              "release point", w.binding.qualified)
                with self._window_cond:
                    w.closed = True
                    if self._windows.get(w.binding.qualified) is w:
                        del self._windows[w.binding.qualified]

    def _window_read(self, w: SettleWindow) -> None:
        """One read of a settle window. A read that passes closes the window and
        releases every row then queued but the frame's `unhandled` ids. The last step's
        read not passing closes it with one log line. Its reads log nothing else: the
        frame's own read said why the release waited."""
        b, q = w.binding, w.binding.qualified
        with self._lock:
            bound = self.bindings.get(q) is b
        if not bound:
            self._close_window(b)
            return
        clock = self._read(b, quiet=True)
        passed = clock.kind in cache.PASSING
        with self._window_cond:
            if w.closed:
                return                        # a newer turn or the binding's end
            w.taken += 1
            ran_out = not passed and w.taken >= len(w.steps)
            if passed or ran_out:
                w.closed = True
                if self._windows.get(q) is w:
                    del self._windows[q]
        if passed:
            self._release(b, flagged=True, exclude=w.exclude)
        elif ran_out:
            log.info("release for %s: its cache still reads %s %gs after the turn ended; "
                     "the rows wait for the next release point", q, self._describe(clock),
                     w.steps[-1])

    def _settle(self, binding: Binding, handled, unhandled) -> None:
        """A `turn` frame's report, after its release. Each `handled` id this binding was
        handed loses its row. Each `unhandled` id it was handed gets a row (an
        `agent-N` too, while it is bound) and leaves `delivered`, so the next release
        point hands it again; not the release of the frame that reported it, which ran
        first. An id this binding was not handed is ignored and logged."""
        q = binding.qualified
        with self._lock:
            for raw in handled:
                mid = str(raw)
                if mid not in binding.delivered:
                    log.info("turn from %s names msg=%s handled, which it was not handed; "
                             "ignored", q, mid)
                    continue
                binding.handled.add(mid)
                if mid in self.queue.get(q, {}):
                    self._queue_drop(q, [mid])
            for raw in unhandled:
                mid = str(raw)
                if mid not in binding.delivered:
                    log.info("turn from %s names msg=%s unhandled, which it was not "
                             "handed; ignored", q, mid)
                    continue
                binding.delivered.discard(mid)
                if self.bindings.get(q) is binding and mid not in self.queue.get(q, {}):
                    self.queue.setdefault(q, {})[mid] = None
                    self._queue_unwritten[(q, mid)] = True
        self._queue_flush()

    def _end_pass(self, memo: dict) -> None:
        """The end of one routing pass, rescan or replay: the cache reads it kept go, and
        the queue's changes reach the record. Never raises."""
        memo.pop(_GATE_MEMO, None)
        self._queue_flush()

    # ------------------------------------------------------------------ boot
    def _load_queue(self) -> None:
        """Boot, before the socket listens: the migration once, then every row. A row
        for an `agent-N` is dropped: its numbering restarts with the daemon, so the name
        would be a later, unrelated session's. A read that RAISES stops the start."""
        if self.gateway.meta_get(META_QUEUE_MIGRATED) is None:
            self._migrate_queue()
        rows = list(self.gateway.queue_rows())
        dropped = []
        with self._lock:
            self.queue = {}
            for mid, q in rows:
                mid, q = str(mid), str(q)
                if is_auto_name(split_tag(q)[0]):
                    dropped.append((q, mid))
                    continue
                self.queue.setdefault(q, {})[mid] = None
            for q, mid in dropped:
                self._queue_unwritten[(q, mid)] = False
            self._queue_loaded = True
        self._queue_flush()
        n = sum(len(v) for v in self.queue.values())
        if n:
            log.info("boot: %d queued message row(s) for %d name(s)", n, len(self.queue))
        if dropped:
            log.info("boot: %d queue row(s) for agent-N names dropped: an agent-N ends "
                     "with its binding", len(dropped))

    def _migrate_queue(self) -> None:
        """Once, at the first boot of a daemon with the queue: the mail an older daemon
        held moves into it. The gate file's `held` ids, past neither the cursor nor gone
        from the record, become rows (its `refused` half is dropped: refused mail was
        never to be delivered). Every `[held]` or `[next-turn]` message gets a row for
        each recipient that resolves, is not an `agent-N` or send-only, and has not
        passed it. Then the gate file goes, the refusals table goes, and the record
        says it is done. No row is written for an id at or below a name's cursor."""
        memo: dict = {}
        rows: dict[str, set] = {}
        try:
            raw = json.loads(self.gate_path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as e:
            log.warning("queue migration: %s could not be read (%s); read as empty",
                        self.gate_path, e)
            raw = {}
        held = raw.get("held") if isinstance(raw, dict) else None
        for q, ids in (held.items() if isinstance(held, dict) else ()):
            if not isinstance(ids, list) or is_auto_name(split_tag(str(q))[0]):
                continue
            for mid in (str(i) for i in ids):
                if not mid.isdigit() or self._past_cursor(str(q), mid, memo):
                    continue
                if self.gateway.get_message(mid) is None:
                    continue
                rows.setdefault(str(q), set()).add(mid)
        # A box with no directory has never routed a message: there is nothing to scan.
        channel_id = self._names_channel() if self.db_path.exists() else None
        found = {m.id: m for token in (HELD_TOKEN, NEXT_TURN_TOKEN)
                 for m in (self.gateway.marked(channel_id, token) if channel_id else ())}
        for m in sorted(found.values(), key=lambda m: int(m.id)):
            ml = parse_machine_line(m.content)
            if ml is None or not ml.held:
                continue                     # the token in prose, not on line zero
            for tag in ml.recipients:
                r = self.resolve(tag)
                if not r.resolvable or r.tier == TIER_EPHEMERAL or r.send_only:
                    continue
                if not _flagged(ml, r.qualified):
                    continue
                if not self._past_cursor(r.qualified, m.id, memo):
                    rows.setdefault(r.qualified, set()).add(m.id)
        for q, ids in rows.items():
            self.gateway.queue_add(q, sorted(ids, key=_as_id))
        self.gate_path.unlink(missing_ok=True)
        drop = getattr(self.gateway, "drop_refusals", None)
        if drop is not None:
            drop()
        self.gateway.meta_set(META_QUEUE_MIGRATED, str(int(self.clock())))
        log.info("boot: queue migration made %d row(s) from held mail",
                 sum(len(v) for v in rows.values()))

    def _name_lock(self, qualified: str) -> threading.RLock:
        with self._lock:
            lock = self._release_locks.get(qualified)
            if lock is None:
                lock = self._release_locks[qualified] = threading.RLock()
            return lock

    def _deliver(self, binding: Binding, msg, ml, author: Author, *, backlog: bool,
                 held: bool = False) -> None:
        # The audience fill-in lives HERE, the one point live routing, the rescan and
        # register replay all pass through, so all three deliver the same payload. A
        # defect in it costs the audience fields, never the delivery.
        try:
            audience, audience_source = self._audience_of(msg, ml)
        except Exception:
            log.exception("audience fill-in failed for msg=%s; delivering it as unknown",
                          msg.id)
            audience, audience_source = None, AUDIENCE_UNKNOWN
        payload = {
            "message_id": msg.id,
            "channel_id": msg.channel_id,
            "room": self.gateway.channel_name(msg.channel_id) or msg.channel_id,
            "author": {"name": author.qualified, "tier": author.tier,
                       "attribution": author.attribution},
            "recipients": list(ml.recipients),
            # the posture, from this message's own line zero: "open", "none", "optional"
            # or "once".
            "posture": ml.posture,
            # `[open]`: a new send that stated open, the root of a thread with limits.
            "open_mark": ml.open_mark,
            "backlog": backlog,
            "text": ml.text,
            # correlation comes from the record's own line zero (a post carries no
            # reply field).
            "reply_to": ml.reply_to,
            "audience": audience,
            "audience_source": audience_source,
            "timestamp": msg.timestamp,
        }
        if ml.thread_mark:
            # "one-left" or "last", the thread mark the send verb wrote on a reply in an
            # open thread; absent otherwise.
            payload["thread_mark"] = ml.thread_mark
        if binding.adapter == SEND_ONLY_ADAPTER:
            # Never a deliver frame to a send-only binding, whatever path got here.
            log.warning("not delivering msg=%s to %s: a send-only binding receives "
                        "nothing", msg.id, binding.qualified)
            with self._lock:
                binding.delivered.discard(msg.id)
            return
        if held:
            # Handed from the queue: the recipient is told it waited, and how long.
            # Replay and live delivery send no `held`.
            payload["held"] = True
        try:
            binding.conn.send({"op": "deliver", "payload": payload})
        except Exception:
            # A failed hand-off is queued, not dropped: the id leaves `delivered`, and
            # the name's next release point hands it again. Not for an `agent-N`, whose
            # mail ends with its binding.
            log.warning("delivery to %s failed (socket gone); msg=%s is queued",
                        binding.qualified, msg.id)
            with self._lock:
                binding.delivered.discard(msg.id)
                if binding.tier != TIER_EPHEMERAL:
                    self._queue_add(binding.qualified, msg.id, _epoch(msg.timestamp))
            self._queue_flush()
            return
        with self._lock:
            now = self.clock()
            # The hand-over time, for `query kind="readable"` and `moxi comms explain`: live
            # routing, released held mail and replay all hand over here.
            self.handed_at[(binding.qualified, msg.id)] = now
        log.info("deliver msg=%s room=%s to=%s backlog=%s",
                 msg.id, payload["room"], binding.qualified, backlog)

    # ------------------------------------------------------------------ the audience
    def _index_audience(self, msg, ml) -> None:
        """Record one parsed message's EFFECTIVE audience.

        Its written audience if it has one; else, with no `re:`, None (known none); else
        its referent's effective audience if the index holds it; else
        AUDIENCE_INDEX_UNKNOWN. A referent held as unknown passes unknown on, so unknown
        never decays into known none down a chain. The index stores the effective value,
        so a chain of hand-typed replies inherits transitively with one lookup and no
        walk. Under the daemon's one lock, because live routing, the rescan and register
        replay feed it concurrently.

        A re-index never replaces a known value with unknown: when a rescan or a
        replay re-parses a message whose referent has left the index, the entry already
        held stands. A recomputed known value (tag or None) does replace the entry, which
        is how a reply that raced the boot rescan heals once the rescan reaches it.
        """
        with self._lock:
            effective = ml.audience
            if effective is None and ml.reply_to is not None:
                effective = self.audiences.get(ml.reply_to, AUDIENCE_INDEX_UNKNOWN)
            if effective is AUDIENCE_INDEX_UNKNOWN and msg.id in self.audiences:
                return                  # keep what is held; unknown adds nothing to it
            self.audiences[msg.id] = effective
            if len(self.audiences) > constants.RESCAN_LIMIT + AUDIENCE_INDEX_SLACK:
                # Bounded to the rescan window: the newest RESCAN_LIMIT ids stay, by id
                # and not by arrival, because the boot race lets a live id in first.
                keep = sorted(self.audiences, key=_as_id)[-constants.RESCAN_LIMIT:]
                self.audiences = {k: self.audiences[k] for k in keep}

    def _audience_of(self, msg, ml) -> tuple[str | None, str | None]:
        """(audience, audience_source) for one delivery -- a dictionary lookup, no read.

        Touches ABSENCE only: an audience on line zero is delivered as typed, even where
        it differs from the referent's, because the verb is where a change is refused and
        the daemon polices no line zero. Nothing here reaches the gateway:
        a referent the index does not hold, or holds as unknown, is `unknown`, said in the
        payload. A referent held as known none delivers no mark.
        """
        if ml.audience is not None:
            return ml.audience, AUDIENCE_WRITTEN
        if ml.reply_to is None:
            return None, None
        with self._lock:
            known = ml.reply_to in self.audiences
            inherited = self.audiences.get(ml.reply_to, AUDIENCE_INDEX_UNKNOWN)
        if inherited is None:
            return None, None           # the referent is indexed and carries no audience
        if inherited is not AUDIENCE_INDEX_UNKNOWN:
            return inherited, AUDIENCE_INHERITED
        if known:
            log.info("audience unknown: msg=%s answers re:%s, whose own audience this "
                     "daemon could not work out (its chain reaches a referent it never "
                     "indexed)", msg.id, ml.reply_to)
        else:
            log.info("audience unknown: msg=%s answers re:%s, which this daemon has not "
                     "indexed (older than the rescan window, never in the record, or not "
                     "yet reached by the boot rescan)", msg.id, ml.reply_to)
        return None, AUDIENCE_UNKNOWN

    # ------------------------------------------------------------------ attribution
    def attribute(self, msg, *, live: bool) -> Author:
        """What the record proves, plus the one thing only the daemon knows.

        A slot webhook's author is *whoever holds the slot right now*, and that is
        daemon-authoritative for LIVE delivery, straight out of memory. Replay no longer
        stops at the display name: `from_record` asks the custody journal who held the
        seat at the message's timestamp and runs the SAME match-gate. Live is the fast
        path for the same question, not a different one.

        MATCH-GATED: `verified` takes TWO independent facts
        agreeing. The live binding proves the SLOT (a credential was spent by its
        holder); the wire username is the sender's CLAIM about who spoke, and it is
        what the record will show. When they agree, the binding's identity is
        verified. When they disagree -- an out-of-band spender of the slot's webhook
        posting under some other name -- the holder is not the speaker: fall through to
        `from_record`, which asks the journal the same question about the same instant
        and gets the same disagreement. A claim never earns verified, live or replayed."""
        with self._db() as db:
            if live and msg.author_kind == "webhook":
                row = directory.webhook(db, msg.author_id)
                if row is not None and row["slot_id"] is not None:
                    with self._lock:
                        q = self.by_slot.get(row["slot_id"])
                        if q is not None:
                            b = self.bindings[q]
                            # compare fully qualified forms: the wire username IS the
                            # qualified name (`planner@proj-a`, global renders bare)
                            if attribution.from_display(
                                    msg.author_display).qualified == b.qualified:
                                return Author(b.name, b.scope, b.qualified, b.tier,
                                              attribution.VERIFIED)
            return attribution.from_record(db, msg)

    # ------------------------------------------------------------------ catch-up
    def _replay(self, binding: Binding) -> None:
        """Replay past the cursor, then `replay_complete`.

        Replay is for mail with no row: live mail handed to an earlier binding and not
        handled when the daemon or the adapter died, and mail posted while the daemon
        was down beyond the boot rescan's window. It skips every id with a row. A flagged
        message it finds with no row becomes a row. The cache is read once, and each
        other message is decided after it as live routing decides it: a passing read
        delivers, an `unknown` read delivers by the message's own cold-at, and the rest
        become rows. `replay_complete`'s cursor covers only what was handed.
        """
        if binding.adapter == SEND_ONLY_ADAPTER:
            # Nothing was ever kept for it, so there is nothing to catch up on.
            return
        truncated = False
        memo: dict = {}
        q = binding.qualified
        if binding.tier == TIER_EPHEMERAL:
            # an ephemeral identity receives no deferred mail, ever -- nothing to replay
            mine = []
            cursor = None
        else:
            cursor = cursors.read(q)
            channel_id = self._names_channel()
            candidates = [m for m in self.gateway.history(channel_id, after=cursor)
                          if int(m.id) <= int(binding.replay_hwm)]
            parsed = [(m, parse_machine_line(m.content)) for m in candidates]
            for m, ml in parsed:
                if ml is not None:
                    self._index_audience(m, ml)       # every line zero parsed, not just mine
            mine = []
            for m, ml in parsed:
                position = None if ml is None else self._position(ml, binding, m)
                if position is not None:
                    mine.append((m, ml, position))
            with self._lock:
                rows = set(self.queue.get(q, {}))
                for m, ml, _ in mine:
                    if (_flagged(ml, q) and m.id not in rows
                            and m.id not in binding.delivered):
                        self._queue_add(q, m.id, _epoch(m.timestamp))
                mine = [(m, ml, p) for m, ml, p in mine
                        if m.id not in rows and not _flagged(ml, q)]
            if cursor is None:
                # Bounded replay is for an ABSENT cursor only: a first bind, or a
                # cursor lost with STATE. It bounds mail with no row; queued mail is
                # never bounded. A present cursor, however old, replays all that is
                # past it. The bound is the `[replay]` setting, read at use.
                replay = self.settings.use("replay")
                bounded = mine[-replay.messages:]
                now = self.clock()
                bounded = [(m, ml, p) for (m, ml, p) in bounded
                           if now - _epoch(m.timestamp) <= replay.seconds]
                truncated = len(bounded) < len(mine)
                mine = bounded
            # The read, once per register, and only when something would be handed.
            with self._lock:
                fresh = [(m, ml, p) for m, ml, p in mine if m.id not in binding.delivered]
            if fresh:
                clock, now = self._read_memo(binding, memo)
                with self._lock:
                    for m, ml, p in fresh:
                        if not _passes(ml, p, clock, now):
                            self._queue_add(q, m.id, _epoch(m.timestamp))
                    gated = {m.id for m, ml, p in fresh if not _passes(ml, p, clock, now)}
                if gated:
                    log.info("replay to %s queued %d message(s): cache %s", q, len(gated),
                             self._describe(clock))
                mine = [(m, ml, p) for m, ml, p in mine if m.id not in gated]
        handed = []
        for m, ml, _ in mine:
            handed.append(m)
            if not self._claim(binding, m.id):
                continue          # `holding`, or live routing got there first
            with self._name_lock(q):
                self._deliver(binding, m, ml, self.attribute(m, live=False), backlog=True)
        self._end_pass(memo)
        # The highest id replay handed (or found the adapter already holding): never a
        # queued id, so the adapter's cursor is never carried past queued mail.
        high = max((m.id for m in handed), key=int, default=cursor)
        frame = {"op": "replay_complete", "identity": q, "cursor": high,
                 "truncated": truncated}
        if truncated:
            frame["notice"] = constants.replay_notice(replay.messages, replay.seconds)
        with contextlib.suppress(Exception):
            binding.conn.send(frame)

    def _position(self, ml, binding: Binding, msg) -> int | None:
        """The position on line zero of the first tag that names this binding's
        identity, or None when none does. The send's cold-at value at that position is
        this recipient's."""
        for position, tag in enumerate(ml.recipients):
            r = self.resolve(tag)
            if r.resolvable and r.qualified == binding.qualified:
                return position
        return None

    # ------------------------------------------------------------------ retention
    def trim_record(self) -> None:
        """Drop what the record has kept past `retain_days` (local backend only).

        The daemon is the one process that runs all day, so it is where "at boot and once a
        day" lives. A backend with no `trim` (the mock) has nothing to do here. A
        failure is logged and nothing else: retention is housekeeping, and a daemon that
        cannot housekeep is still a daemon that delivers.
        """
        # Stamped whatever the backend is, so a daemon on the mock asks
        # once a day and not once a trim tick.
        self._last_trim = self.clock()
        self._trim_handed()
        trim = getattr(self.gateway, "trim", None)
        if trim is None:
            return
        if not self._queue_loaded:
            # The trim keeps what the queue holds, and the queue has not been loaded in
            # this daemon's life, so a queued message past retention would go with the
            # rest. Skipped until it is; the record only grows meanwhile.
            log.warning("record trim skipped: the queue has not been loaded yet")
            return
        try:
            # Queued mail stays past `retain_days` until it is handled: no age limit.
            deleted = trim(keep=self.queued_ids())
        except Exception:
            log.exception("record trim failed; the record keeps what it has")
            return
        if deleted:
            log.info("record trim removed %d message(s) past retention", deleted)

    # ------------------------------------------------------------------ the trim loop
    def _trim_loop(self) -> None:
        """The record trim's clock: the daily `TRIM_INTERVAL` check, every `TRIM_TICK`."""
        while not self._stop.wait(TRIM_TICK):
            if self.clock() - self._last_trim >= TRIM_INTERVAL:
                self.trim_record()

    def classify(self, qualified: str, message_id: str) -> str:
        """`moxi comms explain`'s case for one message handed to one name, on demand."""
        cursor = cursors.read(qualified)
        if cursor is not None and int(cursor) >= int(message_id):
            return CASE_HANDLED
        with self._lock:
            bound = qualified in self.bindings
            injected = self.injected_through.get(qualified)
        if not bound:
            return CASE_NOT_ADDRESSABLE
        if injected is not None and int(injected) >= int(message_id):
            return CASE_TURN_INCOMPLETE
        return CASE_IGNORED


class DaemonStopping(Exception):
    """A register that arrived after `stop()` froze the keep mark.

    Not a refusal and not an accident: the daemon is going away. `_handle` answers it by
    closing the connection with no reply, which the client reads as `DaemonOffline`, and
    every adapter already treats that as "the daemon is not there" and retries. A named
    exception rather than a bare close, so `_handle` catches it quietly: without it the
    reply to a closed connection raised `ProtocolError`, and the accident arm logged a
    traceback for a stop working as intended.
    """


class Refusal(Exception):
    """A refusal that names the holder where there is one."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


def _is_numbered(requested) -> bool:
    """Did a register ask for a slot by number? `next-free` and None did not."""
    return not (requested == "next-free" or requested is None)


def _asked(frame: dict) -> str:
    """What a register asked for, for the refused-register journal line."""
    name = frame.get("name") or "auto"
    scope = GLOBAL if frame.get("global") else (frame.get("scope") or GLOBAL)
    return (f"name={name} scope={scope} slot={frame.get('slot', 'next-free')} "
            f"adapter={frame.get('adapter') or 'claude-shim'}")


def _parse_keep_entry(entry):
    """One keep-mark entry as (kind, slot, qualified, expires), or why it is dropped.

    `expires` is None for a `live` entry. For a `keep` entry it must be a finite number:
    Python's JSON reader takes `NaN` and `Infinity`, and either would make a keep that
    never ends.
    """
    if not isinstance(entry, dict):
        return "not an object"
    kind, slot, q = entry.get("kind"), entry.get("slot"), entry.get("qualified")
    if kind not in ("live", "keep"):
        return "unknown kind"
    if not isinstance(slot, int) or isinstance(slot, bool):
        return "slot is not a number"
    if not isinstance(q, str) or not q:
        return "no name"
    if is_auto_name(split_tag(q)[0]):
        return "an agent-N name is never kept"
    if kind == "live":
        return kind, slot, q, None
    expires = entry.get("expires")
    if (not isinstance(expires, (int, float)) or isinstance(expires, bool)
            or not math.isfinite(expires)):
        return "expiry is not a finite number"
    return kind, slot, q, float(expires)


def _as_id(message_id: str) -> int:
    """A message id as a number, for ordering. Anything unreadable sorts first."""
    try:
        return int(message_id)
    except (TypeError, ValueError):
        return -1



def _flagged(ml, qualified: str) -> bool:
    """Does line zero's `[held]` (or old `[next-turn]`) hold this message for
    `qualified`? For every name but the operator's, whom the flag never holds."""
    return bool(ml.held) and qualified != OPERATOR_NAME


def _passes(ml, position: int, clock, now) -> bool:
    """Does a live hand-over of this message pass on this read? A known read decides
    alone: `warm`, `fresh` and `no-cache` pass, `cold` does not. On an `unknown` read the
    send's reading decides, from the `[cold-at:…]` value at the recipient's position on
    line zero: `-` passes, a time passes while the daemon's clock is still before it,
    and a line with no token (posted before the queue, or not by the send verb)
    carries no reading and does not pass."""
    if clock is None:
        return False
    if clock.kind in cache.PASSING:
        return True
    if clock.kind != cache.UNKNOWN:
        return False
    cold_at = getattr(ml, "cold_at", None)
    if not cold_at or position >= len(cold_at):
        return False
    value = cold_at[position]
    if value == COLD_AT_LIVE:
        return True
    return isinstance(value, int) and now is not None and now < value


def _epoch(timestamp: str) -> float:
    try:
        dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:  # pragma: no cover - exercised by the CLI smoke run
    import argparse

    from .gateway import BACKENDS, LOCAL, MOCK, GatewayError, backend_choice, open_gateway

    from . import provision, tokens

    ap = argparse.ArgumentParser(prog="moxi-comms-daemon")
    ap.add_argument("--gateway", choices=BACKENDS,
                    help="which backend to hold: local or mock; default: "
                         "MOXI_COMMS_GATEWAY, then the config file, then local")
    ap.add_argument("--gateway-store", help="JSON store of the mock gateway")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.gateway_store:
        os.environ["MOXI_COMMS_MOCK_GATEWAY"] = args.gateway_store
    # The flag may name a backend the box's config does not, and every adapter resolves the
    # backend WITHOUT a flag. A daemon holding one record while the shims and the CLI write
    # another is a fleet that looks connected and delivers nothing, so the disagreement is
    # a refusal to start. The exception is `mock` with a scratch store, which is the test
    # and by-hand case.
    # The settings first: a config file that does not load refuses the start. After
    # this the daemon reads them at use and keeps the last good ones.
    settings = config.Config(report=_config_kept)
    try:
        settings.start()
    except config.ConfigError as e:
        raise SystemExit(str(e)) from None
    try:
        choice = backend_choice(None, env=os.environ)
    except GatewayError as e:
        raise SystemExit(str(e)) from None
    resolved = choice.name
    if args.gateway and args.gateway != resolved and not (
            args.gateway == MOCK and args.gateway_store):
        raise SystemExit(
            f"--gateway {args.gateway} disagrees with this box's configured backend "
            f"({resolved}). Every adapter and the CLI resolve the backend from the "
            f"config and the environment, with no flag, so a daemon started this way "
            f"would hold a record nothing else writes. Change `backend` in comms' config "
            f"file (or MOXI_COMMS_GATEWAY) if the flip is what you meant, or drop the flag.")
    try:
        gateway = open_gateway(args.gateway, role="reader")
    except GatewayError as e:
        raise SystemExit(str(e)) from None
    # systemd stops the unit with SIGTERM. Without a handler the process died before the
    # `finally`, every restart left its custody intervals open for the next boot's
    # fence, and the fence's warning could not tell a crash from a restart. Installed
    # before `start()` binds the socket, so no stop can land in a window the handler
    # does not cover.
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.set())
    # A daemon on the local backend provisions an empty directory itself, so an install
    # that names nothing ends with a working daemon; a directory that holds slots is
    # left alone (`provision.first_start`). The mock keeps its own record. Built HERE,
    # in `main`: a Daemon constructed by a test, a smoke script or an embedding caller
    # provisions nothing.
    first_start = (provision.first_start(tokens.TokenStore(None, env=os.environ))
                   if (args.gateway or resolved) == LOCAL else None)
    daemon = Daemon(gateway, first_start=first_start, settings=settings)
    try:
        daemon.start()
    except RuntimeError as e:
        # A refusal is an answer, not a crash: the operator gets the recovery sentence
        # and systemd gets a clean nonzero exit, and the gateway connection this process
        # opened does not leak past the `finally` it never reaches.
        with contextlib.suppress(Exception):
            gateway.close()
        raise SystemExit(str(e)) from None
    log.info("comms daemon listening on %s", daemon.socket_path)
    try:
        stopping.wait()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        with contextlib.suppress(Exception):
            gateway.close()          # the process owns the socket it opened
    return 0


__all__ = ["Daemon", "Refusal", "Author", "Binding", "TRIM_INTERVAL", "TRIM_TICK",
           "CASE_HANDLED", "CASE_IGNORED", "CASE_NOT_ADDRESSABLE", "CASE_TURN_INCOMPLETE",
           "CASE_REASONS"]
