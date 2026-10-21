"""A stand-in turns endpoint for comms' Claude adapter tests, written to turns' socket
protocol (version 1, turns' `contracts/socket-protocol.md`), the contract every caller
speaks. The adapter reaches it through the real `moxi_turns` library (`find`, `hold`,
`watch`, `facts`, `deliver`, `wait`), so these tests exercise comms against the published
protocol and never against turns' internals, which comms may not import.

The test plays the harness: `start_turn` and `end_turn` are the session's turns, `ready`
a SessionStart, `stop` a Stop the holder is asked about, `go_away` the endpoint's end (a
plugin reload or a crash). With `auto` on (the default) a delivery to an idle session
starts its own turn at once, tied to it, as turns' fake harness does; a delivery made
while a turn runs waits for that turn's end. `refuse_next` and `lose_next` settle the next
delivery `Refused` or `Lost` instead.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

VERSION = 1


def runtime_root() -> Path:
    """turns' runtime root as the library finds it (`MOXI_TURNS_RUNTIME_DIR`)."""
    return Path(os.environ["MOXI_TURNS_RUNTIME_DIR"]) / "turns"


class _Conn:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.file = sock.makefile("rb")
        self.lock = threading.Lock()

    def send(self, obj) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with self.lock:
            self.sock.sendall(data)

    def recv(self):
        line = self.file.readline()
        if not line:
            return None
        return json.loads(line)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class FakeEndpoint:
    def __init__(self, *, pid: int | None = None, session_id: str | None = "s-1",
                 auto: bool = True, epoch: int = 0, can=("start", "steer", "hold",
                                                          "clear", "exit")):
        self.pid = os.getpid() if pid is None else pid
        self.session_id = session_id
        self.auto = auto
        self.epoch = epoch
        self.can = list(can)
        self.nonce = uuid.uuid4().hex[:12]
        self.dir = runtime_root() / f"claude-{self.nonce}"
        self.turn: dict | None = None          # {"id", "delivery"} of the open turn
        self.deliveries: dict[str, dict] = {}
        self.waiting: list[str] = []           # deliveries held for the turn's end
        self.texts: list[str] = []             # every text handed in, in order
        self.watchers: list[_Conn] = []
        self.holder: _Conn | None = None
        self.hold_answers: dict[int, object] = {}
        self.holds = 0                         # hold requests answered ok
        self.refusals: list[str] = []
        self.losses: list[str] = []
        self._ids = itertools.count(1)
        self._turns = itertools.count(1)
        self._asks = itertools.count(1)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._server: socket.socket | None = None
        self._conns: list[_Conn] = []
        self._stopped = False

    # ------------------------------------------------------------------ life
    def open(self) -> "FakeEndpoint":
        self.dir.mkdir(parents=True, mode=0o700)
        sock_path = self.dir / "sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(16)
        self._server = srv
        threading.Thread(target=self._accept, daemon=True).start()
        self._write_hello()
        return self

    def _write_hello(self) -> None:
        (self.dir / "hello.json").write_text(json.dumps(self.hello()))

    def hello(self) -> dict:
        return {"harness": "claude", "pid": self.pid, "session_id": self.session_id,
                "earlier": [], "cwd": os.getcwd(), "can": self.can, "nonce": self.nonce,
                "started": time.time(), "endpoint_version": "0.0.0-test",
                "mod_version": None, "protocol": [VERSION]}

    def go_away(self, reason: str = "the endpoint went away") -> None:
        """The endpoint ends: open deliveries read `Lost` (not untied), watchers get
        `gone`, the socket and the directory go."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            for did, d in self.deliveries.items():
                if d["ended"] is None:
                    d["ended"] = {"kind": "lost", "reason": reason}
                    if d["started"] is None:
                        d["started"] = d["ended"]
            self._cond.notify_all()
            watchers = list(self.watchers)
        for w in watchers:
            self._safe_send(w, {"v": VERSION, "event": {"kind": "gone", "reason": reason}})
        if self._server is not None:
            self._server.close()
        for c in list(self._conns):
            c.close()
        for name in ("hello.json", "sock"):
            (self.dir / name).unlink(missing_ok=True)
        try:
            self.dir.rmdir()
        except OSError:
            pass

    close = go_away

    # ------------------------------------------------------------------ the harness
    def start_turn(self, delivery: str | None = None) -> str:
        with self._lock:
            turn_id = f"t{next(self._turns)}"
            self.turn = {"id": turn_id, "delivery": delivery}
            self.epoch += 1
            if delivery is not None:
                self.deliveries[delivery]["started"] = {"kind": "started",
                                                        "turn_id": turn_id}
                self._cond.notify_all()
            self._emit({"kind": "turn-started", "turn_id": turn_id,
                        "session_id": self.session_id, "delivery_id": delivery})
            return turn_id

    def end_turn(self, outcome: str = "answer") -> None:
        with self._lock:
            turn, self.turn = self.turn, None
            assert turn is not None, "no turn is open"
            delivery = turn["delivery"]
            if delivery is not None:
                self.deliveries[delivery]["ended"] = {"kind": "ended",
                                                      "turn_id": turn["id"],
                                                      "outcome": outcome}
                self._cond.notify_all()
            self._emit({"kind": "turn-ended", "turn_id": turn["id"], "outcome": outcome,
                        "session_id": self.session_id, "delivery_id": delivery,
                        "steered": []})
            if self.waiting and self.auto:
                self.start_turn(self.waiting.pop(0))

    def ready(self, source: str, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is not None and session_id != self.session_id:
                self.session_id = session_id
                self.epoch = 0
                self._write_hello()
            self._emit({"kind": "ready", "source": source,
                        "session_id": self.session_id})

    def stop(self, facts: dict | None = None, timeout: float = 3.0):
        """A Stop: ask the holder, return its reason (None: the turn ends; also when
        there is no holder or it does not answer in time)."""
        facts = {"kind": "stop", "turn_id": (self.turn or {}).get("id"),
                 "session_id": self.session_id, "stop_hook_active": False,
                 "task_types": [], "crons": 0, **(facts or {})}
        with self._lock:
            holder = self.holder
            if holder is None:
                return None
            ask = next(self._asks)
        self._safe_send(holder, {"v": VERSION, "op": "stop", "ask": ask, "facts": facts})
        deadline = time.monotonic() + timeout
        with self._lock:
            while ask not in self.hold_answers:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self.hold_answers.pop(ask)

    def refuse_next(self, reason: str) -> None:
        self.refusals.append(reason)

    def lose_next(self, reason: str) -> None:
        self.losses.append(reason)

    def settle(self, delivery: str, result: dict) -> None:
        """Settle a handed delivery by hand (`refused` or `lost`)."""
        with self._lock:
            d = self.deliveries[delivery]
            d["ended"] = result
            if d["started"] is None:
                d["started"] = result
            if delivery in self.waiting:
                self.waiting.remove(delivery)
            self._cond.notify_all()

    # ------------------------------------------------------------------ the wire
    def _emit(self, event: dict) -> None:
        for w in list(self.watchers):
            self._safe_send(w, {"v": VERSION, "event": event})

    def _safe_send(self, conn: _Conn, obj) -> None:
        try:
            conn.send(obj)
        except OSError:
            pass

    def _accept(self) -> None:
        while True:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            conn = _Conn(sock)
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: _Conn) -> None:
        try:
            while True:
                frame = conn.recv()
                if frame is None:
                    break
                if not self._handle(conn, frame):
                    return            # the connection is kept by watch or hold
        except (OSError, ValueError):
            pass
        conn.close()

    def _ok(self, rid, **fields) -> dict:
        return {"v": VERSION, "id": rid, "ok": True, **fields}

    def _handle(self, conn: _Conn, frame: dict) -> bool:
        op, rid = frame.get("op"), frame.get("id")
        if op == "hello":
            conn.send(self._ok(rid, hello=self.hello()))
        elif op == "facts":
            with self._lock:
                facts = {"kind": "facts", "session_id": self.session_id,
                         "epoch": self.epoch, "turn_open": self.turn is not None,
                         "agents": 0, "draft": False, "at": time.time(), "stale": False}
            conn.send(self._ok(rid, facts=facts))
        elif op in ("deliver", "start"):
            conn.send(self._ok(rid, **self._deliver(frame)))
        elif op == "wait":
            conn.send(self._ok(rid, **self._wait(frame)))
        elif op == "watch":
            conn.send(self._ok(rid))
            with self._lock:
                self.watchers.append(conn)
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()
            return False
        elif op == "hold":
            with self._lock:
                if self.holder is not None:
                    conn.send({"v": VERSION, "id": rid, "ok": False, "code": "held",
                               "error": "another connection holds this session"})
                    return True
                self.holder = conn
                self.holds += 1
            conn.send(self._ok(rid))
            threading.Thread(target=self._hold_reader, args=(conn,), daemon=True).start()
            return False
        else:
            conn.send({"v": VERSION, "id": rid, "ok": False, "code": "unknown-op",
                       "error": f"unknown op {op!r}"})
        return True

    def _drain(self, conn: _Conn) -> None:
        try:
            while conn.recv() is not None:
                pass
        except (OSError, ValueError):
            pass
        with self._lock:
            if conn in self.watchers:
                self.watchers.remove(conn)

    def _hold_reader(self, conn: _Conn) -> None:
        try:
            while True:
                frame = conn.recv()
                if frame is None:
                    break
                if frame.get("op") == "stop-answer":
                    with self._lock:
                        self.hold_answers[frame.get("ask")] = frame.get("reason")
                        self._cond.notify_all()
        except (OSError, ValueError):
            pass
        with self._lock:
            if self.holder is conn:
                self.holder = None

    def _deliver(self, frame: dict) -> dict:
        with self._lock:
            did = f"d{next(self._ids)}"
            text = frame.get("text") or ""
            self.texts.append(text)
            d = {"text": text, "started": None, "ended": None}
            self.deliveries[did] = d
            if self.refusals:
                refused = {"kind": "refused", "reason": self.refusals.pop(0)}
                d["started"] = d["ended"] = refused
                return {"delivery": did, "result": refused}
            if self.losses:
                lost = {"kind": "lost", "reason": self.losses.pop(0)}
                d["started"] = d["ended"] = lost
                return {"delivery": did}
            if self.auto:
                if self.turn is None and not self.waiting:
                    self.start_turn(did)
                else:
                    self.waiting.append(did)
            return {"delivery": did}

    def _wait(self, frame: dict) -> dict:
        did = frame.get("delivery")
        key = "started" if frame.get("until") == "started" else "ended"
        timeout_ms = frame.get("timeout_ms")
        deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000
        with self._lock:
            d = self.deliveries.get(did)
            if d is None:
                return {"result": {"kind": "lost", "reason": "no such delivery"}}
            while d[key] is None:
                if deadline is None:
                    self._cond.wait(0.5)
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"pending": True}
                self._cond.wait(left)
            return {"result": d[key]}

    # ------------------------------------------------------------------ for the test
    def wait_for(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting on the fake endpoint")
            time.sleep(0.01)
