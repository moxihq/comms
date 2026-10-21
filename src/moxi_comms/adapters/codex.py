"""`moxi-comms-codex` -- comms' Codex adapter, one launcher process per identity.

This lives OUTSIDE the daemon: the launcher is an **ordinary daemon-socket client**, and
**its socket connection is the liveness anchor -- its death is its deregistration**. The
daemon stays a pure router with no child supervision.

The Codex session itself is turns': the launcher calls `turns.codex.launch`, and turns'
host spawns and supervises `codex app-server`, holds the WebSocket link, starts and
resumes the thread, wires Codex's own hooks (`moxi-turns-ping`) with their per-launch
trust, delivers steer-first, opens the window and reaps every child when the launch ends.
comms reaches the session only through turns' public API. What this process owns:

  * one registration on the daemon socket, with the slot the roster PINS (never
    next-free: the session's environment is stamped at launch, before any register
    could answer), and the claim socket it stamps into that environment;
  * what comms adds to the launch: the identity stamp in the environment, layer one as
    the thread's developer instructions, and the roster's Codex thread options. turns
    passes them on and reads none of them;
  * the queue, the digest, the cursor and the owed ledger. Each digest goes in with
    `deliver(steer=True)`, comms' steer-first: a running turn takes it at once, an idle
    thread gets a new turn. Its ids settle at that turn's end, from the session's own
    turn events (`watch`): `handled` when the turn ended `answer`, `unhandled` on
    `aborted`, `error` or `refusal`, and `unhandled` too for a delivery turns answered
    `Lost`. The cursor advances there, never past an id still held, and the `turn`
    frame follows the write;
  * the holder: at each Stop turns asks `on_stop`, which runs the shim's reply check
    (`HarnessAdapter.reply_check`) and answers the reminder text, which holds the turn
    open, or None. turns gives the Stop's facts, the thread's running background
    terminals among them; Codex has no crons.

The digest itself is `claude_shim`'s `render_digest` / `frame_line` -- one attributed
frame line per message, the routing line already stripped by the daemon -- so the model
sees the same thing on both harnesses, directive standing for the roster's declared
`directors` included.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import codex_threads
from ..client import DaemonClient, DaemonError, DaemonOffline
from ..config import ConfigError
from ..context import BARE_COMMS, MOXI_COMMS_PATH_LINE, own_comms
from . import codex_roster
from .harness import (REQUEUE, SOURCE_COMPACT, SOURCE_RESUME, SOURCE_STARTUP,
                      HarnessAdapter, launch_stamp)

log = logging.getLogger("comms.codex")

ADAPTER_KIND = "codex"
BINARY = "codex"

#: How long a digest waits for turns to say which turn took it. turns' host answers
#: within a few seconds; one it has not answered by then is counted as not known to
#: have gone in, and its ids are `unhandled` on the next `turn` frame.
STARTED_WAIT = 30.0
FLUSH_RETRY = 1.0
#: How many delivery ids whose turn ended before the adapter recorded them it keeps.
EARLY_KEPT = 1024
#: A turn outcome that settles its digests `handled`.
ANSWER = "answer"
#: The reason turns gives when the person closes a Codex window (its prefix).
WINDOW_CLOSED = "the Codex window closed"
EXIT_GAVE_UP = 3


# --------------------------------------------------------------------------- layer one
# The line naming which comms to run (`MOXI_COMMS_PATH_LINE`), the bare fallback
# (`BARE_COMMS`) and `own_comms` are `context`'s, one copy for every harness.


def layer_one_text(roster_info, comms: str = BARE_COMMS, *, directors=()) -> str:
    """Layer one for a Codex thread: the context block, then the line naming the
    `comms` to use, then the labeled roster snapshot (or the offline line).

    The block is `context.layer_one_block` for the agent's declared `directors`, and the
    roster is `roster_view`'s one rendering, as for a Claude session, so the two
    harnesses read the same text."""
    from ..context import layer_one_block
    from ..roster_view import render_agents
    from .shim_roster import LABEL, OFFLINE, usable

    if usable(roster_info):
        roster = f"{LABEL}\n{render_agents(roster_info)}"
    else:
        roster = OFFLINE
    return (f"{layer_one_block(directors)}\n\n{MOXI_COMMS_PATH_LINE.format(comms=comms)}"
            f"\n\n{roster}")


def layer_one_hash(comms: str = BARE_COMMS, *, directors=()) -> str:
    """What the thread file records of the layer one a thread was given: a hash of its
    fixed part, the context block (with the director sentence when the agent declares a
    director) and the comms path line. The roster under them is a snapshot and changes
    every launch, so it is not part of the hash."""
    import hashlib

    from ..context import layer_one_block

    text = f"{layer_one_block(directors)}\n\n{MOXI_COMMS_PATH_LINE.format(comms=comms)}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def fetch_roster(socket_path=None, timeout: float = 2.0):
    """The daemon's `agents` answer for layer one, or None when it does not answer."""
    try:
        client = DaemonClient(socket_path, timeout=timeout).connect()
        try:
            return client.query("agents")
        finally:
            client.close()
    except Exception as e:                                  # noqa: BLE001
        log.warning("roster for layer one unavailable: %s", e)
        return None


# --------------------------------------------------------------------------- the install
def binary_version(binary: str, env=None, timeout: float = 15.0) -> str:
    """What `<binary> --version` prints, or why it printed nothing."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown ({e})"
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0] if text else f"unknown (exit {out.returncode})"


def _turns_launch(cwd, *, instructions=None, env=None, window=True, options=None,
                  verify=None):
    """turns' Codex host. Imported at the call: `import moxi_turns` loads nothing."""
    import moxi_turns as turns

    return turns.codex.launch(cwd, instructions=instructions, env=env, window=window,
                              options=options, verify=verify)


def scrub_own_environ(stamped: dict, environ=None) -> None:
    """turns' host builds the session's environment as this process's own with the
    launch's `env` over it, so a comms stamp the identity does not carry must not be
    in this process's environment either: an agent that declared no directors would
    otherwise inherit the launcher's `MOXI_COMMS_DIRECTORS`."""
    from .session import INHERITED_COMMS_ENV

    environ = os.environ if environ is None else environ
    for var in INHERITED_COMMS_ENV:
        if var not in stamped:
            environ.pop(var, None)


class CodexAdapter(HarnessAdapter):
    """One identity: the daemon binding, the queue, the cursor and the holder, over the
    turns session of the thread this launch started."""

    KIND = ADAPTER_KIND
    STATE_NOUN = "thread-id file"
    QUIET_OFFLINE_ON_STOP = True   # a launcher stopping closed it: nothing to say
    HOLDS_UNTIL_TURN_ENDS = True   # a digest's ids are held until its turn ends
    log = log

    def __init__(self, agent: codex_roster.CodexAgent, *, socket_path=None, launch=None,
                 window: bool = False, autoreconnect: bool = True, backoff=None,
                 status_sink=None, clock=time.time, write_root=...,
                 comms_path: str | None = None, layer_one: bool = True, exit_hook=None,
                 started_wait: float = STARTED_WAIT):
        super().__init__(agent, socket_path=socket_path, autoreconnect=autoreconnect,
                         backoff=backoff, status_sink=status_sink, clock=clock,
                         exit_hook=exit_hook)
        #: `turns.codex.launch`, or what a test hands in its place.
        self._launch = launch or _turns_launch
        self.window = window
        self.started_wait = started_wait
        self._write_root = write_root
        self.write_root_reason = "given by the caller"
        self.comms_path = comms_path or BARE_COMMS
        self.layer_one = layer_one

        #: The turns session of this launch's thread, once launched.
        self.session = None
        self.thread_id: str | None = None
        self.turn_open = False
        self.active_turn_id: str | None = None
        self._hold = None
        self._watcher: threading.Thread | None = None
        #: Why the session ended (`Gone`), once it has.
        self.gone_reason: str | None = None
        self.gone = threading.Event()
        #: Each delivery's digest ids, by the delivery id turns minted, until its turn
        #: ends; and the outcome of a delivery whose turn ended before it was recorded.
        self._deliveries: dict[str, tuple] = {}
        self._ended_early: collections.OrderedDict = collections.OrderedDict()
        #: The turn the reply reminder holds open, while it does.
        self._blocked_turn: str | None = None
        #: Whether a turn of this launch's thread has ended (the cache clock's `turned`).
        self._own_turned = False
        #: Which launch wrote the thread record: `<pid>@<UTC start>`.
        self.launch = launch_stamp()
        #: What the thread record says the thread was born with (layer one's hash).
        self._born_with: dict = {}

    # ------------------------------------------------------------------ the clock's evidence
    def cache_source(self) -> str | None:
        """Where Codex writes rollouts: `$CODEX_HOME/sessions`, else
        `~/.codex/sessions`, in the environment the session gets."""
        env = self.agent.env()
        home = env.get("CODEX_HOME")
        if home:
            return str(Path(home).expanduser() / "sessions")
        base = env.get("HOME")
        root = Path(base) if base else Path.home()
        return str(root / ".codex" / "sessions")

    def record_read(self) -> tuple:
        """The base class's record read. No launch calls it: a launch starts its own
        thread and reads no id from the record."""
        return codex_threads.read_outcome(self.agent.qualified)

    def session_report(self, *, register: bool) -> dict:
        """The thread id, its sessions root and `turned`; on `register`, `new_session`.

        Every launch starts its own thread, so a `register` before the launch reports
        `new_session: true`: the thread that follows is this launch's own creation. A
        `register` that holds the launch's thread (a daemon reconnect inside the launch)
        reports it, `new_session: false`. `turned` is false until a turn of it ends."""
        with self._lock:
            thread_id = self.thread_id
        if thread_id:
            self.claims.note_session(thread_id)
        report: dict = {}
        if thread_id:
            report.update(session_id=thread_id, turned=self._own_turned)
            if register:
                report["new_session"] = False
        elif register:
            report["new_session"] = True
        source = self.cache_source()
        if source:
            report["cache_source"] = source
        return report

    # ------------------------------------------------------------------ the launch
    @property
    def write_root(self):
        if self._write_root is ...:
            self._write_root, self.write_root_reason = codex_roster.record_write_root(
                self.agent.env(), self.agent.cwd)
        return self._write_root

    @write_root.setter
    def write_root(self, value) -> None:
        self._write_root = value

    def launch_options(self) -> dict:
        """The roster-owned settings turns lays on every thread load."""
        return self.agent.launch_options(self.write_root)

    def _directors(self) -> tuple:
        return tuple(getattr(self.agent, "directors", None) or ())

    def developer_instructions(self) -> str | None:
        """Layer one, the thread's developer instructions from its start."""
        if not self.layer_one:
            return None
        return layer_one_text(fetch_roster(self.socket_path), self.comms_path,
                              directors=self._directors())

    def launch_env(self) -> dict:
        """The session's environment: the identity stamp and the claim socket."""
        return {**self.agent.env(), **self.claim_env()}

    def start_port(self) -> None:
        """Launch the thread through turns, become its holder and read its events. A
        failure here ends the launch: there is no session to deliver into."""
        instructions = self.developer_instructions()
        options = self.launch_options()
        session = self._launch(str(self.agent.cwd), instructions=instructions,
                               env=self.launch_env(), window=self.window, options=options,
                               verify=lambda answer: self.agent.verify_load(options, answer))
        thread_id = session.session_id
        born_with = {}
        if instructions:
            born_with["layer_one"] = layer_one_hash(self.comms_path,
                                                    directors=self._directors())
        self._born_with = born_with
        if thread_id:
            codex_threads.write(self.agent.qualified, thread_id,
                                identity=self.agent.qualified, cwd=str(self.agent.cwd),
                                launch=self.launch, **born_with)
        try:
            facts = session.facts()
            turn_open = bool(facts.turn_open)
        except Exception as e:                              # noqa: BLE001
            self.log.info("the first facts of %s could not be read (%s); counted idle",
                          self.agent.qualified, e)
            turn_open = False
        with self._lock:
            self.session = session
            self.thread_id = thread_id
            self.turn_open = turn_open
        self._hold = session.hold(self.on_stop)
        self._watcher = threading.Thread(target=self._watch, args=(session,),
                                         daemon=True, name="comms-codex-watch")
        self._watcher.start()
        self.log.info("launched codex thread %s for %s through turns", thread_id,
                      self.agent.qualified)
        # The launch is the session start; a fresh session's mail goes as it comes up.
        self.report_ready(SOURCE_STARTUP)
        self._wake.set()

    def stop_before_unbind(self) -> None:
        hold, self._hold = self._hold, None
        if hold is not None:
            hold.close()

    # ------------------------------------------------------------------ the session's events
    def _watch(self, session) -> None:
        try:
            for event in session.watch():
                if self._stop.is_set():
                    return
                try:
                    if self._on_event(event):
                        return
                except Exception:                           # noqa: BLE001
                    self.log.exception("a session event of %s failed", self.agent.qualified)
        finally:
            if not self._stop.is_set() and not self.gone.is_set():
                self._on_gone("the session's event stream ended")

    def _on_event(self, event) -> bool:
        """One event of `watch()`. True once the session is gone."""
        self._adopt_thread(getattr(event, "session_id", None))
        kind = type(event).__name__
        if kind == "TurnStarted":
            self._turn_started(event.turn_id)
        elif kind == "TurnEnded":
            self._turn_ended(event.turn_id, event.outcome,
                             [event.delivery_id, *(event.steered or ())])
        elif kind == "Ready":
            self._ready(event.source, event.session_id)
        elif kind == "Gone":
            self._on_gone(event.reason)
            return True
        return False

    def _adopt_thread(self, session_id) -> None:
        """A launch with no window returns once its first load was tried, and a failed
        one leaves turns with no thread yet. The first event that names the thread
        gives it to comms: the thread record, and the daemon's evidence."""
        if not session_id:
            return
        with self._lock:
            if self.thread_id is not None:
                return
            self.thread_id = session_id
        codex_threads.write(self.agent.qualified, session_id,
                            identity=self.agent.qualified, cwd=str(self.agent.cwd),
                            launch=self.launch, **self._born_with)
        self.log.info("codex thread %s of %s named by the session", session_id,
                      self.agent.qualified)
        self.report_refresh()

    def _turn_started(self, turn_id) -> None:
        """A turn started, whoever started it. While the reply reminder holds a turn
        open, a new turn resolves it, as a prompt does on the shim."""
        with self._lock:
            self.turn_open = True
            self.active_turn_id = turn_id
            resolves = self.blocked and turn_id != self._blocked_turn
        if resolves:
            self.clear_block()
        self.report_turn_start()

    def _turn_ended(self, turn_id, outcome: str, delivery_ids) -> None:
        """A turn ended: the digests delivered into it settle, `handled` on `answer`
        and `unhandled` otherwise, in one `turn` frame."""
        ids: list = []
        with self._lock:
            self.turn_open = False
            if self.active_turn_id == turn_id:
                self.active_turn_id = None
            for did in delivery_ids:
                if not did:
                    continue
                kept = self._deliveries.pop(did, None)
                if kept is None:
                    self._ended_early[did] = outcome
                    while len(self._ended_early) > EARLY_KEPT:
                        self._ended_early.popitem(last=False)
                else:
                    ids.extend(kept)
            first = not self._own_turned
            self._own_turned = True
        completed = outcome == ANSWER
        self.report_turn_end(handled=ids if completed else (),
                             unhandled=() if completed else ids)
        self.clear_block()
        self.log.info("turn %s of %s ended %s: %d message(s) %s", turn_id,
                      self.agent.qualified, outcome, len(ids),
                      "handled" if completed else "unhandled")
        if first:
            self.report_refresh()          # `turned` changed: a report refresh

    def _ready(self, source, session_id) -> None:
        """A resume or a compaction of the thread. The startup was reported at launch."""
        if source == SOURCE_RESUME:
            self.owed_session_changed(SOURCE_RESUME, session_id)
            self.report_ready(SOURCE_RESUME)
        elif source == SOURCE_COMPACT:
            self.report_ready(SOURCE_COMPACT)

    def _on_gone(self, reason) -> None:
        """The session ended (the window closed, or the host failed): every digest still
        open is `unhandled`, and the launch ends."""
        with self._lock:
            if self.gone.is_set():
                return
            self.gone_reason = str(reason)
            self.gone.set()
            ids = [mid for kept in self._deliveries.values() for mid in kept]
            self._deliveries.clear()
            self.turn_open = False
        if ids:
            self.report_turn_end(unhandled=ids)
        self.log.info("the codex session of %s ended: %s", self.agent.qualified, reason)
        if not self._stop.is_set():
            self.exit_hook()

    # ------------------------------------------------------------------ the holder
    def on_stop(self, facts) -> str | None:
        """turns' Stop ask: the reply check. The reminder text holds the turn open."""
        tasks = getattr(facts, "task_types", None)
        fields = {"stop_hook_active": getattr(facts, "stop_hook_active", None),
                  "task_types": list(tasks) if tasks is not None else None,
                  "crons": getattr(facts, "crons", None)}
        decision = self.reply_check(fields)
        if not decision.block:
            return None
        self.mark_blocked(decision.names)
        with self._lock:
            self._blocked_turn = getattr(facts, "turn_id", None)
        return decision.block

    def clear_block(self) -> None:
        super().clear_block()
        with self._lock:
            self._blocked_turn = None

    # ------------------------------------------------------------------ delivery
    def can_inject(self) -> bool:
        return self.session is not None and not self.gone.is_set()

    def may_take_held(self) -> bool:
        """A payload released from the queue goes only while no turn runs; at the
        running turn's end the flush starts a new turn with it."""
        return not self.turn_open

    def mirror_session(self) -> str | None:
        with self._lock:
            return self.thread_id

    def deliver(self, text: str, high: int, identity, count: int):
        """Hand the digest to turns, steer-first. A refusal puts the batch back: nothing
        went in. A turn id is the receipt. `Lost`, or no answer within `started_wait`,
        is a receipt of None: the ids are `unhandled` on the next `turn` frame, so the
        digest is handed again, a duplicate and never a loss."""
        ids = self.digest_ids(high)
        delivery = self.session.deliver(text, steer=True)
        did = delivery.id
        if did is not None:
            with self._lock:
                self._deliveries[did] = ids
        try:
            result = delivery.wait_started(self.started_wait)
        except TimeoutError:
            result = None
        kind = type(result).__name__
        if kind == "Refused":
            with self._lock:
                self._deliveries.pop(did, None)
            log.warning("turns refused the digest for %s (%s); %d message(s) requeued",
                        identity, result.reason, count)
            return REQUEUE
        if kind != "Started":
            with self._lock:
                self._deliveries.pop(did, None)
            self.owe_unhandled(ids)
            log.warning("turns cannot say which turn took the digest for %s (%s); %d "
                        "message(s) unhandled on the next turn frame", identity,
                        getattr(result, "reason", "no answer in time"), count)
            return None
        with self._lock:
            early = self._ended_early.pop(did, None) if did is not None else None
            if early is not None:
                self._deliveries.pop(did, None)
        if early is not None:
            done = early == ANSWER
            self.report_turn_end(handled=ids if done else (), unhandled=() if done else ids)
        return result.turn_id

    def delivered(self, text: str, high: int, identity, count: int, receipt) -> None:
        if self._daemon is not None:
            self.notify_injected(identity, high, text)
        log.info("injected %d message(s) for %s through %s into turn %s",
                 count, identity, high, receipt)

    def retry_delay(self) -> float:
        return FLUSH_RETRY


# --------------------------------------------------------------------------- main

def build_parser():
    import argparse

    ap = argparse.ArgumentParser(
        prog="moxi-comms-codex",
        description="comms' Codex adapter: one launcher per identity, over turns' Codex "
                    "host")
    ap.add_argument("identity", help="qualified roster identity, e.g. planner@proj-a")
    ap.add_argument("--window", action="store_true",
                    help="also open the Codex window on this launch's thread, in this "
                         "terminal; closing the window stops the launcher and takes the "
                         "identity offline")
    ap.add_argument("--config", metavar="PATH",
                    help="a comms config file, in config.toml's format, whose [codex] "
                         "table is this launch's roster in place of the box's config "
                         "file's; the file is checked whole, and every other setting "
                         "still comes from the box's config file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan for this identity and exit; launch nothing")
    ap.add_argument("--log-level", default="INFO")
    return ap


def window_log_path(qualified: str) -> Path:
    """Where a `--window` run's launcher log goes."""
    from ..paths import codex_threads_dir

    return codex_threads_dir() / f"{qualified}.log"


def print_plan(agent, roster, args, *, version, comms_path, write_root,
               write_root_reason, out=None) -> None:
    out = out or sys.stdout

    def say(text):
        print(text, file=out)

    say(f"identity   {agent.qualified} (slot {agent.slot}, pinned by the roster)")
    say(f"  roster     {roster.source}")
    say(f"  cwd        {agent.cwd}")
    say(f"  thread     {codex_threads.path(agent.qualified)} "
        "(a new thread at this launch)")
    if agent.directors:
        say(f"  directors  {', '.join(agent.directors)} "
            "(verified messages from these render as directives)")
    say(f"  sandbox    {agent.permissions or agent.sandbox} "
        f"(network {'on' if agent.network_access else 'OFF'}, approvals never by turns)")
    if not agent.permissions:
        for root in agent.effective_writable_roots(write_root):
            say(f"  writable   {root}")
        if write_root is None:
            say(f"  writable   (nothing added: {write_root_reason})")
    say(f"  codex      {BINARY} on the session's PATH ({version})")
    say(f"  comms      {comms_path or BARE_COMMS + ' (this environment has no moxi script)'}")
    say("  host       turns' Codex host (app-server, hooks, steer-first delivery, window)")
    if args.window:
        say(f"  log        {window_log_path(agent.qualified)}")


def main(argv=None) -> int:  # pragma: no cover - exercised end to end by the suite
    import shutil
    import signal

    args = build_parser().parse_args(argv)
    try:
        roster = codex_roster.load(path=args.config)
        agent = roster.require(args.identity)
    except ConfigError as e:
        print(f"moxi-comms-codex: {e}", file=sys.stderr)
        return 2

    comms_path = own_comms()
    write_root, write_root_reason = codex_roster.record_write_root(agent.env(), agent.cwd)
    env = agent.env()
    binary = shutil.which(BINARY, path=env.get("PATH"))
    version = binary_version(binary, env=env) if binary else "not found"
    print_plan(agent, roster, args, version=version, comms_path=comms_path,
               write_root=write_root, write_root_reason=write_root_reason)
    sys.stdout.flush()             # a log reader sees the plan before anything else
    if args.window and comms_path is None:
        print("moxi-comms-codex: this environment has no `moxi` script, so the agent "
              "could not be told which comms to run.", file=sys.stderr)
        return 2
    if args.dry_run:
        return 0
    if args.window:
        from moxi_comms.adapters.terminal import in_terminal_foreground, terminal_fd

        if not in_terminal_foreground(terminal_fd()):
            print("moxi-comms-codex: --window needs to run in the foreground of a terminal, "
                  "because the window takes that terminal.", file=sys.stderr)
            return 2
    log_path = window_log_path(agent.qualified) if args.window else None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=args.log_level, filename=str(log_path),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        print(f"moxi-comms-codex: logging to {log_path}", file=sys.stderr, flush=True)
    else:
        logging.basicConfig(level=args.log_level, stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if comms_path is None:
        log.warning("this environment has no moxi script; layer one names a bare "
                    "`moxi comms`, which the agent's PATH decides")

    stopping = threading.Event()
    adapter = CodexAdapter(agent, window=args.window, write_root=write_root,
                           comms_path=comms_path, exit_hook=stopping.set)
    adapter.write_root_reason = write_root_reason
    scrub_own_environ(adapter.launch_env())

    def _bye(signum, _frame):
        log.info("signal %d: stopping", signum)
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT, signal.SIGHUP):
        signal.signal(sig, _bye)
    if args.window:
        from moxi_comms.adapters.terminal import JOB_CONTROL_SIGNALS

        for sig in JOB_CONTROL_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
    # ONE finally for every road out; turns reaps the host when this process ends.
    try:
        try:
            adapter.start()
        except (DaemonOffline, DaemonError, ConfigError) as e:
            print(f"moxi-comms-codex: {e}", file=sys.stderr)
            return 4 if isinstance(e, DaemonOffline) else 2
        except Exception as e:                              # noqa: BLE001
            if args.window and type(e).__name__ != "HooksUntrusted":
                # turns' window wait ran out, or the window failed before a load was
                # accepted: the launcher gives up, exit 3, with the log named.
                print(f"moxi-comms-codex: gave up on {agent.qualified}: "
                      f"{str(e).rstrip('. ')}. The log is {log_path}.",
                      file=sys.stderr, flush=True)
                return EXIT_GAVE_UP
            # turns could not launch the session (no codex, untrusted hooks, a dead
            # app-server): one line, never a traceback.
            print(f"moxi-comms-codex: {agent.qualified} was not launched: {e}",
                  file=sys.stderr)
            return 2
        log.info("moxi-comms-codex running for %s", agent.qualified)
        while not stopping.wait(0.5):
            pass
        reason = adapter.gone_reason
        if reason is None or reason.startswith(WINDOW_CLOSED):
            return 0
        print(f"moxi-comms-codex: {agent.qualified}'s session ended: {reason}"
              + (f". The log is {log_path}." if log_path else ""), file=sys.stderr,
              flush=True)
        return EXIT_GAVE_UP
    finally:
        adapter.stop()


__all__ = ["CodexAdapter", "ADAPTER_KIND", "build_parser", "main", "scrub_own_environ"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
