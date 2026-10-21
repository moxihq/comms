"""Client side of the claims protocol -- used by the CLI (query) and by adapters.

Adapters reconnect with backoff and surface "comms offline" to their agent while
disconnected; the CLI takes a single shot and reports honestly when the daemon
socket is gone.
"""

from __future__ import annotations

import itertools
import random
import socket
import threading
import time

from . import constants
from .paths import daemon_socket
from .protocol import ORDERED_NOTIFY, LineConn, ProtocolError


class DaemonOffline(Exception):
    """The daemon socket is gone. Never dressed up as anything else."""


class DaemonError(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response or {}


class DaemonTimeout(DaemonError):
    """A request the daemon never answered inside `DaemonClient.timeout`.

    The refusal-class split. A plain `DaemonError` is a daemon that ANSWERED and said no
    -- someone else holds this name. A timeout is a daemon that said nothing at all,
    which is an unreachable daemon wearing a different hat: nothing refused anything,
    and there is no holder to name. The shim's tolerate-at-launch path therefore treats
    it as offline (retry) rather than printing a refusal line for a claim nobody
    contested. Register is the call that made this concrete -- it holds a gateway round
    trip in production -- but the timeout is raised for every op from one place, so this
    is the honest name for it.

    A SUBCLASS on purpose: every existing `except DaemonError` (the CLI's, the shim's
    refusal arm) keeps catching it, and only code that wants the split says so.
    """


class DaemonClient:
    """One duplex connection. `on_push` receives daemon-initiated frames."""

    def __init__(self, socket_path=None, on_push=None, timeout: float = 10.0, on_close=None):
        self.socket_path = str(socket_path or daemon_socket())
        self.on_push = on_push
        self.on_close = on_close
        self.timeout = timeout
        self._conn: LineConn | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        #: The per-source ping counter. One counter per DaemonClient, and every adapter
        #: builds a fresh client on every (re)connect, so "source" is exactly one
        #: adapter process's attachment to the daemon. It is shared by `activity` and
        #: `injected` on purpose: the daemon keeps ONE last-accepted number per source,
        #: so the two kinds have to be numbered off the same strictly increasing count.
        self._notify_seq = itertools.count(1)
        self._cv = threading.Condition()
        self._reader: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> "DaemonClient":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonOffline(f"comms offline: {self.socket_path}: {e}") from e
        self._conn = LineConn(s)
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self

    def close(self) -> None:
        self._closed = True
        if self._conn:
            self._conn.close()
        with self._cv:
            self._cv.notify_all()

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._closed

    def _read_loop(self) -> None:
        conn = self._conn
        try:
            while True:
                frame = conn.recv()
                if frame is None:
                    break
                if "id" in frame:
                    with self._cv:
                        self._pending[frame["id"]] = frame
                        self._cv.notify_all()
                elif self.on_push:
                    self.on_push(frame)
        except (ProtocolError, OSError):
            pass
        finally:
            self._closed = True
            with self._cv:
                self._cv.notify_all()
            if self.on_close:
                self.on_close()

    # ------------------------------------------------------------------ requests
    def request(self, op: str, **kwargs) -> dict:
        if not self.connected:
            raise DaemonOffline("comms offline: not connected to the daemon")
        rid = next(self._ids)
        frame = {"id": rid, "op": op, **kwargs}
        try:
            self._conn.send(frame)
        except (ProtocolError, OSError) as e:
            raise DaemonOffline(f"comms offline: {e}") from e
        deadline = time.monotonic() + self.timeout
        with self._cv:
            while rid not in self._pending:
                if self._closed:
                    raise DaemonOffline("comms offline: daemon closed the connection")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DaemonTimeout(f"timeout waiting for {op}")
                self._cv.wait(remaining)
            resp = self._pending.pop(rid)
        if not resp.get("ok", False):
            raise DaemonError(resp.get("error", "daemon refused"), resp)
        return resp

    def notify(self, op: str, **kwargs) -> None:
        """Fire-and-forget frame (activity / injected pings, nudge).

        The two ordered ops get a `seq` stamped here so that every sender gets one
        without every ping site remembering to; `nudge` and anything else goes
        unnumbered, because nothing on the other side orders it.
        """
        if not self.connected:
            return
        frame = {"op": op, **kwargs}
        if op in ORDERED_NOTIFY:
            frame["seq"] = next(self._notify_seq)
        try:
            self._conn.send(frame)
        except (ProtocolError, OSError):
            self._closed = True

    def register(self, *, slot="next-free", name=None, scope=None, global_=False,
                 project=None, adapter="claude-shim", session_id=None,
                 holding=None, projects_root=None, turned=None, report=None,
                 endpoint=None) -> dict:
        """`session_id`, `holding`, `projects_root` and `turned` are the claude-shim's:
        which live session is behind the name, the ids it already holds, the root its
        Claude Code writes transcripts under, and whether a prompt or a turn end has been
        seen in that session (cold-sends). `report` is a codex or opencode launcher's
        evidence for its cache clock, sent as it stands (`session_id`, `cache_source`,
        `turned`, `new_session`, `agent_model`; `HarnessAdapter.session_report`). A
        daemon older than them reads none, and they are left off when there is nothing
        to say. `endpoint` is a Claude adapter's word on whether its session has a turns
        endpoint yet; None sends nothing."""
        extra = dict(report or {})
        if session_id:
            extra["session_id"] = session_id
        if holding:
            extra["holding"] = list(holding)
        if projects_root:
            extra["projects_root"] = str(projects_root)
        if turned is not None:
            extra["turned"] = bool(turned)
        if endpoint is not None:
            extra["endpoint"] = bool(endpoint)
        return self.request("register", slot=slot, name=name, scope=scope,
                            **{"global": global_}, project=project, adapter=adapter,
                            **extra)

    def query(self, kind: str, **kwargs) -> dict:
        return self.request("query", kind=kind, **kwargs)

    def attributed_message(self, message_id) -> dict | None:
        """One message with its author as the daemon attributes it, or None when the
        record holds no such id. The daemon's `attributed_message` read
        (`Daemon._attributed_message_query`): `message_id`, `room`, `author` (`name`,
        `tier`, `attribution`), `recipients`, `resolved`, `text`, `timestamp`. A read; it
        changes nothing. Raises `DaemonOffline`, `DaemonTimeout`, or `DaemonError` for a
        refusal (an id that is not a message id) or a daemon older than the read."""
        answer = self.query("attributed_message", message_id=str(message_id))
        if not answer.get("found"):
            return None
        return {k: answer.get(k) for k in ATTRIBUTED_FIELDS}


#: The fields `attributed_message` hands back for a message the record holds.
ATTRIBUTED_FIELDS = ("message_id", "room", "author", "recipients", "resolved", "text",
                     "timestamp")


def attributed_message(message_id, *, socket_path=None, timeout: float = 10.0) -> dict | None:
    """`DaemonClient.attributed_message` on a connection of its own, closed after.

    The read the notify service makes when an agent registers an item: it asks for the
    message by the id `moxi comms send` printed, and takes the item's sender and text from
    this answer, never from the registering process."""
    client = DaemonClient(socket_path, timeout=timeout).connect()
    try:
        return client.attributed_message(message_id)
    finally:
        client.close()


def backoff_delays(rng=None):
    """Reconnect backoff shape: 0.5s, x2, capped 30s, +/-20% jitter, forever."""
    rng = rng or random
    delay = constants.BACKOFF_START
    while True:
        jitter = 1.0 + rng.uniform(-constants.BACKOFF_JITTER, constants.BACKOFF_JITTER)
        yield min(delay, constants.BACKOFF_MAX) * jitter
        delay = min(delay * constants.BACKOFF_FACTOR, constants.BACKOFF_MAX)
