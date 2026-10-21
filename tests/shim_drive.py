"""Drive a `ClaudeShim` directly, as comms' Claude adapter does on turns' events
(`claude_adapter.ClaudeAdapter`). comms owns no hook, so the tests that once pinged a
hook control socket call these instead: a SessionStart is `ready`, a prompt is
`turn_started`, a Stop is `stop` then `turn_ended`, and the idle tick is `idle`."""

from __future__ import annotations


def ready(shim, source=None, session_id=None) -> None:
    """turns' `Ready`: the id it names, the ledger's change of context, then
    `activity kind=ready` with the source (owed when the shim is not yet bound)."""
    if session_id:
        shim.note_session(session_id, source)
    with shim._lock:
        shim.ledger.session_changed(source, session_id)
    shim.notify_ready(source)


def turn_started(shim) -> None:
    """turns' `TurnStarted` in the current session: `turned`, the stall timer's sign of
    life, and an `activity` with no kind, which releases nothing."""
    shim.note_turned()
    shim.stall.witness()
    shim.notify_activity(None)


def stop(shim, **fields):
    """The holder at a Stop: the shim's reply check. A block marks the shim blocked;
    the decision is returned."""
    decision = shim.on_stop(fields)
    if decision.block:
        shim.mark_blocked(decision.names)
    return decision


def turn_ended(shim, *, failed: bool = False):
    """turns' `TurnEnded`: a boundary, `unhandled` when the turn did not answer.
    Returns the digest the boundary injected, if any."""
    return shim.on_turn_boundary(failed=failed)


def stop_and_end(shim, **fields):
    """A Stop the holder lets end, then the turn's end; a Stop it holds ends nothing.
    Returns the decision."""
    decision = stop(shim, **fields)
    if not decision.block:
        shim.on_turn_boundary()
    return decision


def idle(shim):
    """The adapter's idle tick: queued mail into a session standing at its prompt."""
    return shim.on_turn_boundary(idle=True)


#: The hook ops the old control socket took, and what each now is.
TURN_BOUNDARY, TURN_FAILED, READY, PROMPT = "turn-boundary", "turn-failed", "ready", "prompt"


def drive(shim, op: str, session_id=None, *, source=None, **fields):
    """One old hook op, by name, handed to the shim the way the adapter hands turns'
    event: the session id first (a `ready`'s source with it), then the op. Returns
    what the op returns: a boundary's injected digest, or a Stop's decision."""
    if op == READY:
        return ready(shim, source, session_id)
    if session_id:
        shim.note_session(session_id, None)
    if op == TURN_BOUNDARY:
        return stop_and_end(shim, **fields)
    if op == TURN_FAILED:
        return turn_ended(shim, failed=True)
    if op == PROMPT:
        return turn_started(shim)
    raise ValueError(f"no such op {op!r}")
