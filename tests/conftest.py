"""Test rig: everything runs in tmp dirs via comms' root overrides.

Nothing here touches a real HOME directory, and nothing touches a real record: the
gateway is the in-process mock, which is also the record.

The rig runs on TWO backends. The
`backend` fixture answers `mock` everywhere by default, and the suites named in
`BOTH_BACKENDS` below run every rig-using test twice, once against the mock and once
against a real `record.db` under the tmp STATE root. The mock stays the default and the
in-memory double; `local` is there so the seam's second real implementation is
exercised by the same tests, not by a parallel set of its own.

The `env` fixture points `MOXI_COMMS_HOME` at the test's tmp home, so the config file,
the record, the store and every path read stay inside tmp. A test that needs settings
writes its own config file with `write_config(env, text)`.

The names in fixtures (`planner`, `reviewer`, `lead`, `proj-a`, `console@acme`) are
placeholders.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from moxi_comms import cli, config, cursors, machine_line, tokens as tokens_mod
from moxi_comms.adapters.claude_shim import ClaudeShim
from moxi_comms.claimfile import HOLDS
from moxi_comms.claimhold import CLAIM_SOCKET_ENV, ClaimServer
from moxi_comms.daemon import Daemon
from moxi_comms.local_gateway import LocalGateway
from moxi_comms.membership import SELF, root_of
from moxi_comms.mock_gateway import NO_MENTIONS, MockGateway
from moxi_comms.paths import daemon_socket, directory_db, tokens_dir
from moxi_comms.provision import NAMES, provision
from moxi_comms.scoping import project_key
from moxi_comms.sender import MalformedStamp, SenderIdentity, credential_for, sender_from_env
from moxi_comms.tokens import TokenStore


class FakeClock:
    """Injectable clock. Starts at wall time so record timestamps stay comparable."""

    def __init__(self, t: float | None = None):
        self.t = time.time() if t is None else t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


CLEAN_HOME_OPTION = "--clean-home"
CODEX_LIVE_OPTION = "--codex-live"


def pytest_addoption(parser):
    parser.addoption(CLEAN_HOME_OPTION, action="store_true", default=False,
                     help="also run the clean-home install test: build the wheel, "
                          "install it into a venv under a temp HOME, and run the daemon "
                          "and two OpenCode identities (a stub opencode) from it")
    parser.addoption(CODEX_LIVE_OPTION, action="store_true", default=False,
                     help="also run the live check of Codex's hooks: the real codex on "
                          "PATH, logged in; spends tokens")


def write_config(env, text: str) -> Path:
    """Write `text` as comms' config file under the roots `env` names, and return its path.

    `env` is the mapping the code under test reads (None: the process environment, which
    the `env` fixture has pointed at the test's tmp home). The file is replaced whole, a
    new inode each time, so moxi.config's cache sees every write.
    """
    path = config.config_path(env if env is not None else os.environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """All five roots under tmp. RUNTIME gets a SHORT path (unix socket name limit).

    `MOXI_COMMS_HOME` collapses the roots under the tmp home; the device and the runtime
    directory are set beside it, and every per-root override is cleared, so nothing a
    test does resolves outside tmp.
    """
    runtime = Path(tempfile.mkdtemp(prefix="cmx"))
    home = tmp_path / "home"
    monkeypatch.setenv("MOXI_COMMS_HOME", str(home))
    monkeypatch.setenv("MOXI_COMMS_DEVICE", "testbox")
    monkeypatch.setenv("MOXI_COMMS_RUNTIME_DIR", str(runtime))
    for root in ("DATA_HOME", "STATE_HOME", "CACHE_HOME", "CONFIG_HOME"):
        monkeypatch.delenv(f"MOXI_COMMS_{root}", raising=False)
    monkeypatch.delenv("MOXI_COMMS_PROJECT", raising=False)
    # RUNTIME/comms exists, as a login's runtime directory does: a rig's fake daemon
    # binds in it directly.
    (runtime / "comms").mkdir()
    # turns' roots too: a Claude adapter looks for endpoints under RUNTIME/turns, and the
    # doctor's turns rows sweep dead endpoints there, so neither may reach the box's own.
    monkeypatch.setenv("MOXI_TURNS_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MOXI_TURNS_CONFIG_HOME", str(home / "config"))
    for var in ("MOXI_TURNS_HOME", "MOXI_TURNS_COMMANDS"):
        monkeypatch.delenv(var, raising=False)
    # The comms env of whatever session is RUNNING the suite. Developing
    # comms from inside a comms-connected session is the normal case here, and a live
    # socket stamp would let a test that had deliberately pointed at nothing find the
    # real daemon. A test rig that can reach production is not a rig.
    # `MOXI_COMMS_TOKEN_STORE` and `MOXI_COMMS_OPERATOR` are on the list for the same
    # reason the sockets are: both name production. The first points the resolver chain
    # at the operator's REAL 0600 store, and the second turns any send in the suite into
    # the operator's lane. `MOXI_COMMS_GATEWAY`, `MOXI_COMMS_LOCAL_RECORD` and
    # `MOXI_COMMS_LOCAL_RETAIN_DAYS` override config keys, so a shell that sets one would
    # change what a test reads.
    # `CLAUDE_CODE_SESSION_ID` is the session RUNNING the suite, which every rig shim
    # would otherwise report as its own.
    # `MOXI_COMMS_CLAIM_SOCKET` names the running session's real claim socket, and
    # `MOXI_COMMS_COMMAND` the comms install its launcher ran.
    for var in ("MOXI_COMMS_SHIM_SOCKET", "MOXI_COMMS_SOCKET", "MOXI_COMMS_NAME",
                "MOXI_COMMS_SLOT", "MOXI_COMMS_SCOPE", "MOXI_COMMS_GLOBAL",
                "MOXI_COMMS_DIRECTORS", "MOXI_COMMS_TOKEN_STORE", "MOXI_COMMS_OPERATOR",
                "MOXI_COMMS_GATEWAY", "MOXI_COMMS_LOCAL_RECORD",
                "MOXI_COMMS_LOCAL_RETAIN_DAYS", "MOXI_COMMS_MOCK_GATEWAY",
                "CLAUDE_CODE_SESSION_ID", "MOXI_COMMS_CLAIM_SOCKET", "MOXI_COMMS_COMMAND"):
        monkeypatch.delenv(var, raising=False)
    # The cache clock (cold-sends). Every reader's root and every shim's reported root
    # are under tmp: `CLAUDE_CONFIG_DIR` is what a shim reports its session's transcripts
    # under and where the reader finds them (`projects/` in it). Outside `tmp_path`,
    # which some suites assert is left empty.
    claude_home = Path(tempfile.mkdtemp(prefix="cmx-claude"))
    (claude_home / "projects").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    # The cache module comms' seam reads through (`moxi_comms.cache`), and sessions under
    # it: their roots under tmp too, so a read takes no config and no drift record of the
    # user running the suite, and a daemon's learning store locks and writes in tmp.
    monkeypatch.setenv("MOXI_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("MOXI_SESSIONS_HOME", str(tmp_path / "sessions-home"))
    for module in ("CACHE", "SESSIONS"):
        for root in ("DATA_HOME", "STATE_HOME", "CACHE_HOME", "CONFIG_HOME", "RUNTIME_DIR"):
            monkeypatch.delenv(f"MOXI_{module}_{root}", raising=False)
    yield
    shutil.rmtree(runtime, ignore_errors=True)
    shutil.rmtree(claude_home, ignore_errors=True)


#: Suites that run on both backends. A module here has every one of its rig-using tests
#: parametrised; a test in it that does not take `rig` (and so has no `backend` in its
#: fixture closure) is left alone.
BOTH_BACKENDS = (
    "test_cli.py",                  # the CLI verbs, and the sender through them
    "test_delivery.py",             # the daemon's routing, delivery and replay
    "test_claims.py",               # claim / release, through the daemon
    "test_custody.py",              # the custody journal and the binding fence
    "test_directory_provision.py",  # provision against the seam
    "test_local_gateway.py",        # its rig tests state the no-op half on the mock too
    "test_audience.py",             # the audience mark, verb to frame
    "test_cold_sends.py",           # the cold-cache hold, `moxi comms cache`, --next-turn
    "test_cold_every_sender.py",    # THE RULE: the clock, the hold, the daemon's gate
    "test_cold_hold.py",            # a cold claude-shim recipient is posted and held
    "test_cache_clock.py",          # the codex and opencode clocks, drift, the free rows
    "test_posture.py",              # the reply posture and the single-reply refusal
    "test_posture_intent.py",       # posture on every new send, replies, open threads
    "test_old_marks.py",            # records with an older mark still parse
    "test_reply_enforcement.py",    # reply enforcement, mark to reminder (its)
    "test_slot_fix.py",             # keeps, the slot fallback, the bundle's binding
    "test_claim_ops.py",            # the claim socket's op classes and ops
    "test_claim_server.py",         # a binding held by a module's own process
    "test_claim_send.py",           # sending as the claim; ordinary sends unchanged
    "test_claim_adapters.py",       # the shim and codex as claim holders
    "test_console_identity.py",     # send-only identities: console@acme
    "test_owner.py",                # the owner read: a session's binding and awaiting
    "test_send_rule.py",            # no env path: every kind sends through its claim
    "test_attributed_read.py",      # the attributed_message read: author and recipients
    "test_send_check.py",           # send --check: send's refusals, nothing posted
    "test_names.py",                # one rule for names; retired identities
)


def pytest_generate_tests(metafunc):
    if "backend" not in metafunc.fixturenames:
        return
    module = getattr(metafunc, "module", None)
    if module is not None and Path(module.__file__).name in BOTH_BACKENDS:
        metafunc.parametrize("backend", ["mock", "local"], indirect=True)


@pytest.fixture
def backend(request):
    """Which gateway `rig` builds. `mock` unless a BOTH_BACKENDS suite parametrised it."""
    return getattr(request, "param", "mock")


def build_gateway(backend: str, clock):
    """One gateway, chosen by name. The record and the daemon always share one clock.

    The local one takes no `secret_reader`: provision is the writer of every minted
    token, so the rig lays the files down the way `moxi-comms-provision` does and the gateway
    reads them back through its own resolver chain. The stand-in that used to serve them
    out of memory is gone with it.
    """
    if backend == "local":
        return LocalGateway(clock=clock, poll_interval=0.02)
    return MockGateway(clock=clock)


def build_store(backend: str):
    """The rig's token store, or None on a backend that mints its tokens elsewhere.

    Rooted EXPLICITLY at `tokens_dir()` rather than resolved from the environment: the
    env fixture puts every root under tmp, and an explicit root means no `MOXI_COMMS_TOKEN_STORE`
    in the shell running the suite can point a test at the operator's real store.
    """
    return TokenStore(tokens_dir()) if backend == "local" else None


#: The verbs that post under the sender's identity.
SEND_VERBS = ("send", "reply")


def _stamp_of(environ) -> SenderIdentity | None:
    """The identity a launch stamp names, read as the send verb read it before the env
    path closed; None for no stamp or a malformed one (which stays a stamp alone)."""
    try:
        return sender_from_env(environ, project_key(env=environ))
    except MalformedStamp:
        return None


#: `Rig.shim`'s default: a new session id with a warm transcript.
WARM_SESSION = object()


@dataclass
class Rig:
    backend: str
    gateway: object
    daemon: Daemon
    clock: FakeClock
    prov: object
    store: object = None          # the token store on `local`, None on the mock
    shims: list = field(default_factory=list)
    #: qualified -> a `ClaimServer` holding that binding for a module's own process
    #: (the pattern a module's own loop uses), which `session_holding` sends through.
    holders: dict = field(default_factory=dict)

    @property
    def operator_token(self) -> str | None:
        """The operator holder's own credential, read back the way the send verb reads it.
        None on the mock, which provisions no such holder and guards
        nothing."""
        if self.store is None:
            return None
        return self.store.read_secret(
            tokens_mod.owner_for_role(tokens_mod.ROLE_OPERATOR),
            tokens_mod.OPERATOR_SECRET_ID)

    # -- the record ---------------------------------------------------------
    @property
    def names_channel(self) -> str:
        return self.prov.rooms[NAMES]

    def age_message(self, message_id, timestamp: str) -> None:
        """Backdate one row of the record, whichever backend is holding it.

        Aging mail in place is how the bounded-replay tests reach past `REPLAY_T`
        without a day going by, and the two records keep it in different places: the
        mock's is a list in memory, the local one is a table.
        """
        gateway = self.gateway
        txn = getattr(gateway, "_txn", None)
        if txn is not None:
            with txn(write=True) as st:
                for m in st.messages:
                    if m["id"] == str(message_id):
                        m["timestamp"] = timestamp
            return
        with gateway._write() as conn:
            conn.execute("UPDATE messages SET timestamp = ? WHERE id = ?",
                         (timestamp, int(message_id)))

    # -- senders ------------------------------------------------------------
    def post_from_slot(self, slot_id: int, recipients, text, *,
                       reply_to=None, display=None, audience=None):
        """Post a tagged message straight through a slot's #names webhook."""
        from moxi_comms import directory
        db = directory.connect(directory_db(), read_only=True)
        try:
            sender = SenderIdentity.for_slot(slot_id, display or f"slot-{slot_id}", "global")
            cred, _ = credential_for(self.gateway, db, sender)
        finally:
            db.close()
        body = machine_line.compose(recipients, text, reply_to=reply_to,
                                    audience=audience)
        return self.gateway.execute_webhook(cred, body, username=display or f"slot-{slot_id}",
                                            allowed_mentions=NO_MENTIONS)

    def slot_webhook_id(self, slot_id: int, room: str = NAMES) -> str:
        """Which webhook a slot's bundle owns in `room` -- so a test can assert on the
        credential a post actually spent, not just on the name it wore."""
        from moxi_comms import directory
        db = directory.connect(directory_db(), read_only=True)
        try:
            channel_id = directory.room_by_name(db, room)["channel_id"]
            return directory.slot_webhook(db, slot_id, channel_id)["webhook_id"]
        finally:
            db.close()

    def agent_webhook_id(self, name: str, scope: str, room: str = NAMES) -> str:
        """The standing identity's OWN webhook in `room` (the one a pin must not use)."""
        from moxi_comms import directory
        db = directory.connect(directory_db(), read_only=True)
        try:
            channel_id = directory.room_by_name(db, room)["channel_id"]
            return directory.agent_webhook(db, name, scope, channel_id)["webhook_id"]
        finally:
            db.close()

    def post_as_operator(self, recipients, text, *, reply_to=None, audience=None):
        """The operator's lane, holding the operator's own credential. On `local` a
        stored hash makes the token REQUIRED, so the rig presents it exactly as the send
        verb does; the mock takes the keyword and ignores it."""
        body = machine_line.compose(recipients, text, reply_to=reply_to,
                                    audience=audience)
        return self.gateway.post_as_user(self.prov.operator_user_id, self.names_channel,
                                         body, token=self.operator_token)

    # -- adapters -----------------------------------------------------------
    def shim(self, *, slot="next-free", name=None, scope=None, global_=False,
             project="proj-a", autoreconnect=False, backoff=None, start=True,
             directors=(), session_id=WARM_SESSION, **extra) -> ClaudeShim:
        """A claude-shim on the rig. By default its session is a new id with a WARM
        transcript (one assistant line, written now), so the daemon's cold-cache gate
        delivers to it as it would to a working agent for the next hour. A test of the
        gate itself passes `session_id=` and lays down what it needs; None leaves the
        shim to its environment (`CLAUDE_CODE_SESSION_ID`, which the rig clears), so it
        reports no session at all, as a shim built before cold-sends did."""
        injected: list = []
        status: list = []
        if session_id is WARM_SESSION:
            session_id = str(uuid.uuid4())
            write_transcript(session_id, age=0)
        s = ClaudeShim(slot=slot, name=name, scope=scope, global_=global_, project=project,
                       injector=injected.append, status_sink=status.append,
                       autoreconnect=autoreconnect, backoff=backoff, directors=directors,
                       clock=self.clock,   # the record and the catch-up age agree
                       session_id=session_id, **extra)
        s.digests = injected      # test visibility
        s.statuses = status
        if start:
            s.start()
        self.shims.append(s)
        return s

    # -- the CLI ------------------------------------------------------------
    def cli(self, argv, *, slot=None, name=None, scope=None, project="proj-a", env=None,
            gateway=None, stamp_only=False, clock=None):
        """Run one CLI verb in this process.

        `name`, `scope` and `slot` stamp the environment as a launcher does. A send verb
        (`send`, `reply`) with a stamp runs INSIDE a live session holding the stamped
        identity, because only such a session sends under it: the
        rig shim bound as that name, through its own claim socket, or, when no rig shim
        holds it, a session holding the claim `offline` on the stamped slot. Either way
        this process is a `self` root of that session for the one call. `stamp_only=True`
        sends from the stamp alone, which is refused. An `env` that brings its own claim
        socket or the operator's stamp is left as it is. `clock` is the verb's clock
        (`Context.clock`); None leaves it the wall clock."""
        environ = {"MOXI_COMMS_PROJECT": project}
        if slot is not None:
            environ["MOXI_COMMS_SLOT"] = str(slot)
        if name is not None:
            environ["MOXI_COMMS_NAME"] = name
        if scope is not None:
            environ["MOXI_COMMS_SCOPE"] = scope
        environ.update(env or {})
        if (not stamp_only and argv and argv[0] in SEND_VERBS
                and not environ.get(CLAIM_SOCKET_ENV) and not environ.get("MOXI_COMMS_OPERATOR")):
            stamped = _stamp_of(environ)
            if stamped is not None:
                with self.session_holding(stamped) as socket_path:
                    environ[CLAIM_SOCKET_ENV] = socket_path
                    return self._run_cli(argv, environ, gateway, clock)
        return self._run_cli(argv, environ, gateway, clock)

    @contextlib.contextmanager
    def session_holding(self, sender: SenderIdentity):
        """This process inside a live session holding `sender`, for one call: yields its
        claim socket. A started rig shim whose claim holds the name (bound or offline)
        is that session; otherwise a `ClaimServer` holding the claim `offline` on the
        stamped slot stands in for the session, and goes at the end of the call. A
        binding this process holds itself (`holders`) is sent through as it is."""
        held = self.holders.get(sender.qualified)
        if held is not None:
            yield str(held.socket_path)       # this process is its `self` root
            return
        me = root_of(os.getpid(), SELF)
        for s in reversed(self.shims):
            holder = s.claims
            claim = holder.claim or {}
            if claim.get("qualified") != sender.qualified or holder.state not in HOLDS:
                continue
            if holder._server is None:
                holder.listen()       # serves claim ops for the rest of the test
            with holder.lock:
                holder.roots.append(me)
                holder.save()
            try:
                yield str(holder.socket_path)
            finally:
                with holder.lock:
                    holder.roots = [r for r in holder.roots if r is not me]
                    holder.save()
            return
        server = ClaimServer(sender.name, sender.scope, kind="claude-shim",
                             slot=sender.slot_id if sender.slot_id is not None
                             else "next-free",
                             global_=sender.scope == "global")
        server.holder.ever_held = True
        server.set_offline()
        server.start()
        try:
            yield str(server.socket_path)
        finally:
            server.stop()

    def _run_cli(self, argv, environ, gateway, clock=None):
        # The verb's env is this process's (the env fixture's tmp roots) plus the stamps:
        # config reads the file under the roots ctx.env names, so stamps alone would
        # resolve the real home's config file.
        out = io.StringIO()
        ctx = cli.Context(gateway=gateway if gateway is not None else self.gateway,
                          db_path=directory_db(), socket_path=daemon_socket(),
                          env=dict(os.environ, **environ), out=out, clock=clock)
        code = cli.main(argv, ctx=ctx)
        return code, out.getvalue()


@pytest.fixture
def rig(backend):
    clock = FakeClock()
    gateway = build_gateway(backend, clock)
    store = build_store(backend)
    # On `local`, provision is the token writer: every bundle's token and
    # the operator's own land in their holder's 0600 file here, and nothing serves them
    # back but the store. On the mock, `store` is None and the mock's record holds them,
    # which is what it always did.
    prov = provision(
        gateway, directory_db(), slots=6,
        standing=[("main", "proj-a"), ("main", "proj-b"), ("herald", "global")],
        operator=("900001", "operator"), store=store,
    )
    daemon = Daemon(gateway, clock=clock, start_trim_thread=False).start()
    rig = Rig(backend=backend, gateway=gateway, daemon=daemon, clock=clock, prov=prov,
              store=store)
    yield rig
    for s in rig.shims:
        s.stop()
    daemon.stop()
    if rig.daemon is not daemon:
        # A test that stopped this daemon and put a fresh one in the rig
        # (`test_guards.test_shim_surfaces_comms_offline_and_reconnects`) used to leave
        # the replacement running: `_stop` never set, its event loop spinning on a closed
        # subscription for the rest of the session. Two threads is nothing to look at and
        # everything to feel -- the next CPU-bound test in the run measures the spin
        # instead of itself.
        rig.daemon.stop()
    gateway.close()


# --------------------------------------------------------------------------- helpers


def projects_root() -> Path:
    """The rig's Claude projects root (tmp): what rig shims report, and the reader's."""
    return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects"


def assistant_line(age: float, *, now: float | None = None, **extra) -> dict:
    """One transcript line that meets the cache clock's rule, `age` seconds old."""
    now = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - age)) + ".000Z"
    line = {"type": "assistant", "timestamp": stamp, "sessionId": "rig",
            "message": {"model": "claude-opus-5-5", "role": "assistant",
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}
    line.update(extra)
    return line


def warm_session() -> str:
    """A new session id with a warm transcript under the rig's root: a working agent,
    for a binding a test makes by hand (a bare register, a `ClaudeShim` of its own)."""
    sid = str(uuid.uuid4())
    write_transcript(sid, age=0)
    return sid


def clock_reads_warm(monkeypatch, *kinds):
    """Make the cache clock read `warm` for bindings of these adapter kinds, for a suite
    that tests an adapter's own mechanics past the daemon's cold-cache gate.

    The fakes write no rollout and no `opencode.db` for cache's codex and opencode
    clocks to read, so under THE RULE (cold-sends) the gate refuses live messages to
    them. The gate still runs on every delivery here; this only stands in for a clock
    that would have read warm. What the gate does with each kind is tested in
    `test_cold_every_sender.py`.
    """
    from moxi_comms import cache
    real = cache.clock_state

    def reads(adapter, session_id, **kw):
        if adapter in kinds:
            return cache.Clock(cache.WARM, age_seconds=0.0)
        return real(adapter, session_id, **kw)

    monkeypatch.setattr(cache, "clock_state", reads)


def cache_api():
    """cache's interface 1, as comms' seam loads it (`moxi.modules.load`), for a test that
    lays down what cache reads or reads what it decided. Skips when cache is not
    installed in the environment running the suite."""
    from moxi import modules

    found = modules.load("cache", interface=1)
    if found.state != "present":
        pytest.skip(f"the cache module is not present: {found.reason}")
    return found.api


def cache_absent(monkeypatch, state="absent"):
    """Make comms' seam find cache `absent` (or `broken`), as a box without it does."""
    from moxi import modules
    from moxi_comms import cache

    gone = modules.Found(name="cache", state=state,
                         reason="no distribution in this environment declares the Moxi "
                                "module 'cache'" if state == "absent" else "the rig broke it")
    monkeypatch.setattr(cache, "module", lambda: gone)


#: The send's `[cold-at:…]` token on line zero.
COLD_AT_RE = re.compile(r" \[cold-at:[0-9,-]+\]")


def no_cold_at(text: str) -> str:
    """`text` with line zero's `[cold-at:…]` token taken out. The verb writes it on every
    send without `[held]`, with times read off the clock, so a test that pins line zero's
    other bytes compares this; `tests/test_verb_held.py` pins the token itself."""
    head, sep, rest = text.partition("\n")
    return COLD_AT_RE.sub("", head, count=1) + sep + rest


def write_transcript(session_id: str, age: float | None = 0, *, root: Path | None = None,
                     lines: list | None = None, project: str = "rig") -> Path:
    """`<root>/<project>/<session_id>.jsonl`. With `lines`, exactly those (dicts are
    dumped, strings written as they are); otherwise one user line and, unless `age` is
    None, one assistant line `age` seconds old."""
    root = projects_root() if root is None else Path(root)
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if lines is None:
        lines = [{"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                  "message": {"role": "user", "content": "hello"}}]
        if age is not None:
            lines.append(assistant_line(age))
    path.write_text("".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n"
                            for ln in lines))
    return path


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def wait_for_queue(shim, count: int, timeout: float = 3.0):
    wait_until(lambda: len(shim.queue) >= count, timeout)
    return shim.queue


def cursor_of(qualified: str):
    return cursors.read(qualified)


@pytest.fixture
def frames(monkeypatch):
    """Every `activity` frame a harness adapter sends, with its identity's cursor as it
    stood when the frame went out (`cursor_then`)."""
    from moxi_comms.client import DaemonClient
    seen: list = []
    real = DaemonClient.notify

    def notify(self, op, **kw):
        if op == "activity":
            seen.append(dict(kw, cursor_then=cursor_of(kw.get("identity"))))
        return real(self, op, **kw)

    monkeypatch.setattr(DaemonClient, "notify", notify)
    return seen


def frame_kinds(frames, kind):
    """The frames of one `kind` (None: the no-kind activity)."""
    return [f for f in frames if f.get("kind") == kind]
# ---------------------------------------------------------------- release-point frames
# The queue's release points ride `activity` frames from the
# binding's own connection. These send them raw through an adapter's own daemon client,
# so a test of the daemon does not depend on which adapter code sends them.

def own_activity(adapter, **fields) -> None:
    """One `activity` frame on `adapter`'s own daemon connection, with its session
    report (session id, `projects_root`, `turned`) and `fields`. `adapter` is a
    ClaudeShim or a harness adapter."""
    client = getattr(adapter, "_client", None) or getattr(adapter, "_daemon", None)
    assert client is not None and client.connected, "the adapter is not connected"
    identity = adapter.identity
    if hasattr(adapter, "session_report"):
        report = adapter.session_report(register=False)
    else:
        report = {"turned": adapter.turned}
        if adapter.session_id:
            report["session_id"] = adapter.session_id
        if adapter.projects_root:
            report["projects_root"] = adapter.projects_root
    client.notify("activity", identity=identity, **{**report, **fields})


def send_turn(adapter, handled=(), unhandled=()) -> None:
    """`activity kind=turn` with `handled` and `unhandled`, as the adapters
    send it at every turn end."""
    own_activity(adapter, kind="turn", turned=True, handled=[str(i) for i in handled],
                 unhandled=[str(i) for i in unhandled])


def send_ready(adapter, source: str | None) -> None:
    """`activity kind=ready`, with `source` unless it is None (an older adapter)."""
    extra = {"source": source} if source is not None else {}
    own_activity(adapter, kind="ready", **extra)


def queued(daemon, qualified: str) -> set:
    """The ids with a row for `qualified` in the daemon's memory."""
    with daemon._lock:
        return set(daemon.queue.get(qualified, {}))


def record_rows(gateway, qualified: str | None = None) -> set:
    """The queue rows in the record: ids for `qualified`, or every (id, name)."""
    rows = {(str(mid), str(q)) for mid, q in gateway.queue_rows()}
    if qualified is None:
        return rows
    return {mid for mid, q in rows if q == qualified}
