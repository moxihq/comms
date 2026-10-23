"""The skeleton every daemon-socket adapter shares, and the hooks its port fills in.

An adapter binds one name on the daemon socket, puts that name's mail in front of a
model at a turn boundary, and writes the name's cursor. Most of that is the same in
every adapter: the daemon binding and its liveness, the queue and the replay hold, the
one flush path, supervision and the daemon notifications. That is this class. What is a
harness's own -- how a digest gets into its session, which outcomes put a batch back,
and how the harness says a turn finished -- is the port, a subclass that fills in the
hooks below. `adapters/codex` and `adapters/opencode` are the ports today.

The class runs in exactly the order the adapters ran before it existed, and each place
where they differed is a flag or a hook here, so moving onto it changed no behavior.

**When a batch goes back.** The flush path puts a batch back on the queue only when the
port's `deliver` returns `REQUEUE`, and a port returns it only for an outcome after which
an immediate retry cannot put the same digest into a live turn it already reached. Any
other value is a receipt, and a receipt is a delivery: the digest is recorded and the
receipt is handed to `delivered`. The base tests `receipt is REQUEUE` and never the
receipt's truth, so a receipt of None is a delivery. Whether the cursor ever moves for
that digest is the port's call, on its harness's own evidence that the turn completed.
"""

from __future__ import annotations

import itertools
import logging
import os
import signal
import sys
import threading
import time

from .. import claimfile, constants, cursors
from .. import owed as owed_mod
from ..claimhold import CLAIM_SOCKET_ENV, ClaimHolder
from ..client import DaemonClient, DaemonError, DaemonOffline, backoff_delays
from ..config import ConfigError
from ..membership import DEFAULT_PROC, TREE, Root
from .claude_shim import OFFLINE_MESSAGE, render_digest, survive_supervisor_error

#: The one value of `deliver` that puts a batch back on the queue.
REQUEUE = object()

#: How long a port that doesn't answer `retry_delay` waits after a `REQUEUE`.
FLUSH_RETRY = 1.0
#: How long `reregister` waits for the daemon to drop the registration it replaces.
REREGISTER_WAIT = 5.0


class DroppedAfterAnswer(DaemonOffline):
    """The daemon answered a register and the connection closed before the adapter took
    the binding: nothing is bound, and the supervisor registers again."""


#: The one turn status that moves a cursor, in what a port hands `TurnLedger.completed`.
TURN_COMPLETED = "completed"

#: The `kind` of an `activity` frame: `ready` at a session start, clear,
#: compaction or resume, always with its `source`; `turn` at every turn end of the
#: agent, with `handled` and `unhandled`. A report refresh is `activity` with no kind.
ACTIVITY_READY, ACTIVITY_TURN = "ready", "turn"
SOURCE_STARTUP, SOURCE_RESUME, SOURCE_CLEAR, SOURCE_COMPACT = (
    "startup", "resume", "clear", "compact")


def launch_stamp() -> str:
    """`<pid>@<UTC start>`: which launcher process wrote a thread record or session
    file. A launch is one launcher process, so this names the launch."""
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return f"{os.getpid()}@{started}"


class TurnLedger:
    """Pending injections, keyed `turnId -> every message id of the digests in it`.

    `claude-shim`'s single `awaiting_completion` string does not generalise: steering N
    digests into one live turn yields ONE completion covering all of them, and a resumed
    connection also sees completions for turns this adapter never injected into.

    A port keys it by whatever id its CLI gives the turn or the prompt the digest went
    into, and hands `completed` (or `end`) a dict with that `id` and a `status`, which is
    `TURN_COMPLETED` only when the CLI's own evidence says the turn finished. Each turn
    keeps the ids of every digest steered or started into it, not only the highest, so
    when the turn ends the adapter knows exactly which ids it held for it.
    """

    def __init__(self):
        self._pending: dict[str, list[str]] = {}

    def injected(self, turn_id: str | None, message_id, ids=()) -> None:
        """Record a digest injected into `turn_id`: its highest message id and every id
        it carried (`ids`)."""
        if not turn_id:
            # An injection whose turn id could not be established is deliberately not
            # tracked: an untracked digest never advances a cursor, so it replays.
            return
        kept = self._pending.setdefault(turn_id, [])
        for mid in (*ids, message_id):
            if str(mid) not in kept:
                kept.append(str(mid))

    def pending(self, turn_id: str) -> str | None:
        """The highest message id injected into `turn_id`, or None."""
        kept = self._pending.get(turn_id)
        return max(kept, key=int) if kept else None

    def ids(self, turn_id: str) -> tuple:
        """Every message id injected into `turn_id`, in the order they went in."""
        return tuple(self._pending.get(turn_id, ()))

    @property
    def turns(self) -> tuple:
        return tuple(self._pending)

    def tracks(self, turn_id: str | None) -> bool:
        return turn_id in self._pending

    def end(self, turn: dict) -> tuple:
        """A turn ended: (the message id to advance to or None, every id it carried).

        The id to advance to is None for all three of: a turn nobody injected into (the
        operator's TUI turn, whose ids are empty too), an `interrupted` turn, and a
        `failed` turn. The last two take the safe direction -- no advance, and the
        digest replays on the next registration."""
        turn_id = turn.get("id")
        if turn_id not in self._pending:
            return None, ()
        kept = tuple(self._pending.pop(turn_id))
        through = max(kept, key=int) if turn.get("status") == TURN_COMPLETED else None
        return through, kept

    def completed(self, turn: dict) -> str | None:
        """A `turn/completed` arrived. Returns the message id to advance to, or None
        (`end`'s first half)."""
        return self.end(turn)[0]


#: Which harness adapter this is in its process: the first is keyed by its pid and
#: start time alone, as a launcher always is; a test that builds several gets suffixes.
_HOLDERS = itertools.count()


class HarnessAdapter:
    """One identity's daemon binding, queue, flush path and supervision."""

    # ---------------------------------------------------------------- set by the port
    #: The daemon kind this adapter registers as.
    KIND: str = ""
    #: What the port keys by the qualified name besides the cursor, for the bind error.
    STATE_NOUN: str = "state file"
    #: One delivery in flight per identity: mail that arrives during one queues, and the
    #: next flush coalesces it.
    ONE_AT_A_TIME: bool = False
    #: Say nothing when the daemon connection closes because `stop()` closed it.
    QUIET_OFFLINE_ON_STOP: bool = False
    #: The port reports each digest's turn end itself (`release_held` with the ids its
    #: `TurnLedger` kept), so a delivered digest's ids stay held until then. A port that
    #: reports none has finished the digest's turn when `deliver` returns.
    HOLDS_UNTIL_TURN_ENDS: bool = False
    #: The port's own logger.
    log: logging.Logger = logging.getLogger("comms.harness")

    def __init__(self, agent, *, socket_path=None, autoreconnect: bool = True,
                 backoff=None, status_sink=None, clock=time.time, exit_hook=None):
        # Reads `agent.directors` (at render) and nothing else of the agent here.
        self.agent = agent
        self.clock = clock                 # only the catch-up header reads it
        self.socket_path = socket_path     # the DAEMON socket
        self.autoreconnect = autoreconnect
        self.status_sink = status_sink or (lambda text: self.log.warning("%s", text))
        self._backoff = backoff

        # identity / daemon
        self.identity: str | None = None
        self.tier: str | None = None
        self.slot_id: int | None = None
        self.offline = True
        self.replaying = False
        self.replay_notice: str | None = None
        self.queue: list = []
        #: Ids queued or injected whose digest's turn has not ended. A daemon restart
        #: replays from the cursor, which has moved past neither, so without this the
        #: same message arrives twice. Listed as `holding` at every register; it loses
        #: exactly an ended digest's ids, never by comparing ids with the cursor.
        self.held_ids: set[str] = set()
        #: The ids of each digest the flush is handing the port, by its high id
        #: (`digest_ids`), while `deliver` runs.
        self._digests: dict[str, tuple] = {}
        #: The payloads of the same digest, by its high id, while `deliver` runs
        #: (`digest_batch`): a port that may have to put a digest back in the queue
        #: later (OpenCode's follow of a window /new) keeps them.
        self._batches: dict[str, list] = {}
        #: Ids whose digest's turn ended without completing, or went into a turn the
        #: port could not identify, and that the next `turn` frame names `unhandled`.
        #: They stay held until that frame is sent (the clamp).
        self.unhandled_owed: list[str] = []
        #: The highest `through` of every completed digest: the cursor's high-water
        #: mark. The cursor advances to the lower of this and one below the lowest id
        #: still held.
        self.high_water: int | None = None
        #: A `ready` whose moment came while the adapter was not bound: its `source`,
        #: sent once right after the next register's answer.
        self._owed_ready: str | None = None
        self.injected: list = []           # digest texts, for the tests and the smoke

        self._daemon: DaemonClient | None = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        #: The exception type the supervisor last reported, so it says each once.
        self._supervisor_error: str | None = None
        self._running = False              # the ONE_AT_A_TIME mark
        self._threads: list = []
        #: The start attempts' record reads (`read_for_register`, `read_for_attempt`).
        self._attempt_no = 0
        self._attempt: tuple | None = None        # (attempt number, read)
        self._registered: tuple | None = None     # the read the last register reported
        self._pinned_read: tuple | None = None    # what a re-register must report
        #: (identity, hold) of each injection, by the high message id it carried: a
        #: cursor moves only in the hold its digest was injected under.
        self.marks: dict = {}
        #: Reply enforcement, as the Claude shim keeps it: the ledger of owed replies,
        #: opened when `deliver` returns a receipt (the model has the digest); this
        #: adapter's own reminders in the running turn (0 or 1); whether a reminder
        #: holds the turn now. `owed_query(identity, authors, referents)` is the
        #: daemon's `owed` query on a short connection of its own; a test may answer it.
        self.reply_ledger = owed_mod.Ledger()
        self.turn_blocks = 0
        self.blocked = False
        self.owed_query = self._query_owed
        self.exit_hook = exit_hook or (lambda: os.kill(os.getpid(), signal.SIGTERM))
        self._make_holder()

    # ------------------------------------------------------------------ the claim
    def _make_holder(self) -> None:
        """This adapter is its session's top process and its claim holder. Its claim
        file is keyed by its pid and that pid's start time, so a reused pid never meets
        a dead adapter's file as its own; its claim socket is
        RUNTIME/comms-adapter-<pid>.sock, stamped into everything it spawns."""
        from ..paths import runtime_root
        me = os.getpid()
        start = DEFAULT_PROC.start_time(me) or "0"
        n = next(_HOLDERS)
        suffix = "" if n == 0 else f"-{n}"
        self.claims = ClaimHolder(
            kind=self.KIND, key=f"{me}-{start}{suffix}",
            socket_path=runtime_root() / f"comms-adapter-{me}{suffix}.sock",
            lock=self._lock, roots=[Root(me, start, TREE)], keying="pid")
        agent = self.agent
        name, scope = getattr(agent, "name", None), getattr(agent, "scope", None)
        claim = {"name": name, "scope": scope, "global": scope == "global",
                 "qualified": getattr(agent, "qualified", None),
                 "slot": getattr(agent, "slot", None), "tier": None,
                 "directors": list(getattr(agent, "directors", []) or [])}
        self._launch_claim = claim
        # Held in memory from construction; the file is written at `start`.
        self.claims.claim = dict(claim)
        self.claims.hold = 1

    def claim_env(self) -> dict:
        """What every process this adapter spawns carries: its claim socket."""
        return {CLAIM_SOCKET_ENV: str(self.claims.socket_path)}

    def _claim_start(self) -> None:
        claimfile.sweep_dead_harness_files(self.claims.proc)
        self.claims.start_launch(self._launch_claim)
        self.claims.listen()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Bind, bring the port up, then serve. A launch into a name another session
        holds waits for it (and cannot send while it waits); any other refusal is fatal:
        the identity is not ours to hold."""
        self._claim_start()
        try:
            with self.claims.register_lock:
                self.connect_daemon()
        except DaemonError as e:
            response = getattr(e, "response", None) or {}
            holder = (response.get("holder") or {}).get("qualified")
            if not (self.autoreconnect and response.get("refused")
                    and not response.get("transient") and holder == self.agent.qualified):
                raise
            self.claims.name_refused(holder)
            self.status_sink(f"comms unbound: {self.agent.qualified} is held by another "
                             "session; this one waits for it and cannot send meanwhile.")
        except DroppedAfterAnswer:
            # The daemon was up a moment ago and dropped the connection under the first
            # register: the supervisor registers again, as after any later drop. A
            # daemon that never answered is still the launch's failure, as before.
            if not self.autoreconnect:
                raise
            self.status_sink(OFFLINE_MESSAGE)
        self.start_port()
        self._spawn_thread(self._flush_loop)
        if self.autoreconnect:
            self._spawn_thread(self._supervise)
        return self

    def _spawn_thread(self, target) -> None:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        self._threads.append(t)

    def connect_daemon(self) -> None:
        """Register with the PINNED slot. The connection is the liveness anchor."""
        with self._lock:
            self.replaying = True          # armed before the round-trip, as the shim does
            # `holding`: the ids this adapter already has queued or injected and whose
            # turn has not ended. The daemon claims them for the new binding at once, so
            # a reconnect's replay from the unmoved cursor never hands them again.
            holding = sorted(self.held_ids, key=int)
        client = DaemonClient(self.socket_path, on_push=self._on_push,
                              on_close=self._on_daemon_close)
        client.connect()
        try:
            resp = client.register(slot=self.agent.slot, name=self.agent.name,
                                   scope=self.agent.scope,
                                   global_=self.agent.scope == "global",
                                   project=self.agent.scope, adapter=self.KIND,
                                   holding=holding,
                                   report=self.session_report(register=True))
        except BaseException:
            client.close()                 # a refused claim never leaves a connection
            raise
        qualified = resp["identity"]["qualified"]
        if qualified != self.agent.qualified:
            client.close()
            raise ConfigError(
                f"the daemon bound this registration as {qualified!r} but the roster "
                f"calls it {self.agent.qualified!r}. The {self.STATE_NOUN} and the cursor "
                "are both keyed by the qualified name, so a mismatch would split the "
                "agent in two -- fix the roster key or its scope.")
        with self._lock:
            if self._stop.is_set():
                # A bind must not outlive `stop()`, as the Claude shim declines it: the
                # daemon would hold it until the process exits, and `bound` would write
                # back the claim file `stop()` removed.
                client.close()
                raise DroppedAfterAnswer("comms offline: the adapter stopped while the "
                                         "daemon answered register")
            if not client.connected:
                # The adapter reads bound only while the connection its register was
                # answered on is open; a drop at any point after the answer sends it
                # back to register. The daemon answered and the connection
                # closed before this thread got here: `_on_daemon_close` has already
                # run (the reader thread marks the client closed before calling it)
                # and saw `offline` still True, so nothing will run again for this
                # client. Taking the binding now would leave `offline` False over a
                # dead client, and the supervisor, which registers only while it is
                # True, would never register again. Under `_lock`, which the close
                # handler takes too, the ordering is total: the close landed first and
                # this declines, or the handler runs after the assignment below and
                # reads the binding offline.
                client.close()
                raise DroppedAfterAnswer(
                    "comms offline: the daemon closed the connection after answering "
                    "register")
            self._daemon = client
            self.identity = qualified
            self.tier = resp["identity"]["tier"]
            self.slot_id = resp["slot"]
            self.offline = False
            if self.reply_ledger.hwm is None and resp.get("hwm") is not None:
                # The first registration's catch-up boundary: a `re:` at or below it
                # was sent before this adapter could see payments.
                self.reply_ledger.hwm = str(resp["hwm"])
            self.claims.bound(resp)
        self.log.info("registered as %s (tier %s, slot %s, adapter %s%s)",
                      self.identity, self.tier, self.slot_id, self.KIND,
                      self.registered_detail())
        with self._lock:
            owed, self._owed_ready = self._owed_ready, None
        if owed:
            self.report_ready(owed)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.stop_before_unbind()
        with self._lock:                   # after a bind that saw no stop, or none
            daemon = self._daemon
        if daemon is not None:
            daemon.close()                 # closing this IS the deregistration
        self.stop_after_unbind()
        self.claims.close()

    # ------------------------------------------------------------------ daemon side
    def _on_push(self, frame: dict) -> None:
        op = frame.get("op")
        if op == "deliver":
            payload = frame["payload"]
            with self._lock:
                if not self.claims.holds:
                    return     # nothing is injected addressed to a claim not held
                mid = str(payload["message_id"])
                if mid in self.held_ids:
                    return     # queued or injected already: a replay handed it again
                self.held_ids.add(mid)
                self.queue.append(payload)
            self._wake.set()
        elif op == "replay_complete":
            with self._lock:
                self.replaying = False
                if frame.get("truncated"):
                    self.replay_notice = frame.get("notice")
            self.log.info("replay complete for %s at cursor %s", frame.get("identity"),
                          frame.get("cursor"))
            self._wake.set()

    def _on_daemon_close(self) -> None:
        with self._lock:
            was_online = not self.offline
            self.offline = True
            released = self.claims.state == claimfile.STANDBY
        if released:
            return                         # standby: the name went elsewhere; nothing to say
        self.claims.went_offline()
        if not was_online:
            return
        if self.QUIET_OFFLINE_ON_STOP and self._stop.is_set():
            return                         # the adapter's own stop closed it
        self.status_sink(OFFLINE_MESSAGE)

    def _supervise(self) -> None:
        """Reconnect to the daemon forever, then give the port its turn.

        Any exception the daemon arm below does not name, and anything the port's turn
        raises, is logged with its traceback, said once per exception type, and the loop
        carries on at its next backoff, where a fixed tuple used to end the thread."""
        delays = self._backoff or backoff_delays()
        while not self._stop.is_set():
            try:
                if self.offline and self.claims.may_register():
                    if not self.claims.register_lock.acquire(blocking=False):
                        self._stop.wait(0.05)
                        continue
                    try:
                        if not self.claims.may_register():
                            continue
                        self.connect_daemon()
                        delays = self._backoff or backoff_delays()
                        self._supervisor_error = None
                        self.status_sink("comms online: reconnected to the delivery "
                                         "daemon.")
                    except DaemonError as e:
                        response = getattr(e, "response", None) or {}
                        holder = (response.get("holder") or {}).get("qualified")
                        if (response.get("refused") and not response.get("transient")
                                and holder == self.agent.qualified):
                            # A name held by another connection: a claim this adapter
                            # has held is gone for good (standby); a launch waits.
                            self.claims.name_refused(holder)
                        self.log.warning("daemon reconnect failed: %s", e)
                        self._stop.wait(next(delays))
                        continue
                    except (DaemonOffline, ConfigError) as e:
                        # DaemonError included: a refused or transiently failed
                        # re-register must not escape this thread and end supervision.
                        self.log.warning("daemon reconnect failed: %s", e)
                        self._stop.wait(next(delays))
                        continue
                    finally:
                        self.claims.register_lock.release()
                if not self.supervise_port():
                    continue                # the port waited out its own backoff
                self._stop.wait(0.05)
            except Exception as e:                          # noqa: BLE001 -- see docstring
                delays = survive_supervisor_error(self, e, delays, self.log)

    def notify_injected(self, identity, through, digest) -> None:
        """Tell the daemon a digest went in, with its text."""
        daemon = self._daemon
        if daemon is not None:
            daemon.notify("injected", identity=identity, through=str(through),
                          digest=digest)

    def notify_activity(self, identity, kind: str | None = None, **fields) -> bool:
        """Tell the daemon the agent did something, with the port's session report.
        With no `kind` it is a report refresh or a turn start and releases nothing;
        `kind` and its fields say what happened (`ready` with `source`, `turn` with
        `handled` and `unhandled`). True when the frame went out on a connection."""
        daemon = self._daemon
        if daemon is None or not daemon.connected:
            return False
        extra = {"kind": kind} if kind else {}
        daemon.notify("activity", identity=identity, **extra, **fields,
                      **self.session_report(register=False))
        return True

    def reregister(self) -> None:
        """Replace this registration, because what `register` reported is stale: close
        the connection, drop the queue (a dropped batch replays at the next
        registration, and the cursor has not moved past it), and register again, so the
        daemon gates the backlog on what is true now. The ids injected and not yet ended
        stay held, and the register lists them as `holding`, so the replay does not hand
        them in again. Raises what `connect_daemon` raises once the daemon has had
        `REREGISTER_WAIT` seconds to drop the old one."""
        with self._lock:
            old, self._daemon = self._daemon, None
            self.offline = True              # the close below says nothing
        if old is not None:
            old.close()
        with self._lock:
            self.held_ids.difference_update(str(p["message_id"]) for p in self.queue)
            self.queue = []
            self.replay_notice = None
        deadline = time.monotonic() + REREGISTER_WAIT
        while True:
            try:
                self.connect_daemon()
                return
            except DaemonError as e:
                if "already bound" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    # ------------------------------------------------------------------ start attempts
    def record_read(self) -> tuple:
        """The port's one read of its thread record or session file: (none | recorded |
        unreadable, the id or None).

        Kept for a port whose launch reads its record. Neither the codex nor the
        opencode launcher calls this, `read_for_register` or `read_for_attempt`: each
        launch creates its own thread or session and reads no id from the record, as a
        Claude launch starts a new session."""
        raise NotImplementedError

    def begin_attempt(self) -> None:
        """A run of the port's start path begins."""
        self._attempt_no += 1

    def read_for_register(self) -> tuple:
        """The read `register` reports, made for the attempt that follows it."""
        read = self._pinned_read if self._pinned_read is not None else self.record_read()
        self._attempt = (self._attempt_no + 1, read)
        self._registered = read
        return read

    def held_for_register(self, outcome_recorded, session_id) -> None:
        """`register` reported an id the adapter already holds."""
        self._registered = (outcome_recorded, session_id)

    def read_for_attempt(self) -> tuple:
        """The current attempt's one read of the record: the one `register` made for
        it, else a new one. When it differs from what `register` reported, the adapter
        registers again before it acts, so the daemon gates the backlog on what is
        true now and mail handed on the stale report is dropped, never injected."""
        attempt, self._attempt = self._attempt, None
        if attempt is not None and attempt[0] == self._attempt_no:
            read = attempt[1]
        else:
            read = self.record_read()
        if self._registered is not None and read != self._registered:
            self.log.info("the record for %s changed since it registered (%s, now %s); "
                          "registering again before anything is resumed, loaded or "
                          "started", self.agent.qualified, self._registered[0], read[0])
            self._pinned_read = read
            try:
                self.reregister()
            finally:
                self._pinned_read = None
                self._attempt = None
        return read

    def report_ready(self, source: str) -> None:
        """A session start, clear, compaction or resume (`source`): `ready` with that
        source, once. One that comes while the adapter is not bound is sent right after
        the next register's answer."""
        with self._lock:
            identity = self.identity if not self.offline else None
            if not identity:
                self._owed_ready = source
                return
        if not self.notify_activity(identity, kind=ACTIVITY_READY, source=source):
            with self._lock:
                self._owed_ready = source

    def report_refresh(self) -> None:
        """The session report changed (a first turn ended, a server restarted): an
        `activity` with no kind, which releases nothing."""
        with self._lock:
            identity = self.identity
        if identity and not self.offline:
            self.notify_activity(identity)

    def report_turn_start(self) -> None:
        """A turn started, whoever started it: `activity` with no kind."""
        self.report_refresh()

    # ------------------------------------------------------------------ turn ends
    def owe_unhandled(self, ids) -> None:
        """Ids the next `turn` frame names `unhandled`: a digest whose turn ended
        without completing and was not reported at once, or one that went into a turn
        the port could not identify. They stay held until that frame is sent."""
        with self._lock:
            for mid in ids:
                if str(mid) not in self.unhandled_owed:
                    self.unhandled_owed.append(str(mid))

    def report_turn_end(self, handled=(), unhandled=()) -> None:
        """A turn of the agent ended: the ids of the digests it
        completed (`handled`) and of those it ended without completing (`unhandled`).

        In this order: the handled ids stop being held; the cursor advances, never
        past an id still held; `turn` goes out with both lists, the owed `unhandled` ids included;
        only then do the unhandled ids stop being held, since until the frame is sent
        they have no row and the cursor must not pass them. A frame that cannot be sent
        (no daemon) leaves the unhandled ids owed and held for the next one."""
        handled = [str(m) for m in handled]
        with self._lock:
            unhandled = list(dict.fromkeys(
                [*self.unhandled_owed, *(str(m) for m in unhandled)]))
            self.unhandled_owed = []
            identity = self.identity
            self.held_ids.difference_update(handled)
        if handled:
            through = max(handled, key=int)
            for mid in handled:
                if mid != through:
                    self.marks.pop(mid, None)
            if self.may_advance(through):
                with self._lock:
                    if self.high_water is None or int(through) > self.high_water:
                        self.high_water = int(through)
        self.advance_cursor()
        sent = bool(identity) and self.notify_activity(
            identity, kind=ACTIVITY_TURN, handled=handled, unhandled=unhandled)
        if sent:
            self.release_held(unhandled)
        else:
            self.owe_unhandled(unhandled)
        self._wake.set()

    def advance_cursor(self) -> str | None:
        """Move the cursor to the lower of the high-water mark and one below the lowest
        id still held: it never passes an id this adapter holds and has not seen
        in a completed turn. Monotonic; returns the cursor after the write, or None."""
        with self._lock:
            target = self.high_water
            identity = self.identity
            if target is not None and self.held_ids:
                target = min(target, min(int(m) for m in self.held_ids) - 1)
        if target is None or target <= 0 or not identity:
            return None
        cursor = cursors.advance(identity, str(target))
        if cursor is not None:
            self.log.info("cursor for %s advanced to %s", identity, cursor)
        return cursor

    # ------------------------------------------------------------------ reply enforcement
    def reply_check(self, fields: dict) -> owed_mod.Decision:
        """The Claude shim's steps for one turn end, through the same `owed` query: a
        boundary (`block` None) or a block whose reason is `owed.reminder_text`.

        `fields` are the turn end's facts as a Stop hook gives them; the port's
        `stop_facts` supplies what its harness's turn end lacks, and only once a debt
        is open. Every missing or failed fact returns a boundary with every pending
        mark voided; nothing here raises. `dropped` lists the debts counted and dropped
        at this turn end, already recorded with the daemon as `unanswered`. The caller
        delivers the reminder and calls `mark_blocked` once it is in front of the model.
        """
        try:
            decision = self._decide_reply(fields if isinstance(fields, dict) else {})
        except Exception:                                          # noqa: BLE001
            self.log.exception("the reply check failed; this turn end passes")
            with self._lock:
                self.reply_ledger.void_pending()
            return owed_mod.Decision()
        for debt in decision.dropped:
            self._record_unanswered(debt)
        return decision

    def _decide_reply(self, fields: dict) -> owed_mod.Decision:
        now = self.clock()
        with self._lock:
            self.reply_ledger.expire(now)                         # step 1
            if not self.reply_ledger.debts:
                return owed_mod.Decision()
        fields = self.stop_facts(dict(fields))
        with self._lock:
            active = fields.get("stop_hook_active")
            tasks = fields.get("task_types")
            crons = fields.get("crons")
            if (not isinstance(active, bool) or not isinstance(tasks, list)   # step 2
                    or not isinstance(crons, int) or isinstance(crons, bool)):
                self.reply_ledger.void_pending()
                return owed_mod.Decision()
            authors, referents = self.reply_ledger.query_args()
            identity = self.identity
        answer = None
        if identity is not None:                                  # step 3
            try:
                answer = self.owed_query(identity, authors, referents)
            except Exception as e:                                # noqa: BLE001
                self.log.info("the owed query failed (%s); this turn end passes", e)
        if not isinstance(answer, dict):
            with self._lock:
                self.reply_ledger.void_pending()
            return owed_mod.Decision()
        with self._lock:
            self.reply_ledger.settle(answer, now)
            work = owed_mod.is_work(tasks, crons, queued=bool(self.queue),   # step 4
                                    undelivered=answer.get("undelivered"),
                                    awaiting=answer.get("awaiting"))
            return self.reply_ledger.decide(stop_hook_active=active,   # steps 5 to 9
                                            turn_blocks=self.turn_blocks, work=work,
                                            floor=answer.get("floor"), now=now)

    def stop_facts(self, fields: dict) -> dict:
        """The turn end's facts with what the port's harness does not give filled in:
        `stop_hook_active`, `task_types` and `crons`. A fact the port cannot read is
        left out, and the turn ends with no reminder. Called outside the lock, and only
        when a debt is open."""
        return fields

    def _query_owed(self, identity: str, authors: dict, referents: list) -> dict:
        """The daemon's `owed` query on a short connection of its own, with the shim's
        timeout. Raises on any failure."""
        client = DaemonClient(self.socket_path, timeout=constants.OWED_QUERY_TIMEOUT)
        client.connect()
        try:
            return client.query("owed", identity=identity, authors=authors,
                                referents=referents)
        finally:
            client.close()

    def _record_unanswered(self, debt) -> None:
        """An ignored reminder: one `unanswered` on the binding's own connection, as the
        shim sends it."""
        with self._lock:
            identity, client = self.identity, self._daemon
        if identity and client is not None and client.connected:
            client.notify("unanswered", identity=identity, message_id=debt.message_id,
                          author=debt.author, at=debt.at)
        self.log.info("comms: %s ended a turn without answering %s's msg %s", identity,
                      debt.author, debt.message_id)

    def mark_blocked(self, names) -> None:
        """The reminder is in front of the model and holds the turn: the named debts
        are pending, and this turn has had its one reminder."""
        with self._lock:
            self.blocked = True
            self.reply_ledger.mark_pending(names)
            self.turn_blocks += 1

    def clear_block(self) -> None:
        """A turn end, or what resolves a reminder: every pending mark not already
        settled is voided, and the next turn may be reminded again."""
        with self._lock:
            self.reply_ledger.void_pending()
            self.blocked = False
            self.turn_blocks = 0

    def owed_session_changed(self, source: str, session_id) -> None:
        """A clear empties the debts; a resume empties those injected into another
        session. While a reminder holds the turn, a startup, resume or clear resolves
        it, as on the shim; a compaction resolves nothing."""
        with self._lock:
            gone = self.reply_ledger.session_changed(source, session_id)
            blocked = self.blocked
        if gone:
            self.log.info("%s: %d owed repl(ies) left the ledger with the context",
                          source, gone)
        if blocked and source in owed_mod.RESOLVING_SOURCES:
            self.clear_block()

    # ------------------------------------------------------------------ injection
    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.2)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.flush()
            except Exception:      # a bad flush never takes the adapter down with it
                self.log.exception("flush failed for %s", self.agent.qualified)

    def flush(self) -> str | None:
        """ONE flush path. Backlog coalesces into one digest; live messages arrive one
        at a time and take the same path. Nothing is delivered while catch-up is still
        arriving, while the port says it can't take a digest, or, for a port that holds
        one delivery at a time, while a delivery is running.

        An exception out of `deliver` that the port did not turn into `REQUEUE` leaves
        here: the taken batch is dropped and replays at the next registration. A
        `REQUEUE` after this adapter stopped holding the name the batch was injected
        under drops it too: an adapter with no claim injects nothing addressed to one,
        and the daemon has re-pended those ids for the name's next holder."""
        with self._lock:
            if (self.replaying or not self.queue or not self.can_inject()
                    or (self.ONE_AT_A_TIME and self._running)):
                return None
            # A row released from the queue (`held: true`) never goes into a running
            # turn: it waits in the queue, in id order, for the turn's end.
            takes_held = self.may_take_held()
            batch = sorted((p for p in self.queue if takes_held or not p.get("held")),
                           key=lambda p: int(p["message_id"]))
            if not batch:
                return None
            self.queue = [p for p in self.queue if not takes_held and p.get("held")]
            notice, self.replay_notice = self.replay_notice, None
            identity = self.identity
            if self.ONE_AT_A_TIME:
                self._running = True
        ids = tuple(str(p["message_id"]) for p in batch)
        high = max(int(mid) for mid in ids)
        try:
            text = render_digest(batch, notice, self.agent.directors, clock=self.clock)
            mark = self.marks[str(high)] = self.claims.hold_mark()
            self._digests[str(high)] = ids
            self._batches[str(high)] = batch
            receipt = self.deliver(text, high, identity, len(batch))
        except BaseException:
            self.release_held(ids)         # dropped: it replays at the next registration
            raise
        finally:
            self._digests.pop(str(high), None)
            self._batches.pop(str(high), None)
            if self.ONE_AT_A_TIME:
                with self._lock:
                    self._running = False
        if receipt is REQUEUE and not self.claims.may_advance(mark):
            self.marks.pop(str(high), None)
            self.release_held(ids)
            self.log.info("a delivery for %s through %s did not complete and the name has "
                          "been released since; the batch is dropped, not requeued",
                          identity, high)
            return None
        if receipt is REQUEUE:
            with self._lock:               # nothing was delivered: put it back, in order
                self.queue = batch + self.queue
                self.replay_notice = notice or self.replay_notice
            delay = self.retry_delay()
            if delay:
                time.sleep(delay)
            self._wake.set()
            return None
        with self._lock:
            self.injected.append(text)
            # A receipt: the model has the digest, so its owed replies open now. The
            # ledger checks each payload's sender, mark and age.
            now = self.clock()
            directors = tuple(getattr(self.agent, "directors", None) or ())
            session_id = self.mirror_session()
            for p in batch:
                self.reply_ledger.open(p, directors, session_id, now)
        self.delivered(text, high, identity, len(batch), receipt)
        if not self.HOLDS_UNTIL_TURN_ENDS:
            self.release_held(ids)         # the digest's turn ended inside `deliver`
        return text

    # ------------------------------------------------------------------ the held ids
    def digest_ids(self, high) -> tuple:
        """Every message id of the digest whose high id is `high`, while the base's
        `deliver` call for it runs (for the port's `TurnLedger`)."""
        return self._digests.get(str(high), (str(high),))

    def digest_batch(self, high) -> list:
        """The payloads of the digest whose high id is `high`, while `deliver` runs."""
        return list(self._batches.get(str(high), ()))

    def release_held(self, ids) -> None:
        """A digest's turn ended, completed or not, or its batch was dropped: exactly
        its ids stop being held, so a later registration's replay may hand any that the
        cursor has not passed. Never by comparing ids with the cursor: a lower id can
        still be queued when a higher digest's turn ends."""
        with self._lock:
            self.held_ids.difference_update(str(mid) for mid in ids)

    # ------------------------------------------------------------------ the port's hooks
    def may_advance(self, through) -> bool:
        """A cursor moves only for the name its digest was injected under, in the same
        hold, while this adapter still holds it."""
        mark = self.marks.pop(str(through), None)
        if self.claims.may_advance(mark):
            return True
        self.log.info("a turn completed after its name was released; no cursor advance")
        return False

    def mirror_session(self) -> str | None:
        """The session the port runs now ("" for a blank anchor), or None when the
        port cannot say. A delivered digest's owed replies are opened against it."""
        return None

    def start_port(self) -> None:
        """Bring the port up, after the bind and before the threads start."""

    def can_inject(self) -> bool:
        """Whether the port can take a digest now. Called under the lock."""
        return True

    def may_take_held(self) -> bool:
        """Whether a payload released from the queue (`held: true`) may go now: only
        while no turn runs. A port that delivers only into an idle
        session answers True. Called under the lock."""
        return True

    def deliver(self, text: str, high: int, identity, count: int):
        """Put the digest in front of the model. `REQUEUE`, or a receipt."""
        raise NotImplementedError

    def delivered(self, text: str, high: int, identity, count: int, receipt) -> None:
        """After the base recorded a delivered digest."""

    def retry_delay(self) -> float:
        """Seconds to wait after a `REQUEUE` before the next try. Read at call time.

        Every port answers a positive wait outside tests. A set wake runs the next flush
        at once, so a `REQUEUE` with no wait retries as fast as the port can fail."""
        return FLUSH_RETRY

    def supervise_port(self) -> bool:
        """The port's turn in each supervision pass. False when it already waited out
        its own backoff, so the pass starts again at once."""
        return True

    def stop_before_unbind(self) -> None:
        """Called by `stop()` before the daemon connection closes."""

    def stop_after_unbind(self) -> None:
        """Called by `stop()` after the daemon connection closes."""

    def registered_detail(self) -> str:
        """Appended inside the parentheses of the "registered as" log line."""
        return ""

    def session_report(self, *, register: bool) -> dict:
        """The port's evidence for the daemon's cache clock, sent with `register`
        (`register=True`) and every `activity`: the session or thread id, the root its
        harness keeps that record under (`cache_source`), whether a turn of it has
        ended (`turned`), and, on `register` only, `new_session`. A key left out says
        nothing; the daemon keeps what it had."""
        return {}


__all__ = ["ACTIVITY_READY", "ACTIVITY_TURN", "FLUSH_RETRY", "REREGISTER_WAIT", "REQUEUE",
           "SOURCE_CLEAR", "SOURCE_COMPACT", "SOURCE_RESUME", "SOURCE_STARTUP",
           "TURN_COMPLETED", "HarnessAdapter", "TurnLedger", "launch_stamp"]
