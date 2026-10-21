"""comms' Claude Code adapter: the process that holds one launched session's comms.

`moxi comms claude` forks it before it execs `claude` (`session.launch`), one per session
it launches, and it lives as long as the session's `claude` process. It holds what the
claude-shim held when Claude Code spawned it as an MCP server: the session's daemon
binding, the claim socket, the `turned` and root evidence, and the queue, digest, cursor
and owed ledger (`ClaudeShim`, which this process runs). What it no longer holds is any
way into the session: turns is the one interface to a harness session, so this adapter
is a turns caller. It finds the session's endpoint, becomes its holder, watches its turn
events and hands each digest to it with `deliver`.

    the binding        ClaudeShim.start(): registered before `claude` starts
    the claim socket   ClaudeShim's ClaimHolder, rooted at the `claude` process
    finding            turns.find(pid=<claude>), once a second until an endpoint answers
    the holder         Session.hold(on_stop): the Stop decision is ClaudeShim.on_stop
    release points     each TurnEnded, and an idle session (no turn open, mail queued)
    a digest           Session.deliver(text), settled from its turn's outcome

**Finding the endpoint.** Turns' Claude runner starts the endpoint at the session's
start, and it answers once it has its session id. Until this adapter has found it, mail
stays in the shim's queue and the daemon is told the session has no endpoint
(`ClaudeShim.notify_endpoint`), which a sender reads as `waiting`. The first look is a
release point when the session is idle (a `look` frame: no turn, nothing settled), so mail
the daemon held for a `ready` raised before the look is not left waiting. A look also
learns the session id and reads `turned` from the epoch: a session that has had a turn
has `turned` set.

**Delivery.** One digest at a time, at turn boundaries only. A digest goes in when a turn
ends (any turn: the person's, or the one that took the digest before it) or when the
session is idle with mail queued and the queue has stopped growing. turns' `deliver`
starts the turn now if the session is idle and else when the running turn ends. Each
digest is settled once, from what turns says of it:

    Ended(answer)                      handled: the cursor may pass it
    Ended(aborted | error | refusal)   unhandled: the daemon hands it again later
    Refused(reason)                    unhandled; it never went in
    Lost, untied                       unhandled; the endpoint is still there
    Lost (any other), or unreachable   unhandled, and the endpoint is gone

A `Lost` whose reason starts `untied` or `expired` (`Lost.untied`) was decided by an
endpoint that lives on: one delivery is unaccounted for and the endpoint is kept. Any
other `Lost`, a watch that ends, or an endpoint that cannot be reached means the
endpoint went away (a plugin reload, a crash): the adapter settles its open digest
`unhandled`, tells the daemon the session has no endpoint, and looks again. A Stop in
that gap is not held, and ends the turn.

**Esc.** turns reports an interrupted turn as `aborted`, so the turn's outcome says
whether the digest was handled.

**What ends it.** `claude`'s exit, seen on the handle the launcher took before it forked
(a pidfd, so a reused pid cannot stand in for it). The adapter closes its binding, which
deregisters the name, and removes its claim socket and claim file. An adapter that dies
first leaves the session unbound until it is relaunched.
"""

from __future__ import annotations

import logging
import os
import select
import threading
import time

from .. import constants
from .claude_shim import ClaudeShim

log = logging.getLogger("comms.claude.adapter")

#: How often the adapter looks for its session's endpoint while it has none (once
#: a second, as the design has it).
LOOK_INTERVAL = 1.0
#: While mail waits and the watch says a turn runs, how often the adapter reads the
#: ledger (`facts()`) to check it: a turn end the watch missed between the look's read and
#: the watch's start would otherwise hold the mail until the next turn ends.
RECHECK_INTERVAL = 5.0
#: The `activity` kind of a first look at an idle session (the daemon's `ACTIVITY_LOOK`).
LOOK = "look"
#: The outcome that settles a digest `handled`.
ANSWER = "answer"


def _turns_api():
    import moxi_turns

    return moxi_turns


class _Unsent:
    """A digest that could not be handed to an endpoint (it went away under the flush):
    its wait answers `Refused("gone")` at once, so it settles `unhandled`."""

    id = None

    def __init__(self, refused):
        self._refused = refused

    def wait_ended(self, timeout):
        return self._refused


class ClaudeAdapter:
    """One Claude Code session's comms: the shim's binding, queue and ledger, delivered
    through turns.

    `harness_pid` is the session's `claude` process, which `find(pid=…)` walks to.
    `turns` is the `moxi_turns` API (injectable for a test)."""

    def __init__(self, shim: ClaudeShim, harness_pid: int, *, turns=None,
                 look_interval: float = LOOK_INTERVAL,
                 recheck_interval: float = RECHECK_INTERVAL,
                 idle_poll: float = constants.IDLE_POLL,
                 idle_settle: float = constants.IDLE_SETTLE, clock=time.monotonic):
        self.shim = shim
        self.harness_pid = int(harness_pid)
        self.turns = turns if turns is not None else _turns_api()
        self.look_interval = look_interval
        self.recheck_interval = recheck_interval
        self.idle_poll = idle_poll
        self.idle_settle = idle_settle
        self._clock = clock
        #: The endpoint (`moxi_turns.Session`), or None while the session has none.
        self.session = None
        #: The delivery whose digest awaits its outcome, or None.
        self.delivery = None
        #: Whether a turn runs, as the watch last said (and the look's `facts()`).
        self.turn_open = False
        #: Endpoints found (test and log visibility).
        self.looks = 0
        self._hold = None
        #: Bumped at each look and each loss, so a watch or a wait of an endpoint that is
        #: gone changes nothing.
        self._generation = 0
        #: One decision at a time: a flush, a settle, a turn end, an endpoint's loss. The
        #: shim's own lock nests inside it.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seen = 0
        self._settled_at: float | None = None
        self._checked_at: float | None = None
        shim.injector = self._inject
        shim.endpoint = False

    # ------------------------------------------------------------------ lifecycle
    def start(self, *, tolerate_offline: bool = True) -> "ClaudeAdapter":
        """Bind (the shim's register, which needs no endpoint), then look and tick."""
        self.shim.start(tolerate_offline=tolerate_offline)
        for target, name in ((self._look_loop, "look"), (self._idle_loop, "idle")):
            t = threading.Thread(target=target, daemon=True, name=f"comms-claude-{name}")
            t.start()
            self._threads.append(t)
        return self

    def stop(self, *, remove_claim_file: bool = True) -> None:
        """The session ended: give up the hold, close the binding (the name deregisters)
        and remove the claim socket and, by default, the claim file."""
        self._stop.set()
        with self._lock:
            hold, self._hold = self._hold, None
            self.session = None
            self._generation += 1
        if hold is not None:
            hold.close()
        for t in self._threads:
            t.join(timeout=2.0)
        self.shim.stop(remove_claim_file=remove_claim_file)

    # ------------------------------------------------------------------ finding
    def _look_loop(self) -> None:
        while not self._stop.is_set():
            if self.session is None:
                try:
                    self.look()
                except Exception:                                  # noqa: BLE001
                    log.exception("looking for the session's endpoint failed")
            self._stop.wait(self.look_interval)

    def look(self) -> bool:
        """One look for the session's endpoint. True when one was found and held."""
        T = self.turns
        s = T.find(pid=self.harness_pid)
        if s is None:
            return False
        try:
            hold = s.hold(self._on_stop)
        except T.TurnsError as e:
            log.info("the session's endpoint did not take this adapter as its holder "
                     "(%s); looking again", e)
            return False
        try:
            facts = s.facts()
        except T.TurnsError as e:
            log.info("the session's endpoint did not answer facts (%s); looking again", e)
            hold.close()
            return False
        with self._lock:
            if self._stop.is_set():
                hold.close()
                return False
            self._generation += 1
            generation = self._generation
            self.session, self._hold = s, hold
            self.turn_open = bool(facts.turn_open)
            self._checked_at = self._clock()
            self.looks += 1
            first = self.looks == 1
            self._learn_session(facts.session_id or s.session_id)
            if facts.epoch > 0:
                self.shim.note_turned()
            self.shim.notify_endpoint(True)
        # The watch starts after the ledger is read. The library gives no sign of the
        # watch's subscription, so a turn event in the milliseconds between the read and
        # the subscription can be missed: the delivery's own end still reaches its wait,
        # and `tick` reads the ledger again while mail waits behind a turn or a block.
        threading.Thread(target=self._watch, args=(s, generation), daemon=True,
                         name="comms-claude-watch").start()
        log.info("found the session's endpoint at %s (session %s, %s)", s.dir,
                 facts.session_id or s.session_id, "a turn runs" if facts.turn_open
                 else "idle")
        if first and not facts.turn_open:
            # The first look is a release point when the session is idle: a `ready` the
            # session raised before this adapter was watching loses nothing. It is not a
            # turn, so it is no evidence for `turned` and settles nothing. A later look
            # (after a plugin reload or an endpoint crash) follows no `ready`, so it
            # releases nothing: `--held` mail waits for a real release point.
            self.shim.notify_activity(LOOK)
        return True

    def _learn_session(self, session_id) -> None:
        """The session id a look read. The first one this adapter learns is not a new
        session (it changes nothing about `turned`); a later one that differs may hold a
        context, so `note_session` sets `turned`."""
        if not session_id:
            return
        with self.shim._lock:
            first = self.shim.session_id is None
            if first:
                self.shim.session_id = session_id
        if not first:
            self.shim.note_session(session_id)

    # ------------------------------------------------------------------ events
    def _watch(self, s, generation: int) -> None:
        T = self.turns
        reason = "the watch ended"
        try:
            for event in s.watch():
                if self._stop.is_set() or generation != self._generation:
                    return
                if isinstance(event, T.Gone):
                    reason = event.reason
                    break
                self._on_event(event)
        except Exception as e:                                     # noqa: BLE001
            log.exception("the watch of the session's endpoint failed")
            reason = f"the watch failed: {type(e).__name__}: {e}"
        self._endpoint_gone(generation, reason)

    def _current(self, session_id) -> bool:
        return not session_id or session_id == self.shim.session_id

    def _on_event(self, event) -> None:
        T = self.turns
        if isinstance(event, T.Ready):
            # A start, resume, clear or compaction. A `clear` lowers `turned` for the new
            # id, and empties the debts the running context never saw; the daemon releases
            # held mail by its source.
            with self._lock:
                if event.session_id:
                    self.shim.note_session(event.session_id, event.source)
                with self.shim._lock:
                    gone = self.shim.ledger.session_changed(event.source, event.session_id)
            if gone:
                log.info("ready %s: %d owed repl(ies) left the ledger with the context",
                         event.source, gone)
            self.shim.notify_ready(event.source)
        elif isinstance(event, T.TurnStarted):
            with self._lock:
                self.turn_open = True
                self._checked_at = self._clock()
            if self._current(event.session_id):
                self.shim.note_turned()
            latency = self.shim.stall.witness()
            if latency is not None:
                log.info("turn %s started %.2fs after the hand-over (delivery %s)",
                         event.turn_id, latency, event.delivery_id)
            self.shim.notify_activity(None)
        elif isinstance(event, T.TurnEnded):
            with self._lock:
                self.turn_open = False
                self._checked_at = self._clock()
                d = self.delivery
                if d is not None and event.delivery_id is not None \
                        and event.delivery_id == d.id:
                    self._settle_ended(d, event.outcome)
                elif d is not None:
                    # Another turn ended (the person's, or one the digest waits behind):
                    # the digest still awaits its own. The turn's block is over, and the
                    # daemon hears of the boundary, which releases its held mail.
                    self.shim._clear_block()
                    if self._current(event.session_id):
                        self.shim.note_turned()
                    self.shim._send_turn()
                else:
                    # A boundary with no digest awaiting: it completes nothing, tells the
                    # daemon, and hands over what is queued.
                    self.shim.on_turn_boundary()

    # ------------------------------------------------------------------ delivery
    def _inject(self, text: str) -> None:
        """The shim's injector: hand one digest to the endpoint. Called from a flush,
        under the adapter's lock. Never raises."""
        s = self.session
        if s is None:
            d = _Unsent(self.turns.Refused("gone"))
        else:
            d = s.deliver(text)
        log.info("handed a digest to turns as delivery %s", d.id)
        self.delivery = d
        threading.Thread(target=self._wait, args=(d,), daemon=True,
                         name="comms-claude-wait").start()

    def _wait(self, d) -> None:
        try:
            result = d.wait_ended(None)
        except Exception as e:                                     # noqa: BLE001
            log.exception("waiting on a delivery failed")
            result = self.turns.Lost(f"the wait failed: {type(e).__name__}: {e}")
        self._on_result(d, result)

    def _on_result(self, d, result) -> None:
        T = self.turns
        with self._lock:
            if self.delivery is not d:
                return                 # settled already, by its turn's end on the watch
            if isinstance(result, T.Ended):
                self._settle_ended(d, result.outcome)
                return
            self.delivery = None
            gone = ((isinstance(result, T.Lost) and not result.untied)
                    or (isinstance(result, T.Refused) and d.id is None))
            log.info("delivery %s %s (%s): its digest is unhandled",
                     d.id, "was refused" if isinstance(result, T.Refused) else "was lost",
                     getattr(result, "reason", result))
            self.shim._complete_turn(failed=True)
            generation = self._generation
        if gone:
            self._endpoint_gone(generation, f"delivery {d.id}: {getattr(result, 'reason', '')}")

    def _settle_ended(self, d, outcome: str) -> None:
        """The digest's turn ended: handled on `answer`, unhandled on any other outcome.
        The turn end is a boundary, so the next digest goes in. Under the lock."""
        self.delivery = None
        log.info("delivery %s: its turn ended %s; the digest is %s", d.id, outcome,
                 "handled" if outcome == ANSWER else "unhandled")
        self.shim.on_turn_boundary(failed=outcome != ANSWER)

    def _endpoint_gone(self, generation: int, reason: str) -> None:
        """The endpoint went away while `claude` may live (a reload, a crash): settle the
        open digest `unhandled`, tell the daemon, and look again."""
        with self._lock:
            if generation != self._generation or self.session is None:
                return
            self._generation += 1
            hold, self._hold = self._hold, None
            self.session = None
            self.turn_open = False
            d, self.delivery = self.delivery, None
            if d is not None:
                self.shim._complete_turn(failed=True)
            self.shim._clear_block()
            self.shim.notify_endpoint(False)
        if hold is not None:
            hold.close()
        if not self._stop.is_set():
            log.info("the session's endpoint went away (%s); looking again", reason)

    # ------------------------------------------------------------------ holding
    def _on_stop(self, facts) -> str | None:
        """The holder's answer at a Stop: the shim's reply check. A reminder holds the
        turn open; None lets it end. A fact the harness did not give stays missing, and
        a missing fact lets the turn end (`ClaudeShim.on_stop`)."""
        fields = {}
        if facts.stop_hook_active is not None:
            fields["stop_hook_active"] = facts.stop_hook_active
        if facts.task_types is not None:
            fields["task_types"] = list(facts.task_types)
        if facts.crons is not None:
            fields["crons"] = facts.crons
        decision = self.shim.on_stop(fields)
        if decision.block:
            self.shim.mark_blocked(decision.names)
            return decision.block
        return None

    # ------------------------------------------------------------------ idle
    def _idle_loop(self) -> None:
        while not self._stop.wait(self.idle_poll):
            try:
                self.tick()
            except Exception:                                      # noqa: BLE001
                log.exception("the idle check failed")

    def tick(self) -> bool:
        """One idle check. True when it handed a digest over.

        An idle session with mail queued is standing at a boundary: once the queue has
        stopped growing for `idle_settle`, the queue goes in as one digest, through the
        same flush a turn end uses. It is not a turn end: it sets no `turned` and its
        notify carries no `kind`, so it releases nothing held."""
        recheck = None
        with self._lock:
            shim = self.shim
            if (self.session is None or self.delivery is not None or not shim.queue
                    or shim.awaiting_completion):
                self._settled_at = None
                self._seen = len(shim.queue)
                return False
            if self.turn_open or shim.blocked:
                self._settled_at = None
                now = self._clock()
                if self._checked_at is None or now - self._checked_at >= self.recheck_interval:
                    self._checked_at = now
                    recheck = self.session
                else:
                    return False
            else:
                queued = len(shim.queue)
                now = self._clock()
                if queued != self._seen or self._settled_at is None:
                    self._seen, self._settled_at = queued, now
                    return False
                if now - self._settled_at < self.idle_settle:
                    return False
                self._settled_at = None
                shim.on_turn_boundary(idle=True)
                return self.delivery is not None
        # Mail waits behind a turn the watch says runs, or a block on it: read the ledger,
        # outside the lock. No turn open means the watch missed that turn's end, which
        # also ended any block on it.
        try:
            facts = recheck.facts()
        except self.turns.TurnsError:
            return False
        with self._lock:
            if self.session is recheck and not facts.turn_open:
                if self.turn_open or self.shim.blocked:
                    log.info("the ledger says no turn runs; the watch missed a turn end")
                self.turn_open = False
                self.shim._clear_block()
                self._learn_session(facts.session_id)
        return False


# --------------------------------------------------------------------------- the process

def wait_for_exit(fd: int, stop: threading.Event | None = None) -> None:
    """Block until the process `fd` (a pidfd) refers to has ended, or `stop` is set."""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    while not (stop is not None and stop.is_set()):
        if poller.poll(500):
            return


def serve(shim: ClaudeShim, harness_pid: int, harness_fd: int, *, ready=None,
          turns=None) -> int:
    """The adapter process's life: bind, say ready, deliver until `claude` ends, close.

    `ready()` is called once the bind has been tried (bound, or the daemon not there and
    the supervisor left to bind when it returns) and the claim socket listens, so the
    launcher can exec `claude`. A register the daemon refused for good (an unknown kind,
    an invalid name) ends the adapter with its one line in the log: the session runs
    without comms, as a dead shim's did."""
    from ..client import DaemonError

    adapter = ClaudeAdapter(shim, harness_pid, turns=turns)
    try:
        adapter.start(tolerate_offline=True)
    except DaemonError as e:
        log.warning("this session did not get its comms binding: %s", e)
        if ready is not None:
            ready()
        shim.stop(remove_claim_file=True)
        return 2
    if ready is not None:
        ready()
    log.info("adapter for claude pid %d is up (pid %d)", harness_pid, os.getpid())
    try:
        wait_for_exit(harness_fd)
    finally:
        log.info("claude pid %d has ended; closing this session's comms", harness_pid)
        adapter.stop(remove_claim_file=True)
    return 0


__all__ = ["ClaudeAdapter", "LOOK_INTERVAL", "RECHECK_INTERVAL", "serve", "wait_for_exit"]
