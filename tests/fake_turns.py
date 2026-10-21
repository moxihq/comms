"""A stand-in for one turns session, shaped as `moxi_turns.Session` is: `deliver`,
`facts`, `watch` and `hold`, with the values turns hands back. The test drives the
session's turns by hand, as turns' Codex host would report them.

It imports nothing of turns: comms reaches turns only through its public API, and the
suite's end-to-end case (`test_codex_turns_host.py`) runs turns' real host."""

from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Started:
    turn_id: str | None


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Lost:
    reason: str


@dataclass(frozen=True)
class Facts:
    session_id: str | None
    epoch: int
    turn_open: bool
    agents: int = 0
    draft: bool = False
    at: float = 0.0
    stale: bool = False


@dataclass(frozen=True)
class Ready:
    source: str | None
    session_id: str | None


@dataclass(frozen=True)
class TurnStarted:
    turn_id: str | None
    session_id: str | None
    delivery_id: str | None = None


@dataclass(frozen=True)
class TurnEnded:
    turn_id: str | None
    outcome: str
    session_id: str | None
    delivery_id: str | None = None
    steered: tuple = ()


@dataclass(frozen=True)
class Gone:
    reason: str


@dataclass(frozen=True)
class StopFacts:
    turn_id: str | None = None
    session_id: str | None = None
    stop_hook_active: bool | None = None
    task_types: tuple | None = None
    crons: int | None = None


class FakeDelivery:
    def __init__(self, delivery_id, started):
        self.id = delivery_id
        self._started = started

    def wait_started(self, timeout):
        if self._started is None:
            raise TimeoutError("not started")
        return self._started


class FakeHold:
    def __init__(self, session, on_stop):
        self.session = session
        self.on_stop = on_stop
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    """One hosted thread. `answer` decides how the next delivery is answered:
    "start" (a new turn when idle, the running turn when one runs), "refuse", "lose"
    or "hang" (no answer)."""

    harness = "codex"
    can = frozenset({"start", "steer", "hold"})

    def __init__(self, session_id="thread-1", *, turn_open=False):
        self.session_id = session_id
        self.answer = "start"
        self.delivered: list = []          # (delivery id, text, steer)
        self.holder: FakeHold | None = None
        self.open_turn: str | None = None
        self.turn_deliveries: dict = {}    # turn id -> [delivery ids]
        self._events: queue.Queue = queue.Queue()
        self._ids = itertools.count(1)
        self._turns = itertools.count(1)
        self._lock = threading.Lock()
        self._epoch = 0
        if turn_open:
            self.start_turn()

    # -- what comms calls ---------------------------------------------------------------
    def facts(self):
        return Facts(self.session_id, self._epoch, self.open_turn is not None)

    def deliver(self, text, *, steer=False):
        did = f"d{next(self._ids)}"
        self.delivered.append((did, text, steer))
        if self.answer == "refuse":
            return FakeDelivery(did, Refused("busy-turn"))
        if self.answer == "lose":
            return FakeDelivery(did, Lost("the endpoint went away"))
        if self.answer == "hang":
            return FakeDelivery(did, None)
        with self._lock:
            turn = self.open_turn
        if turn is None or not steer:
            turn = self.start_turn(did)
        else:
            self.turn_deliveries.setdefault(turn, []).append(did)
        return FakeDelivery(did, Started(turn))

    def hold(self, on_stop):
        self.holder = FakeHold(self, on_stop)
        return self.holder

    def watch(self):
        while True:
            event = self._events.get()
            yield event
            if isinstance(event, Gone):
                return

    # -- what the test drives -----------------------------------------------------------
    def start_turn(self, delivery_id=None) -> str:
        turn = f"turn-{next(self._turns)}"
        with self._lock:
            self.open_turn = turn
            self._epoch += 1
        self.turn_deliveries[turn] = [delivery_id] if delivery_id else []
        self._events.put(TurnStarted(turn, self.session_id, delivery_id))
        return turn

    def end_turn(self, outcome="answer") -> str:
        with self._lock:
            turn, self.open_turn = self.open_turn, None
        ids = self.turn_deliveries.pop(turn, [])
        lead = ids[0] if ids else None
        self._events.put(TurnEnded(turn, outcome, self.session_id, lead, tuple(ids[1:])))
        return turn

    def stop(self, **facts):
        """A Stop of the running turn: what the holder answers."""
        return self.holder.on_stop(StopFacts(turn_id=self.open_turn,
                                             session_id=self.session_id, **facts))

    def ready(self, source):
        self._events.put(Ready(source, self.session_id))

    def gone(self, reason="the Codex window closed with status 0"):
        self._events.put(Gone(reason))


def launcher(session: FakeSession):
    """A `launch` stand-in that records its call and hands back `session`."""
    calls: list = []

    def launch(cwd, *, instructions=None, env=None, window=True, options=None,
               verify=None):
        calls.append({"cwd": cwd, "instructions": instructions, "env": dict(env or {}),
                      "window": window, "options": dict(options or {}),
                      "verify": verify})
        return session

    launch.calls = calls
    return launch
