"""The fast local stall signal: an injection that never visibly started.

A short timer armed at the injection site in the shim, reporting locally that the
injection never visibly started.

**The adapter's own report.** The stall line belongs to the adapter that did the
injecting, it fires in seconds, and it reports where the caller is. What it cannot see,
an adapter that died, is not reported by anything: `moxi comms explain` is where to look.

**What it depends on, and what it deliberately does not.** Arming is first-hand: the
adapter knows the instant it injected, with nothing asked of anyone. The report goes
out through the adapter's own `status_sink` -- its log, its stderr, whatever the
process that launched it wired up -- and touches no socket, no daemon, no credential.
That is what makes it survive two known weaknesses: a forged `activity` ping on the
daemon's claims socket cannot reach this, and the
unauthenticated shim control socket cannot be used to file a report this never sent.

It is NOT proof of anything. A fired timer says one thing: nothing has visibly started
under this name since we injected. That can be a dead session, a wake that went
nowhere, or a harness that is simply slower than the window today. The line says so and
points at `moxi comms explain`, which is the command built to answer the question this
raises.

**Re-measuring the window.** `INJECT_STALL_WINDOW` is a measured number, not a copied
one, and the thing it was measured against is a harness that ships a new version every
week. `last_latency` keeps the arm-to-witness gap of the most recent injection and both
adapters log it at info, one line per injected turn, so the number can be re-measured
from an ordinary run rather than from a rig somebody has to rebuild.
"""

from __future__ import annotations

import logging
import threading
import time

from .constants import INJECT_STALL_WINDOW

log = logging.getLogger("comms.stall")

#: The name of the thing, in `moxi comms explain`'s vocabulary. Its case is
#: `delivered-but-the-turn-never-completed` (`daemon.CASE_TURN_INCOMPLETE`); this one is
#: earlier and narrower, and the two names differ by exactly the word that differs in
#: fact: that case saw no turn END, this saw no turn START.
STALL_REASON = "injected-but-the-turn-never-started"

#: One line, said once per injection. It names the reason, gives the two numbers a
#: reader needs to believe it, and sends them to the command that can answer the
#: question it raises. It never carries the digest: a status line is a log line, and
#: the log rule admits ids and lengths, never bodies.
STALL_REPORT = (
    "{identity}: {reason}. The digest through {through} went in {elapsed:.0f}s ago and "
    "nothing has started under this name since. Run moxi comms explain {identity} for the "
    "binding, the cursor and the last delivery. A slow turn reads the same as a dead "
    "one from here, so this is a place to look, not a verdict."
)


class StallTimer:
    """One adapter's stall timer. Armed at injection, disarmed by the first sign of life.

    Thread-safe and re-entrant by design: the shim arms from the control socket's thread
    and witnesses from the same one. Everything is guarded by one lock and every public
    method is idempotent, because a timer that raises at an injection site would cost a
    delivery to save a warning.
    """

    def __init__(self, report, *, window: float = INJECT_STALL_WINDOW,
                 clock=time.monotonic, timer_factory=threading.Timer):
        self.report = report                # the adapter's status_sink
        self.window = float(window)
        self.clock = clock
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._armed_at: float | None = None
        self._identity: str | None = None
        self._through: str | None = None
        #: seconds between the last arm and the sign of life that answered it, or None
        #: when nothing has been witnessed yet. This is the measurement surface.
        self.last_latency: float | None = None
        #: how many injections have gone silent past the window, for the tests and for
        #: anyone who wants to know whether the number wants revisiting.
        self.fired = 0

    # ------------------------------------------------------------------ arming
    def arm(self, identity: str | None, through) -> None:
        """The adapter has just injected. Start counting.

        A second arm replaces the first: one injection is outstanding at a time (the
        shim holds `awaiting_completion`), and if that ever stopped being true the
        newest injection is the one worth watching.
        """
        with self._lock:
            self._cancel_locked()
            self._identity = identity
            self._through = str(through) if through is not None else "?"
            self._armed_at = self.clock()
            self._timer = self._timer_factory(self.window, self._fire)
            self._timer.daemon = True
            self._timer.start()

    # ------------------------------------------------------------------ disarming
    def witness(self) -> float | None:
        """A sign of life reached the adapter. Returns the arm-to-life gap, or None.

        None means nothing was armed, which is the ordinary case for every sign of life
        that is not answering an injection -- a typed turn, a chatty child's tenth line.
        Callers treat it as "nothing to do", never as an error.
        """
        with self._lock:
            if self._armed_at is None:
                return None
            latency = self.clock() - self._armed_at
            self.last_latency = latency
            self._cancel_locked()
            return latency

    def cancel(self) -> None:
        """Disarm without a witness: shutdown, a requeue, an injection being replaced."""
        with self._lock:
            self._cancel_locked()

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed_at is not None

    def _cancel_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._armed_at = None

    # ------------------------------------------------------------------ the report
    def _fire(self) -> None:
        with self._lock:
            if self._armed_at is None:      # witnessed between the timer and the lock
                return
            identity, through = self._identity, self._through
            elapsed = self.clock() - self._armed_at
            self._cancel_locked()           # said once per injection, never repeated
            self.fired += 1
        line = STALL_REPORT.format(identity=identity or "this session",
                                   reason=STALL_REASON, through=through,
                                   elapsed=elapsed)
        try:
            self.report(line)
        except Exception:                                            # noqa: BLE001
            # A status sink that throws must not take the adapter's timer thread with
            # it. Nothing downstream of here is load-bearing.
            log.exception("the stall report could not be delivered")


__all__ = ["STALL_REASON", "STALL_REPORT", "StallTimer"]
