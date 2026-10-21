"""The claims protocol: JSON lines over the unix socket
RUNTIME/comms/moxi-comms-daemon.sock.

Requests carry an `id`; the daemon echoes it on the reply. Daemon-initiated frames
(`deliver`, `replay_complete`, `notice`) carry no `id` -- that is how a client tells a
reply from a push on one duplex connection.

    ->  {"id": 1, "op": "register", "slot": 1, "name": "planner", "project": "proj-a",
         "adapter": "claude-shim"}
    <-  {"id": 1, "ok": true, "identity": {...}, "slot": 1}
    <-  {"op": "deliver", "payload": {...}}          # the delivery payload
    <-  {"op": "replay_complete", "identity": "...", "cursor": "..."}
    ->  {"id": 2, "op": "query", "kind": "resolve", "name": "main"}
    ->  {"id": 3, "op": "query", "kind": "explain", "identity": "planner@proj-a"}
    ->  {"op": "injected", "identity": "...", "through": "<message_id>",
         "seq": 7, "digest": "<the text the model was shown>"}         # no reply
    ->  {"op": "activity", "identity": "...", "seq": 8}                    # no reply

Connection close = deregistration; the connection IS the liveness signal.
"""

from __future__ import annotations

import errno as _errno
import json
import socket
import threading


class ProtocolError(Exception):
    pass


#: The claims protocol's version. The daemon answers `register` with it. Raise it when a
#: frame either side sends changes shape.
PROTOCOL_VERSION = 1

#: The verbs' contract with their daemon, apart from the claims protocol above: what a
#: verb writes on line zero and which query answers it reads. The daemon answers `ping`
#: with it, and `cli.Context.daemon()`, where every verb connects, refuses a daemon that
#: answers another number (an answer without it reads 0). Raise it whenever that contract
#: changes shape. 1 (2026-10): `[held]`, `[cold-at:…]`, `[reply-optional]`, `ever_bound`
#: in the resolve answer and `waiting` in the agents answer.
#: 2 (2026-10): `cache_code` in the session answer.
CLI_PROTOCOL = 2


# ------------------------------------------------------------------- ping ordering

#: The two fire-and-forget pings that carry a strictly increasing `seq`. They are hints
#: that feed `moxi comms explain`'s classification, and the only frames where a LATE or
#: REPLAYED copy can mislead it -- an `activity` ping that arrives after the window opened
#: says the harness is awake when it is not. The sender stamps `seq` from one counter per
#: connection (`DaemonClient.notify`); the daemon drops a ping whose `seq` is not
#: greater than the last it accepted from that same (connection, identity).
#:
#: `seq` is ORDERING, NOT AUTHENTICATION. It stops a replayed ping; it does not stop a
#: forged one, because nothing here checks the frame's identity against the
#: connection's binding. A frame that
#: carries no `seq` at all is still accepted, unordered: these are hints, and a lost
#: hint degrades the classification while a wrongly dropped one does the same.
ORDERED_NOTIFY = ("activity", "injected")


# ---------------------------------------------------------------- probe before unlink

#: Connect answered: something is LISTENING on that path right now.
PROBE_LIVE = "live"
#: ECONNREFUSED or ENOENT: a name with nothing behind it. Safe to clear.
PROBE_STALE = "stale"
#: Anything else. The path may be held; the prober cannot say. Safe to clear is NOT
#: what this means, and the caller must refuse.
PROBE_UNKNOWN = "unknown"

#: Load-bearing: a blocking `connect()` onto a full AF_UNIX backlog waits forever, and
#: a boot that hangs is worse than a boot that refuses.
PROBE_TIMEOUT = 0.5

#: The ALLOWLIST. Only these two errnos mean "nothing is behind this name". Measured:
#: ECONNREFUSED also covers a bound-but-never-listened socket and a plain regular file
#: at the path -- both safe to clear.
PROBE_STALE_ERRNOS = (_errno.ECONNREFUSED, _errno.ENOENT)


def probe(path, timeout: float = PROBE_TIMEOUT):
    """Is anything answering at `path`? Returns `(verdict, error)`.

    This is the opposite of `DaemonClient.connect()`, deliberately. The client treats
    every `OSError` as "offline", because for a client a socket it cannot reach is a
    socket it cannot use. A server about to UNLINK the path must reason the other way:
    an outcome it cannot classify is an outcome that might be a live holder, and the
    incident this exists to prevent was one process deleting another's
    live socket. So the errnos that mean "dead" are an allowlist, and everything else
    -- EAGAIN from a full backlog, EACCES from a foreign owner, a bare `OSError` with
    `errno is None` from an over-long path, a timeout -- comes back `PROBE_UNKNOWN`
    carrying its error, for the caller to refuse over by name.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError as e:
        if e.errno in PROBE_STALE_ERRNOS:
            return PROBE_STALE, None
        return PROBE_UNKNOWN, e
    else:
        return PROBE_LIVE, None
    finally:
        # A bare connect+close reaches the peer's serve loop as a clean EOF: `recv`
        # returns None, the handler breaks, nothing is registered and nothing is logged.
        sock.close()


class LineConn:
    """Framing helper: newline-delimited JSON both ways, with a write lock."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._wlock = threading.Lock()
        self._buf = b""
        self._closed = False

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            if self._closed:
                raise ProtocolError("connection closed")
            self.sock.sendall(data)

    def recv(self) -> dict | None:
        """Next frame, or None at EOF."""
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(65536)
            except (OSError, ValueError):
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        if not line.strip():
            return self.recv()
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise ProtocolError(f"bad frame: {e}") from e

    def close(self) -> None:
        with self._wlock:
            self._closed = True
        # shutdown() first: a peer thread blocked in recv() holds the open file
        # description alive, so close() alone neither wakes it nor sends FIN.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
