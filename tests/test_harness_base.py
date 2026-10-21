"""The shared adapter base on its own, with a port that does nothing but record.

`test_adapter_seam.py` pins what the codex port does where it meets the base. These pin what the base does for every port, whichever CLI is behind it.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from moxi_comms.adapters import harness
from moxi_comms.adapters.harness import TURN_COMPLETED, HarnessAdapter, TurnLedger

IDENTITY = "planner@proj-a"


def payload(mid):
    return {"message_id": str(mid), "channel_id": "c1", "room": "#names",
            "author": {"name": "lead", "tier": "chosen", "attribution": "verified"},
            "recipients": [IDENTITY], "posture": "open",
            "backlog": True, "text": f"body {mid}", "reply_to": None, "audience": None,
            "audience_source": None,
            "timestamp": "2026-09-30T12:00:00Z"}


class RecordingPort(HarnessAdapter):
    KIND = "test"

    def __init__(self):
        super().__init__(SimpleNamespace(directors=()), autoreconnect=False)
        self.delivered_batches: list = []

    def deliver(self, text, high, identity, count):
        self.delivered_batches.append((high, count))
        return "receipt"


def test_nothing_is_delivered_while_catch_up_is_still_arriving():
    """The catch-up hold: a registration arms it, and until the daemon says the replay
    is complete, a flush delivers nothing and leaves every queued message in place. The
    replay's end releases it, and the whole backlog then goes as one digest."""
    port = RecordingPort()
    port.identity = IDENTITY
    port.replaying = True
    port._on_push({"op": "deliver", "payload": payload(7)})
    port._on_push({"op": "deliver", "payload": payload(5)})

    assert port.flush() is None
    assert port.delivered_batches == []
    assert [p["message_id"] for p in port.queue] == ["7", "5"]

    port._on_push({"op": "replay_complete", "identity": IDENTITY, "cursor": "4"})
    assert port.replaying is False
    assert port.flush() is not None
    assert port.delivered_batches == [(7, 2)]
    assert port.queue == []


def test_the_turn_ledger_lives_in_the_shared_base():
    """The pending-injection ledger stays in comms for each adapter's owed ids."""
    ledger = harness.TurnLedger()
    ledger.injected("prompt-1", "12")
    assert ledger.completed({"id": "prompt-1", "status": harness.TURN_COMPLETED}) == "12"
    ledger.injected("prompt-2", "13")
    assert ledger.completed({"id": "prompt-2", "status": "error"}) is None
    assert not ledger.tracks("prompt-2")

# ------------------------------------------ M2: the held ids ([R19], §4.6)

class TurnPort(RecordingPort):
    """A port that reports each digest's turn end by id, as codex and opencode do."""
    HOLDS_UNTIL_TURN_ENDS = True

    def __init__(self):
        super().__init__()
        self.ledger = TurnLedger()
        self._turn_numbers = itertools.count(1)

    def deliver(self, text, high, identity, count):
        turn = f"turn-{next(self._turn_numbers)}"
        self.ledger.injected(turn, high, self.digest_ids(high))
        self.delivered_batches.append((high, count))
        return turn

    def end_turn(self, turn, status=TURN_COMPLETED):
        through, ids = self.ledger.end({"id": turn, "status": status})
        self.release_held(ids)
        return through


def push(port, *mids):
    for mid in mids:
        port._on_push({"op": "deliver", "payload": payload(mid)})


def queued(port):
    return [p["message_id"] for p in port.queue]


def held(port):
    return sorted(port.held_ids, key=int)


def online_port(cls=TurnPort):
    port = cls()
    port.identity = IDENTITY
    port._on_push({"op": "replay_complete", "identity": IDENTITY, "cursor": None})
    return port


def test_a_pushed_id_already_held_is_dropped():
    """Dedupe on push: an id the adapter already has queued, or injected into a turn
    that has not ended, is not queued again when a reconnect's replay hands it in."""
    port = online_port()
    push(port, 5, 5)
    assert queued(port) == ["5"]
    assert port.flush() is not None
    push(port, 5)
    assert port.queue == [] and port.flush() is None
    assert port.delivered_batches == [(5, 1)]


def test_the_register_lists_the_held_ids_as_holding(monkeypatch):
    """`holding` on register: the ids queued or injected and not ended, so the daemon
    claims them for the new binding and its replay does not hand them in again."""
    registered: list = []

    class Refused(Exception):
        pass

    class Client:
        def __init__(self, socket_path, on_push=None, on_close=None):
            pass

        def connect(self):
            pass

        def register(self, **fields):
            registered.append(fields)
            raise Refused

        def close(self):
            pass

    monkeypatch.setattr(harness, "DaemonClient", Client)
    port = online_port()
    port.agent = SimpleNamespace(directors=(), slot=3, name="planner", scope="proj-a")
    push(port, 12, 9)
    port.flush()                          # 9 and 12 in turn-1, which has not ended
    push(port, 15)                        # queued
    with pytest.raises(Refused):
        port.connect_daemon()
    assert registered[-1]["holding"] == ["9", "12", "15"]


def test_a_digests_turn_end_releases_exactly_its_ids():
    """The held set loses the ids of the digest whose turn ended, completed or not, and
    nothing else: a lower id still queued when a higher digest's turn completes stays
    held, so a re-push of it after a re-register is dropped, never injected twice."""
    port = online_port()
    push(port, 7)
    port.flush()                          # 7 in turn-1
    push(port, 3)                         # a lower id, queued out of order
    assert held(port) == ["3", "7"]
    assert port.end_turn("turn-1") == "7"
    assert held(port) == ["3"], "not pruned by comparing ids with the cursor"
    push(port, 3)
    assert queued(port) == ["3"]

    port.flush()                          # 3 in turn-2
    assert port.end_turn("turn-2", status="interrupted") is None
    assert held(port) == [], "an interrupted turn has ended too"


def test_the_ledger_keeps_each_turns_ids():
    ledger = TurnLedger()
    ledger.injected("turn-1", "7", ("5", "7"))
    ledger.injected("turn-1", "9", ("8", "9"))     # a second digest steered in
    ledger.injected("turn-2", "4", ("4",))
    assert ledger.ids("turn-1") == ("5", "7", "8", "9")
    assert ledger.pending("turn-1") == "9"
    assert ledger.end({"id": "turn-1", "status": "failed"}) == (None, ("5", "7", "8", "9"))
    assert ledger.end({"id": "turn-2", "status": TURN_COMPLETED}) == ("4", ("4",))
    assert ledger.end({"id": "operators", "status": TURN_COMPLETED}) == (None, ())
    assert ledger.turns == ()


def test_reregister_drops_the_queue_and_keeps_the_injected_ids(monkeypatch):
    """`reregister` drops the queue, whose ids replay, and keeps the ids injected into a
    turn that has not ended, which its register lists as `holding`."""
    port = online_port()
    push(port, 5)
    port.flush()                          # 5 in turn-1, still running
    push(port, 8)                         # queued
    seen: list = []
    port.connect_daemon = lambda: seen.append((held(port), queued(port)))
    port.reregister()
    assert seen == [(["5"], [])]
    push(port, 5, 8)                      # the new registration's replay
    assert queued(port) == ["8"]


def test_a_dropped_batch_and_a_returned_delivery_hold_nothing():
    """A batch the flush drops replays at the next registration, so its ids stop being
    held; a port that reports no turn end has ended the digest's turn when `deliver`
    returns."""
    class Failing(RecordingPort):
        def deliver(self, text, high, identity, count):
            raise RuntimeError("the harness went away")

    failing = online_port(Failing)
    push(failing, 4)
    with pytest.raises(RuntimeError):
        failing.flush()
    assert failing.held_ids == set()

    plain = online_port(RecordingPort)
    push(plain, 6)
    plain.flush()
    assert plain.held_ids == set()


# ------------------------------------------ [B8] the turn frame and the clamp (§3.3.3, [R3])

class FakeDaemon:
    """The binding's connection, as far as `notify_activity` needs it."""

    def __init__(self, connected=True):
        self.connected = connected
        self.frames: list = []

    def notify(self, op, **kw):
        from moxi_comms import cursors
        self.frames.append(dict(kw, op=op, cursor_then=cursors.read(IDENTITY)))


def bound_port(connected=True):
    port = online_port()
    port.offline = False
    port._daemon = FakeDaemon(connected)
    return port


def test_a_lower_held_id_keeps_the_cursor_below_it_until_it_completes():
    """The clamp: a digest completes while a lower id is still held (queued out of
    order). The cursor goes to one below that id, not to the digest's; once the lower
    id completes, the next completion reaches the high-water mark."""
    from moxi_comms import cursors
    port = bound_port()
    push(port, 7)
    port.flush()                                   # 7 in turn-1
    push(port, 3)                                  # held, queued
    _, ids = port.ledger.end({"id": "turn-1", "status": TURN_COMPLETED})
    port.report_turn_end(handled=ids)
    assert cursors.read(IDENTITY) == "2", "never past a held id"
    assert port.high_water == 7
    frame = port._daemon.frames[-1]
    assert (frame["kind"], frame["handled"], frame["unhandled"]) == ("turn", ["7"], [])
    assert frame["cursor_then"] == "2", "the cursor is written before the frame"

    port.flush()                                   # 3 in turn-2
    _, ids = port.ledger.end({"id": "turn-2", "status": TURN_COMPLETED})
    port.report_turn_end(handled=ids)
    assert cursors.read(IDENTITY) == "7"
    assert held(port) == []


def test_an_unhandled_id_is_held_until_the_frame_naming_it_is_sent():
    """Owed `unhandled` ids have no row until the frame names them, so they stay held
    (the clamp counts them) while no frame can go out, and are let go once one does."""
    from moxi_comms import cursors
    port = bound_port(connected=False)
    push(port, 5)
    port.flush()                                   # 5 in turn-1
    _, ids = port.ledger.end({"id": "turn-1", "status": "interrupted"})
    port.report_turn_end(unhandled=ids)
    assert port._daemon.frames == []
    assert port.unhandled_owed == ["5"] and held(port) == ["5"]
    assert cursors.read(IDENTITY) is None

    port._daemon.connected = True
    port.report_turn_end()                         # the next turn end of the agent
    frame = port._daemon.frames[-1]
    assert (frame["handled"], frame["unhandled"]) == ([], ["5"])
    assert port.unhandled_owed == [] and held(port) == []


def test_owe_unhandled_names_each_id_once():
    port = bound_port()
    port.owe_unhandled(["4", "4", 6])
    port.owe_unhandled(["6"])
    port.report_turn_end(unhandled=["4"])
    assert port._daemon.frames[-1]["unhandled"] == ["4", "6"]


def test_a_ready_that_cannot_be_sent_is_owed_to_the_next_register():
    """§3.4 item 1: a `ready` whose moment comes while the adapter is not bound (or its
    frame cannot go out) is sent once, right after the next register's answer."""
    port = bound_port(connected=False)
    port.report_ready("compact")
    assert port._owed_ready == "compact" and port._daemon.frames == []
    unbound = online_port()
    unbound.identity = None
    unbound.report_ready("startup")
    assert unbound._owed_ready == "startup"
