"""The claim holder every adapter carries, its claim socket, and its ops.

Every adapter process is its session's claim holder: the Claude shim, moxi-comms-codex and
moxi-comms-opencode. It holds at most one CLAIM (a comms identity it may register). It writes
what it holds to its claim file (`claimfile`) before it answers anything, and serves a
local claim socket whose path it stamps into everything it spawns as
`MOXI_COMMS_CLAIM_SOCKET`.

**The token rule.** Each comms identity is a single token. At every instant it sits in
claim files, and at most one of them holds it in a form that may bind. It leaves a file
only when the daemon refuses it because a different connection holds the name, or when
the process that holds the file ends. A timeout, an unreachable daemon, or a refusal by
the adapter's own still-open connection never removes it.

**Who may call each op.** What a process may send as is answered only inside the
session that holds it (`claim`); what the session holds is answered to any local caller
and can never be sent as (`state`). There is no other op.

`moxi comms whoami` is here too (`whoami`), with the read of a session whose adapter is gone
(`dead_adapter_answer`).
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from . import claimfile
from .claimfile import BOUND, HOLDS, OFFLINE, STANDBY, WAITING
from .client import DaemonClient, DaemonError, DaemonOffline, DaemonTimeout
from .membership import DEFAULT_PROC, SELF, TREE, ProcView, Root, root_of, sessions_of
from .protocol import LineConn, ProtocolError

log = logging.getLogger("comms.claim")

#: The one variable every adapter stamps into what it spawns.
CLAIM_SOCKET_ENV = "MOXI_COMMS_CLAIM_SOCKET"

CLAIM_OPS = ("claim",)
STATE_OPS = ("state",)
OPS = CLAIM_OPS + STATE_OPS

#: The refusal any caller outside the socket's own session gets for `claim`.
NOT_INSIDE = ("this process is not inside the session that holds this socket's identity")


# --------------------------------------------------------------------------- lines

def send_line(record: dict | None) -> str | None:
    """Why a process inside this session cannot send, or None when it holds a claim
    that names an identity (`claimfile.held_identity`). The same line `moxi comms whoami`
    prints. A claim with no name holds no comms identity, in every state."""
    if record is None:
        return "this session holds no comms identity"
    state = record.get("state")
    if claimfile.held_identity(record):
        return None
    if state == WAITING:
        claim = record.get("claim") or {}
        name = claim.get('qualified') or 'this name'
        why = record.get("waiting_why")
        if why == "":
            return f"{name} is not bound to comms yet"
        if isinstance(why, str):
            return f"comms refused {name}: {why}"
        return f"{name} is held by another session"
    if record.get("lost"):
        return f"{record['lost']} is held by another session"
    return "this session holds no comms identity"


# --------------------------------------------------------------------------- peer

def peer_pid(sock: socket.socket) -> int | None:
    """The connecting process's pid (`SO_PEERCRED`)."""
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", creds)
        return pid
    except (OSError, AttributeError):
        return None


class _Conn:
    """What one socket connection holds: its caller."""

    def __init__(self, pid):
        self.pid = pid


# --------------------------------------------------------------------------- holder

class ClaimHolder:
    """One adapter's claim and file, and the ops on them. The adapter reports what its
    binding is (`bound`, `went_offline`, `name_refused`, `note_session`); the holder
    writes it to the claim file and answers the socket from it."""

    def __init__(self, *, kind: str, key: str, socket_path, lock=None, roots=(),
                 keying: str = "session", proc=None, launch_session=None):
        self.kind = kind
        self.key = key
        self.socket_path = Path(socket_path)
        self.lock = lock or threading.RLock()
        self.proc: ProcView = proc or DEFAULT_PROC
        self.keying = keying
        self.roots: list = list(roots)
        # the record
        self.state = OFFLINE
        self.claim: dict | None = None
        self.hold = 0
        self.launch_session = launch_session
        #: The port's running harness session (P4): its id; "" when the holder knows it
        #: runs none; None while it is not known (a port that has not noted one yet). A record with no key at
        #: all, or null, reads as not known.
        self.running_session: str | None = None
        self.exact_slot = False
        self.lost: str | None = None
        self.ever_held = False
        #: Why a `waiting` claim waits, when it is not that another session holds its
        #: name (`send_line`): "" while it has been neither bound nor refused, else the
        #: daemon's words for a refusal that names no holder. None (null, or no key):
        #: another session holds the name. Only a `ClaimServer` sets it (s126.1).
        self.waiting_why: str | None = None
        #: One register in flight (T4): the adapter's supervisor's.
        self.register_lock = threading.Lock()
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        #: Closed with its file removed (`close()`): `save` writes nothing from then on,
        #: so a late event (a harness's reader thread running `went_offline` after its
        #: `stop()`) cannot put back the file. A shim's `close(remove_file=False)` keeps
        #: its file, and its writes.
        self._removed = False
        self._threads: list = []

    # ------------------------------------------------------------------ the record
    def record(self) -> dict:
        return {"kind": self.kind, "key": self.key, "keying": self.keying,
                "socket": str(self.socket_path), "state": self.state,
                "claim": self.claim, "hold": self.hold,
                "roots": [r.as_dict() for r in self.roots],
                "launch_session": self.launch_session,
                "running_session": self.running_session,
                "exact_slot": self.exact_slot, "lost": self.lost,
                "ever_held": self.ever_held, "waiting_why": self.waiting_why}

    def save(self) -> None:
        with self.lock:
            if self._removed:
                return
            claimfile.write(self.key, self.record())

    def load(self, record: dict) -> None:
        """Come up in a recorded state (a respawned shim inside its session). A state
        this holder does not know (an old file's `passing`) comes up `standby`."""
        state = record.get("state")
        self.claim = record.get("claim") or None
        self.hold = int(record.get("hold") or 0)
        self.running_session = record.get("running_session")
        self.exact_slot = bool(record.get("exact_slot"))
        self.lost = record.get("lost")
        self.ever_held = bool(record.get("ever_held"))
        if record.get("launch_session"):
            self.launch_session = record["launch_session"]
        # A held claim comes back offline: its connection went with the old process.
        self.state = OFFLINE if state in HOLDS else (state if state in claimfile.STATES
                                                    else STANDBY)

    def start_launch(self, claim: dict) -> None:
        """A launch with no file of its own: `offline` with the launch claim."""
        with self.lock:
            self.claim = dict(claim)
            self.state = OFFLINE
            self.hold = 1
            self.save()

    # ------------------------------------------------------------------ adapter events
    @property
    def holds(self) -> bool:
        return self.state in HOLDS and self.claim is not None

    def may_register(self) -> bool:
        """Whether the adapter's supervisor may register now: an offline or waiting
        claim."""
        return self.claim is not None and self.state in (OFFLINE, WAITING)

    def register_claim(self) -> tuple:
        """(claim, exact_slot) for the adapter's next register."""
        with self.lock:
            return dict(self.claim or {}), self.exact_slot

    def bound(self, resp: dict) -> None:
        """A register of the claim was accepted."""
        with self.lock:
            if self.claim is None:
                return
            ident = resp.get("identity") or {}
            self.claim.update(qualified=ident.get("qualified", self.claim.get("qualified")),
                              tier=ident.get("tier"), slot=resp.get("slot"),
                              name=ident.get("name", self.claim.get("name")),
                              scope=ident.get("scope", self.claim.get("scope")))
            self.state = BOUND
            self.exact_slot = False
            if self.lost == self.claim.get("qualified"):
                self.lost = None           # a name lost and bound again; another stays said
            self.ever_held = True
            self.save()

    def went_offline(self) -> None:
        with self.lock:
            if self.state == BOUND:
                self.state = OFFLINE
                self.save()

    def name_refused(self, holder: str | None = None) -> None:
        """A register refused because a different connection holds the name. A claim
        this adapter has held leaves it (`standby` with `lost`); a launch claim never
        held keeps waiting."""
        with self.lock:
            if self.claim is None:
                return
            if not self.ever_held:
                self.state = WAITING
                self.save()
                return
            self.lost = holder or self.claim.get("qualified")
            self.claim = None
            self.state = STANDBY
            self.save()

    def note_session(self, session_id: str | None) -> None:
        """The port's running harness session (P4)."""
        with self.lock:
            if session_id != self.running_session:
                self.running_session = session_id
                self.save()

    def hold_mark(self) -> tuple | None:
        """(qualified, hold) for an injection made now, or None with no claim."""
        with self.lock:
            if not self.holds:
                return None
            return self.claim.get("qualified"), self.hold

    def may_advance(self, mark) -> bool:
        """P5: a cursor moves only for the name its digest was injected under, in the
        same hold, while the adapter still holds it."""
        with self.lock:
            return (mark is not None and self.holds
                    and (self.claim.get("qualified"), self.hold) == tuple(mark))

    # ------------------------------------------------------------------ reads
    def claim_answer(self) -> dict:
        with self.lock:
            claim = dict(self.claim) if self.claim else None
            return {"ok": True, "state": self.state, "claim": claim,
                    "hold": self.hold, "launch_session": self.launch_session,
                    "kind": self.kind, "key": self.key,
                    "line": send_line(self.record())}

    def state_answer(self) -> dict:
        with self.lock:
            return {"ok": True, "state": self.state, "kind": self.kind,
                    "key": self.key,
                    "claim": (self.claim or {}).get("qualified") if self.holds else None,
                    "claim_name": (self.claim or {}).get("qualified"),
                    "lost": self.lost}

    # ------------------------------------------------------------------ membership
    def caller_sessions(self, pid) -> list:
        if pid is None:
            return []
        records = claimfile.live_records(self.proc)
        # This holder's own file is live by construction while it serves.
        if not any(k == self.key for k, _r in records):
            records.append((self.key, self.record()))
        return sessions_of(pid, claimfile.membership_table(records), self.proc)

    # ------------------------------------------------------------------ the socket
    def listen(self) -> None:
        """Serve the claim socket on its own thread (harness adapters, ClaimServer)."""
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o600)
        srv.listen(16)
        srv.settimeout(0.2)
        self._server = srv
        self._spawn(self._accept_loop)

    def _spawn(self, target) -> None:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        self._threads.append(t)

    def close(self, *, remove_file: bool = True) -> None:
        # Set under the lock every write takes: one that took it first has written
        # before the file is removed, and one after it writes nothing.
        with self.lock:
            self._stop.set()
            self._removed = self._removed or remove_file
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self.socket_path.unlink(missing_ok=True)
            self._server = None
        if remove_file:
            claimfile.remove(self.key)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            pid = peer_pid(sock)
            threading.Thread(target=self.serve, args=(LineConn(sock), pid),
                             daemon=True).start()

    def serve(self, conn: LineConn, pid, first: dict | None = None) -> None:
        """One connection: frames until EOF."""
        ctx = _Conn(pid)
        try:
            frame = first if first is not None else conn.recv()
            while frame is not None:
                conn.send(self.dispatch(frame, ctx))
                frame = conn.recv()
        except (ProtocolError, OSError):
            pass
        finally:
            conn.close()

    def dispatch(self, frame, ctx: _Conn | None = None) -> dict:
        ctx = ctx or _Conn(os.getpid())
        if not isinstance(frame, dict):
            return {"ok": False, "error": "a claim frame is a JSON object"}
        op = frame.get("op")
        if op not in OPS:
            return {"ok": False, "error": f"unknown claim op {op!r}; expected one of "
                                          + ", ".join(OPS)}
        try:
            if op == "state":
                return self.state_answer()
            if self.key not in self.caller_sessions(ctx.pid):
                return {"ok": False, "error": NOT_INSIDE, "not_inside": True}
            return self.claim_answer()
        except Exception as e:                                   # noqa: BLE001
            log.exception("claim op %s failed", op)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- client

class ClaimConn:
    """A connection to a claim socket: frames in order, one answer each."""

    def __init__(self, path, timeout: float = 15.0):
        self.path = str(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(self.path)
        self.conn = LineConn(sock)

    def call(self, op: str, **fields) -> dict:
        self.conn.send({"op": op, **fields})
        answer = self.conn.recv()
        if answer is None:
            raise ProtocolError("the claim socket closed without answering")
        return answer

    def close(self) -> None:
        self.conn.close()


def call(path, op: str, timeout: float = 15.0, **fields) -> dict:
    c = ClaimConn(path, timeout=timeout)
    try:
        return c.call(op, **fields)
    finally:
        c.close()


def socket_from_env(env=None) -> Path | None:
    """The claim socket `env` (else this process) names, or None when it names none.

    A path outside comms' runtime directory raises `paths.OutsideRuntime`, naming the
    variable and the directory: a socket stamped by another copy of comms, or by hand,
    is never asked who this process is. The directory is this process's own; a stamp
    cannot move it.
    """
    from .paths import refuse_outside_runtime

    value = (env if env is not None else os.environ).get(CLAIM_SOCKET_ENV)
    return refuse_outside_runtime(value, CLAIM_SOCKET_ENV) if value else None


# --------------------------------------------------------------------------- whoami

def whoami(env=None, say=print, opener=None, proc=None) -> int:
    """`moxi comms whoami`: what this session holds, `<qualified> (<state>, slot <slot>)`,
    or the line a send would refuse with; then `session <id>` when the claim records
    its launch session. 0 when it holds an identity, 2 when it holds none."""
    from .paths import OutsideRuntime

    env = env if env is not None else os.environ
    opener = opener or ClaimConn
    try:
        path = socket_from_env(env)
    except OutsideRuntime as e:
        say(f"{e} Nothing was asked.")
        return 2
    if not path:
        say("this process has no claim socket: it is inside no comms session and holds "
            "no comms identity")
        return 2
    try:
        conn = opener(path)
        try:
            answer = conn.call("claim")
        finally:
            conn.close()
    except (OSError, ProtocolError):
        answer = dead_adapter_answer(path, proc=proc)
        if answer is None:
            say("this session's comms adapter is gone and it holds no comms identity")
            return 2
    if not answer.get("ok"):
        say(answer.get("error"))
        return 2
    claim = answer.get("claim") or {}
    line = answer.get("line")
    if line is None:
        say(f"{claim.get('qualified')} ({answer.get('state')}, slot {claim.get('slot')})")
    else:
        say(line)
    if answer.get("launch_session"):
        say(f"session {answer['launch_session']}")
    return 0 if line is None else 2


def dead_adapter_answer(path, proc=None, pid=None) -> dict | None:
    """The claim file of a session whose adapter is gone, read for a caller inside that
    session: an answer shaped like `claim`'s, or None when there is no live file whose
    session the caller is inside."""
    proc = proc or DEFAULT_PROC
    pid = os.getpid() if pid is None else pid
    records = claimfile.all_records()
    mine = [(k, r) for k, r in records if r.get("socket") == str(path)]
    if not mine:
        return None
    key, record = mine[0]
    if not claimfile.tree_root_alive(record, proc):
        return None
    live = [(k, r) for k, r in records if claimfile.is_live(r, proc)]
    if key not in sessions_of(pid, claimfile.membership_table(live), proc):
        return None
    state = record.get("state")
    claim = record.get("claim") if claimfile.held_identity(record) else None
    return {"ok": True, "state": OFFLINE if claim else state, "claim": claim,
            "launch_session": record.get("launch_session"), "kind": record.get("kind"),
            "key": key, "line": send_line(record) if not claim else None,
            "dead_adapter": True}


# --------------------------------------------------------------------------- ClaimServer

_GATE = ("import os,sys\n"
         "fd=int(sys.argv[1]); os.read(fd,1); os.close(fd)\n"
         "os.execvp(sys.argv[2], sys.argv[2:])\n")


class ClaimServer:
    """A claim socket for a binding held by a module's own process (its own loop, a
    send-only console). Its roots are the holder, reach `self`, and any child roots the
    holder adds for this binding. It answers `claim` inside its session and `state` to
    any local caller.

    The holder reports what its binding is (`set_bound`, `set_offline`, `set_waiting`),
    or gives the server its own daemon arm (`arm=True`): it registers at `start`, goes
    `offline` and registers again across a daemon restart, holds nothing after a
    refusal (logged once), and closes the binding at `stop`.
    """

    def __init__(self, name: str, scope: str, *, kind: str, slot="next-free",
                 directors=(), socket_path=None, arm: bool = False, daemon_socket=None,
                 proc=None, global_: bool = False, backoff=None):
        from .paths import runtime_root
        from .scoping import GLOBAL, qualify

        self.proc = proc or DEFAULT_PROC
        me = os.getpid()
        start = self.proc.start_time(me) or "0"
        global_ = global_ or scope == GLOBAL
        self.claim = {"name": name, "scope": GLOBAL if global_ else scope,
                      "global": global_, "qualified": qualify(name, GLOBAL if global_
                                                              else scope),
                      "slot": slot, "tier": None, "directors": list(directors)}
        key = f"srv-{me}-{start}-{self.claim['qualified']}"
        path = socket_path or runtime_root() / f"comms-claimsrv-{me}-{abs(hash(key)) % 10**8}.sock"
        self.holder = ClaimHolder(kind=kind, key=key, socket_path=path, proc=self.proc,
                                  keying="server", roots=[Root(me, start, SELF)])
        self.holder.claim = dict(self.claim)
        self.holder.state = WAITING
        self.holder.waiting_why = ""            # not bound yet, and nothing refused it
        self.arm = arm
        #: The slot the holder asked for, which is what every register asks: `next-free`
        #: unless the holder pinned a number. Never the slot a bind answered, which the
        #: claim keeps for the send verb: re-asking that by number after a daemon
        #: restart would turn a free choice into a pin another name can squat on. A
        #: `next-free` register gets the slot kept for this name back anyway.
        self.asked = slot if slot is not None else "next-free"
        self.daemon_socket = daemon_socket
        self.kind = kind
        self._client: DaemonClient | None = None
        self._stop = threading.Event()
        self._backoff = backoff
        self.refusal: str | None = None
        self._children: dict = {}

    @property
    def socket_path(self) -> Path:
        return self.holder.socket_path

    def env(self) -> dict:
        return {CLAIM_SOCKET_ENV: str(self.socket_path)}

    # holder-reported state. Each is a no-op once `stop()` has run: the file went with
    # the socket, and a late report would write it back as a dead `srv-` file (s126.4).
    def set_bound(self, slot=None, tier=None) -> None:
        with self.holder.lock:
            if self._stop.is_set():
                return
            self.holder.claim.update(slot=slot if slot is not None else
                                     self.holder.claim.get("slot"), tier=tier)
            self.holder.state = BOUND
            self.holder.ever_held = True
            self.holder.save()

    def set_offline(self) -> None:
        with self.holder.lock:
            if self._stop.is_set():
                return
            self.holder.state = OFFLINE if self.holder.ever_held else WAITING
            self.holder.save()

    def set_waiting(self, refusal: str | None = None) -> None:
        """The claim holds nothing. `refusal`: the daemon's words when it refused the
        register for a reason other than another session holding the name, which the
        send verb then says; without them it says the name is held elsewhere."""
        with self.holder.lock:
            if self._stop.is_set():
                return
            self.holder.state = WAITING
            self.holder.waiting_why = refusal
            self.holder.save()

    # roots. After `stop()` neither writes, as the reports above: a `spawn` racing a
    # service's stop gets no root, so its child meets no socket and its send fails.
    def add_root(self, pid: int, reach: str = TREE) -> Root | None:
        root = root_of(pid, reach, self.proc)
        if root is None:
            return None
        with self.holder.lock:
            if self._stop.is_set():
                return None
            self.holder.roots.append(root)
            self.holder.save()
        return root

    def drop_root(self, pid: int) -> None:
        with self.holder.lock:
            if self._stop.is_set():
                return
            self.holder.roots = [r for r in self.holder.roots
                                 if not (r.pid == pid and r.reach == TREE)]
            self.holder.save()

    def spawn(self, argv, *, env=None, **popen) -> subprocess.Popen:
        """Start a child that is a `tree` root of this binding, written to the claim
        file before the child's program runs, and dropped at its exit. The child's
        environment carries this binding's claim socket."""
        child_env = dict(env if env is not None else os.environ)
        child_env[CLAIM_SOCKET_ENV] = str(self.socket_path)
        r, w = os.pipe()
        try:
            proc = subprocess.Popen([sys.executable, "-c", _GATE, str(r), *argv],
                                    env=child_env, pass_fds=(r,), **popen)
        finally:
            os.close(r)
        try:
            self.add_root(proc.pid, TREE)
        finally:
            os.write(w, b"x")
            os.close(w)

        def reap():
            proc.wait()
            self.drop_root(proc.pid)
        threading.Thread(target=reap, daemon=True).start()
        return proc

    # lifecycle
    def start(self) -> "ClaimServer":
        self.holder.save()
        self.holder.listen()
        if self.arm:
            self._register_once()
            threading.Thread(target=self._supervise, daemon=True).start()
        return self

    def stop(self) -> None:
        # Set under the holder's lock: a report that takes the lock after this sees the
        # stop, and one that took it first has written before the file is removed.
        with self.holder.lock:
            self._stop.set()
            client = self._client
        if client is not None:
            client.close()
        self.holder.close()

    def _register_once(self) -> bool:
        c = self.holder.claim
        client = DaemonClient(self.daemon_socket, on_close=self._on_close)
        try:
            client.connect()
            try:
                resp = self._register(client, c)
            except DaemonError as e:
                if not self._slot_refusal(e, c):
                    raise
                # A pinned slot another name holds: bind wherever the daemon gives,
                # as the Claude shim does, and ask `next-free` from then on.
                log.warning("slot %s was refused for %s (%s); registering next-free",
                            self.asked, c["qualified"], e)
                self.asked = "next-free"
                resp = self._register(client, c)
        except (DaemonOffline, DaemonTimeout):
            client.close()
            self.set_offline()
            return False
        except DaemonError as e:
            client.close()
            if self.refusal != str(e):
                log.warning("the daemon refused %s: %s", c["qualified"], e)
            self.refusal = str(e)
            response = getattr(e, "response", None) or {}
            held = (response.get("holder") or {}).get("qualified") == c["qualified"]
            self.set_waiting(None if held else self.refusal)
            return False
        # The arm reads `bound` only while the connection its register was answered on
        # is open, and a drop at any point after the answer sends it back to register.
        # The reader thread marks the client closed BEFORE it runs `_on_close`, and
        # `_on_close` takes this lock, so the check and `set_bound` under it make the
        # ordering total: either the close landed first and this declines the bind, or
        # `_on_close` runs after it and reads the binding offline. Unchecked, a close
        # between the answer and `set_bound` was overwritten with `bound` and the
        # supervisor slept on a binding the daemon no longer held (s113.1, as the
        # Claude shim's `_connect_once` guards it).
        with self.holder.lock:
            if self._stop.is_set():
                # A bind must not outlive `stop()`, as the Claude shim declines it: the
                # daemon would hold it until the process exits (s125.1).
                client.close()
                return False
            if not client.connected:
                client.close()
                self.set_offline()
                return False
            self._client = client
            self.refusal = None
            self.set_bound(resp.get("slot"), (resp.get("identity") or {}).get("tier"))
        return True

    def _register(self, client, c) -> dict:
        return client.register(slot=self.asked, name=c["name"], scope=c["scope"],
                               global_=c.get("global", False), project=c["scope"],
                               adapter=self.kind)

    def _slot_refusal(self, error, c) -> bool:
        """A refusal of a numbered register that is not about this binding's own name:
        the slot is held by another name, or not provisioned."""
        if self.asked == "next-free":
            return False
        response = getattr(error, "response", None) or {}
        if not response.get("refused") or response.get("transient"):
            return False
        holder = (response.get("holder") or {}).get("qualified")
        return holder != c["qualified"]

    def _on_close(self) -> None:
        if not self._stop.is_set():
            self.set_offline()

    def _supervise(self) -> None:
        from .client import backoff_delays
        delays = self._backoff or backoff_delays()
        while not self._stop.is_set():
            if self.holder.state in HOLDS and self.holder.state == BOUND:
                self._stop.wait(0.2)
                continue
            if self._register_once():
                delays = self._backoff or backoff_delays()
                continue
            self._stop.wait(next(delays))


__all__ = ["CLAIM_OPS", "CLAIM_SOCKET_ENV", "ClaimConn", "STAMP_VARS", "claim_overlay",
           "ClaimHolder", "ClaimServer", "NOT_INSIDE", "OPS", "STATE_OPS", "call",
           "dead_adapter_answer", "peer_pid", "send_line", "socket_from_env", "whoami"]


STAMP_VARS = ("MOXI_COMMS_NAME", "MOXI_COMMS_SCOPE", "MOXI_COMMS_GLOBAL", "MOXI_COMMS_SLOT")


def claim_overlay(env) -> dict:
    """`env` as a reader of the launch stamp should see it: with a claim socket, the
    stamp's identity variables are replaced by what this session's claim holds now (or
    removed when it holds nothing, or the caller is not inside it), so `whois`,
    `explain`, `cache` and the roster hook read the claim, never a stale stamp. With no
    socket, `env` unchanged. A socket outside comms' runtime directory is asked
    nothing, and the stamp is removed: this session holds nothing comms can read."""
    from .paths import OutsideRuntime

    if not env.get(CLAIM_SOCKET_ENV):
        return env
    out = {k: v for k, v in env.items() if k not in STAMP_VARS}
    try:
        path = socket_from_env(env)
    except OutsideRuntime as e:
        log.warning("%s", e)
        return out
    try:
        answer = call(path, "claim", timeout=5.0)
    except (OSError, ProtocolError):
        answer = dead_adapter_answer(path)
    if not answer or not answer.get("ok") or answer.get("line") is not None:
        return out
    claim = answer.get("claim") or {}
    if not claim.get("name"):
        return out
    out["MOXI_COMMS_NAME"] = claim["name"]
    if claim.get("global"):
        out["MOXI_COMMS_GLOBAL"] = "1"
    out["MOXI_COMMS_SCOPE"] = claim.get("scope") or ""
    if isinstance(claim.get("slot"), int):
        out["MOXI_COMMS_SLOT"] = str(claim["slot"])
    return out
