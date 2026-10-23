"""Test kit for the claim holder: fake sessions on a real daemon.

A `Session` is one adapter process as the claim machinery sees it: a `ClaimHolder` on a
real claim socket, registered on a real `DaemonClient` against the rig's daemon, so
registers, refusals and the binding table are the daemon's own.

Which session a process is in is decided by a fake `/proc` (`FakeProc`): each session
has a root pid, and a caller runs as a child pid of that root. `DirectConn` hands each
frame to the holder's own `dispatch` as that pid, so the membership rule runs exactly as
on a real socket.
"""

from __future__ import annotations

import itertools
import os
import threading
import time

from moxi_comms import claimfile
from moxi_comms.claimhold import ClaimHolder, _Conn
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import Daemon
from moxi_comms.membership import TREE, ProcView, Root
from moxi_comms.paths import daemon_socket, runtime_root
from moxi_comms.scoping import qualify

_pids = itertools.count(41000, 100)


class FakeProc(ProcView):
    def __init__(self):
        super().__init__()
        self.table: dict = {}

    def stat(self, pid):
        return self.table.get(int(pid))

    def spawn(self, ppid=1, start=None) -> int:
        pid = next(_pids)
        self.table[pid] = (ppid, start or f"s{pid}")
        return pid

    def child(self, ppid) -> int:
        pid = ppid + 1
        while pid in self.table:
            pid += 1
        self.table[pid] = (ppid, f"s{pid}")
        return pid

    def kill(self, pid) -> None:
        self.table.pop(int(pid), None)


class Session:
    """One fake adapter process holding one claim."""

    def __init__(self, proc, name, scope="proj-a", slot="next-free", *, kind="codex",
                 root=None):
        self.proc = proc
        if root is None:
            self.root = proc.spawn()
            self.verb_pid = proc.child(self.root)
            start = f"s{self.root}"
        else:                         # a real root: this test process is inside it
            self.root, start = root.pid, root.start
            self.verb_pid = os.getpid()
        self.key = f"{self.root}-{start}-{next(_pids)}"
        self.client: DaemonClient | None = None
        self.registers = 0
        self.dead = False
        self.holder = ClaimHolder(
            kind=kind, key=self.key,
            socket_path=runtime_root() / f"comms-adapter-test-{self.root}.sock",
            roots=[root or Root(self.root, f"s{self.root}", TREE)],
            keying="pid", proc=proc)
        claim = {"name": name, "scope": scope, "global": False,
                 "qualified": qualify(name, scope), "slot": slot, "tier": None,
                 "directors": []}
        self.holder.start_launch(claim)
        self.holder.listen()

    def register(self, claim):
        self.registers += 1
        client = DaemonClient(daemon_socket(), on_close=self._closed)
        client.connect()
        try:
            resp = client.register(slot=claim.get("slot") or "next-free",
                                   name=claim["name"], scope=claim["scope"],
                                   project=claim["scope"], adapter="codex")
        except BaseException:
            client.close()
            raise
        self.client = client
        self.holder.bound(resp)
        return resp

    def _closed(self):
        if self.holder.state == claimfile.BOUND:
            self.holder.went_offline()

    def bind(self):
        claim, _exact = self.holder.register_claim()
        return self.register(claim)

    @property
    def socket(self) -> str:
        return str(self.holder.socket_path)

    @property
    def state(self):
        return self.holder.state

    @property
    def qualified(self):
        return (self.holder.claim or {}).get("qualified")

    def record(self):
        return claimfile.read(self.key)

    def kill(self):
        """The process ends: its connection closes, its socket goes, its root dies.
        The file stays, as a crash leaves it."""
        self.dead = True
        if self.client is not None:
            self.client.close()
            self.client = None
        self.holder.close(remove_file=False)
        if isinstance(self.proc, FakeProc):
            self.proc.kill(self.root)
            self.proc.kill(self.verb_pid)

    def stop(self):
        if not self.dead:
            if self.client is not None:
                self.client.close()
            self.holder.close()


class DirectConn:
    def __init__(self, holder, pid):
        self.holder = holder
        self.ctx = _Conn(pid)

    def call(self, op, **fields):
        if self.holder._server is None:
            raise OSError("connection refused")
        return self.holder.dispatch({"op": op, **fields}, self.ctx)

    def close(self):
        pass


class World:
    """All the sessions of one test."""

    def __init__(self):
        self.proc = FakeProc()
        self.sessions: list = []

    def real(self, *a, bind=True, **kw) -> Session:
        """A session whose root is this test process's parent, on the real `/proc`: the
        in-process CLI is inside it (its socket's SO_PEERCRED is real)."""
        from moxi_comms.membership import DEFAULT_PROC, root_of
        s = Session(DEFAULT_PROC, *a, root=root_of(os.getppid(), TREE), **kw)
        self.sessions.append(s)
        if bind:
            s.bind()
        return s

    def session(self, *a, bind=True, **kw) -> Session:
        s = Session(self.proc, *a, **kw)
        self.sessions.append(s)
        if bind:
            s.bind()
        return s

    def call(self, target: Session, caller_pid, op, **fields):
        return DirectConn(target.holder, caller_pid).call(op, **fields)

    def stop(self):
        for s in self.sessions:
            s.stop()


def bound_names(rig) -> dict:
    return {b.qualified: b.slot_id for b in rig.daemon.bindings.values()}


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ------------------------------------------------------------- a dropped register

class RegisterDrop:
    """A daemon arm's next register, with its connection dropped at a chosen point.
    Patches `DaemonClient` in the one module the arm builds its client from; `arm(point)`
    applies to the next register only:

    * `before_answer`: the client is closed before the register goes out;
    * `after_answer_restart`: the daemon answers and is stopped;
    * `after_answer_drop`: the daemon answers and the connection drops, the daemon
      staying up and dropping the binding;
    * `after_answer_stop`: the daemon answers and `on_answer` runs (the arm's `stop()`)
      before the register returns, the connection left open.

    After the answer the register returns only once the reader thread has run the
    arm's close handler, so the losing interleaving is the one under test.
    `fresh_daemon()` puts a new daemon on the socket and in the rig."""

    def __init__(self, monkeypatch, module, rig):
        self.rig = rig
        self.point = None
        self.on_answer = None
        self.fired = threading.Event()
        self.daemons: list = []
        #: Daemons whose `start()` has not returned. A register can be answered while
        #: boot is still running its tail (subscribe, rescan); stopping a daemon then
        #: races its own boot, so a stop waits for the boot to end. The arm is held
        #: inside its register meanwhile, so the window under test is unchanged.
        self.booting: set = set()
        #: Registers a forced point did not reach because they were not answered.
        self.unanswered: list = []
        drop = self
        real = module.DaemonClient

        class Forced(real):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.closed = threading.Event()
                inner = self.on_close

                def on_close():
                    try:
                        if inner is not None:
                            inner()
                    finally:
                        self.closed.set()
                self.on_close = on_close

            def register(self, **kw):
                point, drop.point = drop.point, None
                if point is None:
                    return super().register(**kw)
                if point == "before_answer":
                    try:
                        self.close()
                        return super().register(**kw)
                    finally:
                        drop.fired.set()
                try:
                    resp = super().register(**kw)
                except BaseException as e:
                    # Not answered (under load a fresh connection can be closed before
                    # its register goes out): that is the before-the-answer case, which
                    # the arm retries. The drop stays armed for the register that is.
                    drop.unanswered.append(repr(e))
                    drop.point = point
                    raise
                try:
                    if point == "after_answer_stop":
                        drop.on_answer()
                        return resp
                    if point == "after_answer_restart":
                        # the daemon stops after it
                        # answered, and the arm's main thread goes on only once the
                        # reader thread has run its close handler
                        daemon = drop.rig.daemon
                        assert wait_for(lambda: id(daemon) not in drop.booting, 10.0)
                        daemon.stop()
                    elif point == "after_answer_drop":
                        # the connection drops under a daemon that stays up; the daemon
                        # has dropped the binding before the arm goes on
                        self.close()
                    assert self.closed.wait(5.0), "the close handler never ran"
                    if point == "after_answer_drop":
                        q = resp["identity"]["qualified"]
                        assert wait_for(lambda: q not in drop.rig.daemon.bindings)
                    return resp
                finally:
                    drop.fired.set()

        monkeypatch.setattr(module, "DaemonClient", Forced)

    def arm(self, point, on_answer=None):
        self.fired.clear()
        self.on_answer = on_answer
        self.point = point

    def fresh_daemon(self):
        """A daemon on the same socket, in the rig before it accepts anything, so a drop
        inside its first register stops this one."""
        d = Daemon(self.rig.gateway, clock=self.rig.clock, start_trim_thread=False)
        self.rig.daemon = d
        self.daemons.append(d)
        self.booting.add(id(d))
        try:
            return d.start()
        finally:
            self.booting.discard(id(d))

    def stop_all(self):
        for d in self.daemons:
            d.stop()
