"""Reply enforcement's ledger: which delivered messages owe a reply, and what pays one.

Harness-neutral on purpose: the claude shim keeps one `Ledger` per process and asks it
at every Stop, and another module may apply the same predicate, so nothing here knows
about hooks, sockets or the daemon's wire beyond the shape of the `owed` answer.

**Facts only.** Owed and paid are decided only on facts comms holds: the delivered payload,
the session's own launch declaration (`directors`) and the daemon's sent index. Nothing
here reads a body.

**The predicate.** A delivered message `M` from `A` owes `R` a reply when it was
injected since the last `/clear` (the shim opens a debt only after its injector returns),
it is under `OWED_TTL` old, it is not paid, and one of these
holds, read top to bottom by `owing_branch`:

  1. `M` is `[no reply]` or `[reply-optional]`: it owes nothing;
  2. `A` is the operator, by name or tier: it owes;
  3. `M` is `[reply-once]` and `A` is a declared director with verified attribution:
     it owes, as a directive;
  4. `M` is `[reply-once]` and `A`'s attribution is verified: it owes, as a peer's ask;
  5. anything else owes nothing.

So a verified once ask owes its recipient one reply whoever sent it, and the
operator's mail owes on any posture but none and optional. The ledger holds the rest.

**Paid.** By `R`'s own message to `A`, after `M`, whose posture is not `once`. An
operator debt is paid only by a message to `operator`.

**The loop-breaker.** A director-branch message threaded to a message that was
`R`'s payment to `A` owes nothing unless it is `[reply-once]`. A referent whose standing
cannot be known -- below this shim's registration high-water id, or not in the daemon's
index -- makes the message owe nothing: a missing fact only relaxes. The operator's
mail is exempt. Since a director's mail and a peer's owe only
on `[reply-once]`, no debt reaches it; the code stays.

**One counted reminder.** A debt's count is one flag (`counted`): a block marks the debts
it names `pending`; the next Stop with no work running counts them, and a counted debt is
dropped and recorded at that same quiet Stop. A pending mark is voided, uncounted, when the
Stop after the block had work running, could not read its facts, or the turn ended by
prompt, API error or a new session.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass

from . import constants
from .attribution import TIER_OPERATOR, VERIFIED
from .directors import standing_of
from .machine_line import POSTURE_NONE, POSTURE_ONCE, POSTURE_OPTIONAL

OPERATOR = "operator"

#: The branch a debt owes through. Only the director branch is subject to the
#: loop-breaker, and only off `[reply-once]`, which no director debt is any more.
BRANCH_OPERATOR, BRANCH_DIRECTOR = "operator", "director"
#: A verified `[reply-once]` ask from any other author.
BRANCH_PEER = "peer"

#: Background task types the harness runs for itself. Every other type, an
#: unknown one included, is work that will end and wake the session.
HARNESS_INTERNAL_TASKS = ("dream", "auto_mode_scan")

#: SessionStart sources that resolve a blocked turn as a full boundary. `compact`
#: lands inside the running continuation and resolves nothing.
RESOLVING_SOURCES = ("startup", "resume", "clear")

#: How many owed messages a reminder lists before "and <k> more".
REMINDER_LIST = 3


def owing_branch(payload: dict, directors=()) -> str | None:
    """The "who from" and "which mark" tests on one delivered payload: the branch `M`
    owes through, or None when it owes nothing. `directors` is the recipient's own
    declaration.

    The operator test is the name OR the tier: the name covers a forwarder that
    posts on the standing `operator` bundle, and the tier covers the terminal lane.
    The steps are the module docstring's, in order.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("posture") in (POSTURE_NONE, POSTURE_OPTIONAL):
        return None                                                    # 1
    author = payload.get("author") or {}
    name = author.get("name")
    if name == OPERATOR or author.get("tier") == TIER_OPERATOR:
        return BRANCH_OPERATOR                                         # 2
    if payload.get("posture") != POSTURE_ONCE:
        return None                                                    # 5
    if standing_of(author, directors).directive:
        return BRANCH_DIRECTOR                                         # 3
    if author.get("attribution") == VERIFIED:
        return BRANCH_PEER                                             # 4
    return None                                                        # 5


def epoch_of(stamp) -> float | None:
    """A record timestamp as epoch seconds, or None when it will not parse."""
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _as_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


@dataclass
class Debt:
    """One owed message. `author` is the name a payment must address: `operator` for
    every operator-branch debt, whatever lane the operator typed on."""

    message_id: str
    author: str
    branch: str
    reply_to: str | None
    posture: str
    at: float
    session_id: str | None
    counted: bool = False       # its one reminder has counted
    pending: bool = False       # a printed block named it, not yet counted or voided


@dataclass
class Decision:
    """What a Stop gets: a block naming these debts, or (`block` None) a boundary."""

    block: str | None = None
    names: tuple = ()
    dropped: tuple = ()         # debts counted and dropped at this Stop, to record


class Ledger:
    """The shim's in-memory ledger. Not thread-safe: the shim holds its lock."""

    def __init__(self):
        self.debts: dict[str, Debt] = {}
        #: R's own message ids known to be payments, with when they were learned; kept
        #: for `OWED_TTL` so the loop-breaker can recognise an answer to one.
        self.payments: dict[str, float] = {}
        #: The shim's registration high-water id (the catch-up boundary at its first
        #: register). A referent at or below it was sent before this shim could see
        #: payments, so whether it was one is unknown.
        self.hwm: str | None = None

    # ------------------------------------------------------------------ opening
    def open(self, payload: dict, directors, session_id, now: float) -> Debt | None:
        """Open a debt at injection, when the payload owes one."""
        branch = owing_branch(payload, directors)
        if branch is None:
            return None
        at = epoch_of(payload.get("timestamp"))
        if at is None or now - at >= constants.OWED_TTL:
            return None                                                # clause 5
        message_id = str(payload.get("message_id") or "")
        if not message_id.isdigit() or message_id in self.debts:
            return None
        author = OPERATOR if branch == BRANCH_OPERATOR else str(
            (payload.get("author") or {}).get("name") or "")
        debt = Debt(message_id=message_id, author=author, branch=branch,
                    reply_to=payload.get("reply_to"),
                    posture=str(payload.get("posture") or "open"), at=at,
                    session_id=session_id)
        self.debts[message_id] = debt
        return debt

    # ------------------------------------------------------------------ housekeeping
    def expire(self, now: float) -> None:
        """Debts and payments past `OWED_TTL` leave."""
        for mid in [m for m, d in self.debts.items() if now - d.at >= constants.OWED_TTL]:
            del self.debts[mid]
        for mid in [m for m, t in self.payments.items() if now - t >= constants.OWED_TTL]:
            del self.payments[mid]

    def void_pending(self) -> None:
        """Every pending mark voided, uncounted: the agent was not free to answer."""
        for d in self.debts.values():
            d.pending = False

    def mark_pending(self, names) -> None:
        for mid in names:
            if mid in self.debts:
                self.debts[mid].pending = True

    def session_changed(self, source, session_id) -> int:
        """SessionStart (debts empty when the context changes): `clear` empties every
        debt; `resume` empties the debts injected into another session, a resume with no
        session id counting as another (a missing fact only relaxes). `compact`, and a
        resume of the same session, keep them. Returns how many left."""
        if source == "clear":
            gone = list(self.debts)
        elif source == "resume":
            gone = [m for m, d in self.debts.items()
                    if session_id is None or d.session_id != session_id]
        else:
            return 0
        for mid in gone:
            del self.debts[mid]
        return len(gone)

    # ------------------------------------------------------------------ the Stop
    def query_args(self) -> tuple[dict, list]:
        """(`authors`: each author's oldest unpaid id, `referents`: each kept `re:`)."""
        authors: dict[str, str] = {}
        for d in self.debts.values():
            if d.author not in authors or _as_id(d.message_id) < _as_id(authors[d.author]):
                authors[d.author] = d.message_id
        referents = sorted({d.reply_to for d in self.debts.values()
                            if d.branch == BRANCH_DIRECTOR and d.reply_to
                            and d.posture != POSTURE_ONCE}, key=_as_id)
        return authors, referents

    def settle(self, answer: dict, now: float) -> None:
        """Payments, then the loop-breaker, from the daemon's `owed` answer, settled at a
        Stop. A paid debt leaves with its state; so does one the loop-breaker exempts."""
        sent = answer.get("sent") or {}
        for d in list(self.debts.values()):
            paying = [row for row in self._candidates(sent, d)
                      if _as_id(row.get("id")) > _as_id(d.message_id)
                      and row.get("posture") != POSTURE_ONCE]
            if paying:
                del self.debts[d.message_id]
                for row in paying:
                    self.payments.setdefault(str(row["id"]), now)
        referents = answer.get("referents") or {}
        for d in list(self.debts.values()):
            if self._loop_broken(d, referents):
                del self.debts[d.message_id]

    def _candidates(self, sent: dict, d: Debt) -> list:
        """R's messages to the debt's author: the only rows that can pay it."""
        return [r for r in (sent.get(d.author) or ()) if isinstance(r, dict)]

    def _loop_broken(self, d: Debt, referents: dict) -> bool:
        """Clause 4: does this director-branch debt owe nothing because it is threaded
        to R's payment -- or to a message whose standing cannot be known?"""
        if d.branch != BRANCH_DIRECTOR or not d.reply_to or d.posture == POSTURE_ONCE:
            return False
        if d.reply_to in self.payments:
            return True
        if self.hwm is None or _as_id(d.reply_to) <= _as_id(self.hwm):
            return True                             # sent before this shim saw payments
        if referents.get(d.reply_to) is None:
            return True                             # not in the index: unknown
        # R's own message above the high-water id and not a payment is R's unprompted
        # message, and someone else's message is not R's payment: either way it owes.
        return False

    def decide(self, *, stop_hook_active: bool, turn_blocks: int, work: bool,
               floor, now: float) -> Decision:
        """The rest of a Stop's decision, after the facts are read and settled."""
        if stop_hook_active and turn_blocks == 0:
            return Decision()                       # step 5: another hook blocked
        for d in self.debts.values():               # step 6: settle pending marks
            if d.pending:
                d.counted = d.counted or not work
                d.pending = False
        if work:
            return Decision()                       # step 7
        dropped = tuple(d for d in self.debts.values() if d.counted)
        for d in dropped:                           # step 8: drop, and record
            del self.debts[d.message_id]
        recorded = tuple(d for d in dropped if self.decidable(d, floor))
        remind = sorted((d for d in self.debts.values()
                         if not d.counted and self.decidable(d, floor)),
                        key=lambda d: _as_id(d.message_id))
        if turn_blocks == 0 and remind:             # step 9
            return Decision(block=reminder_text(remind, now),
                            names=tuple(d.message_id for d in remind), dropped=recorded)
        return Decision(dropped=recorded)

    @staticmethod
    def decidable(d: Debt, floor) -> bool:
        """A debt older than the index's floor neither blocks nor is recorded. A
        missing floor makes every debt undecidable."""
        try:
            return d.at >= float(floor)
        except (TypeError, ValueError):
            return False


def is_work(task_types, crons: int, *, queued: bool, undelivered: bool,
            awaiting) -> bool:
    """Is work running that will wake the session when it ends?"""
    return (any(t not in HARNESS_INTERNAL_TASKS for t in task_types) or crons > 0
            or queued or bool(undelivered) or bool(awaiting))


# --------------------------------------------------------------------------- the text

def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 2 * 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _named(d: Debt, now: float) -> str:
    if d.branch == BRANCH_OPERATOR:
        return f"the operator's msg {d.message_id} (directive, {_age(now - d.at)})"
    if d.branch == BRANCH_PEER:
        return f"{d.author}'s msg {d.message_id} ([reply-once], {_age(now - d.at)})"
    return f"{d.author}'s msg {d.message_id} (directive, {_age(now - d.at)})"


ANSWER_HINT = '"the result, the blocker, or your question"'
ASK_BACK = ("If you need to ask your director or a peer before you can answer, add "
            "--posture once: that keeps it open.")
TERMINAL = "If you already answered the operator at the terminal, stop again."
TAIL = "If it needs no answer, stop again."


def reminder_text(debts, now: float) -> str:
    """The block's reason.

    Up to three owed messages, oldest first, then "and <k> more". The ask-back sentence
    stays when any listed debt is a director's or a peer's (for an operator-only block it
    has no work to do); a debt to the operator adds the terminal sentence. It never asks
    for progress.
    """
    debts = sorted(debts, key=lambda d: _as_id(d.message_id))
    shown = debts[:REMINDER_LIST]
    if len(debts) == 1:
        d = debts[0]
        lines = [f"comms: you are ending this turn without answering {_named(d, now)}. "
                 "Answer it now with",
                 f"  moxi comms reply {d.message_id} {ANSWER_HINT}"]
    else:
        lines = ["comms: you are ending this turn without answering:"]
        lines += [f"  {_named(d, now)}" for d in shown]
        if len(debts) > len(shown):
            lines.append(f"  and {len(debts) - len(shown)} more")
        lines += ["Answer each now with", f"  moxi comms reply <id> {ANSWER_HINT}"]
    tail = []
    if any(d.branch in (BRANCH_DIRECTOR, BRANCH_PEER) for d in debts):
        tail.append(ASK_BACK)
    if any(d.branch == BRANCH_OPERATOR for d in debts):
        tail.append(TERMINAL)
    tail.append(TAIL)
    return "\n".join(lines + [" ".join(tail)])


__all__ = ["BRANCH_DIRECTOR", "BRANCH_OPERATOR", "BRANCH_PEER", "Debt",
           "Decision",
           "HARNESS_INTERNAL_TASKS", "Ledger", "RESOLVING_SOURCES", "is_work",
           "owing_branch", "reminder_text"]
