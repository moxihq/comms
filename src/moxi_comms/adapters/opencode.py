"""`moxi-comms-opencode <identity>` -- an OpenCode session the person sits in, with comms mail.

One launcher process per identity, an ordinary daemon-socket client whose connection is
its liveness, like `moxi-comms-codex`. What it owns:

  * one registration on the daemon socket, with the slot the roster PINS;
  * one `opencode serve` (`opencode_server`), spawned with the identity stamped into its
    environment, on loopback, with a per-launch password;
  * a new OpenCode session at every launch, whose id it records in STATE; it never
    loads one an earlier launch recorded, as a Claude launch starts a new session;
  * the event stream of that server, read for the launch's session;
  * the window: `opencode attach <url> -s <session>` in the launcher's own terminal,
    where the person sits. Closing it stops the launcher and takes the identity offline.

**Delivery is a channel of its own.** A digest is posted to the server with
`POST /session/:id/prompt_async`, under a message id comms chooses. The window is a
client of the same server and shows the posted turn live; nothing is ever typed into
the window, so what the person has half-typed in its input box is never touched.

**Only into an idle session.** The session has one loop: a prompt that arrives while a
turn runs is taken at that turn's next step, and the running turn's own reply is never
written. So the launcher posts only when the session is idle, read from
`GET /session/status` immediately before each post (a 200 whose JSON has no entry for
the session, or an entry that says idle; anything else is "not idle"), and holds the
next post from its own post until that turn ends. A prompt the person submits between
that read and the server admitting the post can still meet it; that window is a few
milliseconds on loopback, on top of the few milliseconds from their Enter to the
server's busy status.

**The cursor moves on the first idle after the model saw the digest, when the turn that
ran ended with `finish=stop`.** The model saw it once an assistant message whose parent
is comms' message id appears. If the person submits while the digest's turn runs, their
prompt takes that turn over: the digest's own reply is never written, the stop is on
the reply to them, and the digest completes on that idle. A turn that ends by an abort
or an error moves no cursor, and its ids are `unhandled`, so the daemon hands them again.

**Every turn is reported**, whoever prompted it: busy is an
`activity` with no kind, and the idle after it one `turn` frame with the settled posts'
ids `handled` or `unhandled`, sent after the clamped cursor write. An idle after an abort
sends none: nothing reports the abort itself, as no hook reports Claude Code's Esc. Its
ids go `unhandled` on the next frame, as the Claude shim names an interrupted digest
`unhandled` once it reads the Esc in the session's transcript. `session.compacted` is `ready source=compact`. A window
/new or /clear is followed: a top-level `session.created` this launcher did not make
rebinds the launch to that session, and the old session's pending posts go into it.

**The reply reminder is a line of the adapter's own**, as Claude Code's is a held-open
Stop. At each `turn` the adapter runs the shim's reply check
(`HarnessAdapter.reply_check`), with no other hook to block (`stop_hook_active` false)
and no background work (an idle is the end of the run loop). A turn that ends owing a
reply posts the reminder into the idle session as an untracked turn; the `turn` that
ends that turn counts the debts it named, and any other turn end first voids them.

**The model is a launch default.** The launch config sets OpenCode's `model` and
`small_model` to a free model the roster names (`opencode_roster.DEFAULT_MODELS` for a
row that names none), or to another free model when none of those is offered. The
choice is made on a model list fetched for this launch: before it starts the server, the
launcher runs `opencode models --refresh`, within `MODELS_REFRESH_TIMEOUT`, so a model
retired since OpenCode last fetched its list is not offered. A refresh that fails or
overruns leaves the list as it stands, and the launch says so with the list's age. A
server handed in to the launcher keeps the list it read at its own start. Comms never
changes the model after the launch, and its posts name none. A request
that runs on any other model is written to the log and the identity's status line, and
nothing more is done about it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import secrets
import string
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..client import DaemonError, DaemonClient, DaemonOffline, backoff_delays
from ..config import ConfigError
from ..context import BARE_COMMS, MOXI_COMMS_PATH_LINE, own_comms
from . import opencode_roster
from .harness import (REQUEUE, SOURCE_CLEAR, SOURCE_COMPACT, SOURCE_STARTUP, TURN_COMPLETED,
                      HarnessAdapter, TurnLedger, launch_stamp)
from .opencode_server import (EventStream, Http, NoAnswer, Server, Unreachable,
                              parse_stream_line)

log = logging.getLogger("comms.opencode")

ADAPTER_KIND = "opencode"
#: Seconds to wait after a delivery that put its batch back.
FLUSH_RETRY = 1.0
#: Consecutive failed posts of the kind a retry can fix (no connection, a 5xx, a 429)
#: after which the status line says how many and which, once per run of failures.
FAILURE_STREAK = 5
#: How long the launcher keeps a launch notice on the screen before the window opens.
NOTICE_HOLD = 5.0
#: How long a launch waits for its session to be ready before giving up.
READY_WAIT = 120.0
#: Status lines the session file keeps, newest last.
STATUS_KEEP = 20
#: The `error.name` of an assistant message whose turn was aborted (the window's Esc).
ABORTED = "MessageAbortedError"

#: The launch notices: the zero-retention default isn't offered.
NO_FREE_MODEL = ("no free model offered: OpenCode will use its own default, which may be "
                 "a paid model if your own credentials allow one")
FALLBACK_MODEL = ("{wanted} is not offered: this session's default is {model}, another "
                  "free model, which is not the zero-retention one and may keep or train "
                  "on what the session sends it")
#: The launch notice when a later entry of the shipped default list is chosen.
SHIPPED_DEFAULT = ("{wanted} is not offered: this session's default is {model}, one of "
                   "comms' shipped zero-retention defaults")
#: The launch notice when OpenCode's model list could not be refreshed. `age` is
#: "last written <age> ago", or "of unknown age".
MODELS_STALE = ("OpenCode's model list could not be refreshed ({reason}); this launch "
                "chose from the list as it stands, {age}")

#: The refresh a launch runs before it starts its server: OpenCode fetches its model
#: list and writes it at once. Measured on OpenCode 1.18.34: 1.2 to 1.5 s, one download
#: of about 6 MB. It exits 0 and says "Models cache refreshed" when the fetch failed
#: too; only the error line it logs tells (`MODELS_FETCH_FAILED`).
MODELS_REFRESH_ARGS = ("models", "--refresh", "--pure", "--print-logs", "--log-level",
                       "ERROR", "opencode")
MODELS_REFRESH_TIMEOUT = 20.0
MODELS_FETCH_FAILED = "Failed to fetch models.dev"
#: A request on a model other than the launch default.
OTHER_MODEL = ("a request in session {session} ran on {model} (small={small}), not on the "
               "launch default {default}")
NO_DEFAULT_MODEL = ("a request in session {session} ran on {model} (small={small}); no "
                    "launch default is set")
UNSEEN_REQUEST = ("an assistant message in session {session} on {model} came with no "
                  "request line in the server's log before it")
#: A post the server refused in a way a retry can't fix.
POST_REFUSED = (
    "the OpenCode server refused a post for {identity} with {status} ({meaning}): "
    "{detail}. No more mail goes into this session until the launcher is started "
    "again; the undelivered messages replay then.")
POST_REFUSED_CONTENT = (" If the refusal is about the digest itself, the same messages "
                        "will be refused again after a relaunch; the launcher's log has "
                        "the server's answer.")
FAILING = ("{count} posts in a row for {identity} have failed ({last}); still retrying "
           "every {wait:g}s.")
MEANINGS = {400: "a request the server refuses", 401: "the server password did not match",
            403: "forbidden", 404: "this launch's session is gone"}


class OpenCodeOffline(OSError):
    """The server, or the session, could not be reached."""


# --------------------------------------------------------------------------- the record
def read_record(path: Path) -> dict:
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def write_record(path: Path, record: dict) -> None:
    """Whole file, atomic replace, 0600 in a 0700 directory, as the cursors are."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


#: What one read of the session file found: no session, a session id, or a file that
#: exists but could not be read as one. Only `NONE` is evidence that a session the
#: launcher then creates is new.
NONE, RECORDED, UNREADABLE = "none", "recorded", "unreadable"


def read_outcome(path: Path) -> tuple:
    """(NONE | RECORDED | UNREADABLE, the session id or None). `read_record` folds an
    unreadable file into an empty one; this read keeps them apart."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return NONE, None
    except OSError:
        return UNREADABLE, None
    try:
        record = json.loads(text)
    except ValueError:
        return UNREADABLE, None
    if not isinstance(record, dict):
        return UNREADABLE, None
    session_id = record.get("session_id")
    if session_id is None or session_id == "":
        return NONE, None
    if not isinstance(session_id, str):
        return UNREADABLE, None
    return RECORDED, session_id


#: OpenCode's built-in agents: whether each is a primary agent a prompt with no agent
#: can run on (1.18.34, `agent/agent.ts`).
BUILTIN_AGENTS = {"build": "primary", "plan": "primary", "general": "subagent",
                  "explore": "subagent", "compaction": "hidden", "title": "hidden",
                  "summary": "hidden"}


def default_agent_model(config) -> dict | None:
    """`{"agent": name, "model": "provider/model" or None}` for the agent OpenCode runs
    a prompt that names none on, from the merged config `opencode debug config`
    prints, or None when comms cannot name it from that config.

    `default_agent` when the config sets it; else `build`, unless the config disables,
    hides, renames or demotes it. In any other case OpenCode takes the first visible
    primary agent of a list the config does not print, so this answers None."""
    if not isinstance(config, dict):
        return None
    agents = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    modes = config.get("mode") if isinstance(config.get("mode"), dict) else {}

    def entry(name):
        a, m = agents.get(name, {}), modes.get(name)
        if not isinstance(a, dict) or (m is not None and not isinstance(m, dict)):
            return None
        merged = dict(a)
        if m is not None:
            merged.update(m)
            merged["mode"] = "primary"
        return merged

    def usable(name, merged) -> bool:
        if merged is None:
            return False
        if merged.get("disable") is True or merged.get("hidden") is True:
            return False
        if merged.get("name") not in (None, name):
            return False
        mode = merged.get("mode")
        if mode is None:
            if name not in agents and name not in modes:
                return BUILTIN_AGENTS.get(name) == "primary"
            mode = {"primary": "primary"}.get(BUILTIN_AGENTS.get(name), "all")
        return mode in ("primary", "all")

    default = config.get("default_agent")
    name = default if default is not None else "build"
    if not isinstance(name, str) or not name:
        return None
    merged = entry(name)
    if not usable(name, merged):
        return None
    model = merged.get("model")
    if model is not None and (not isinstance(model, str) or "/" not in model):
        return None
    return {"agent": name, "model": model}


def update_record(path: Path, **fields) -> dict:
    record = read_record(path)
    record.update(fields)
    record["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_record(path, record)
    return record


def status_lines(path: Path) -> list:
    """The identity's status lines, oldest first: `[{"at": ..., "text": ...}]`."""
    lines = read_record(path).get("status")
    return lines if isinstance(lines, list) else []


# --------------------------------------------------------------------------- the launch
def mint_message_id(n: int = 1) -> str:
    """A message id in OpenCode's own `msg_` format: 12 hex digits of
    `milliseconds * 4096 + n`, then 14 base62 characters. The server keeps it."""
    ms = int(time.time() * 1000)
    tail = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(14))
    return f"msg_{(ms * 0x1000 + n) & 0xFFFFFFFFFFFF:012x}{tail}"


def free_models(providers: dict | None) -> list:
    """The models a `GET /config/providers` answer offers at no cost, as
    `provider/model`, in the listing's order. A model is free when every price in its
    `cost` is zero."""
    found = []
    for provider in (providers or {}).get("providers") or []:
        for model_id, model in (provider.get("models") or {}).items():
            cost = model.get("cost") if isinstance(model, dict) else None
            if not isinstance(cost, dict):
                continue
            prices = [cost.get("input"), cost.get("output")]
            cache = cost.get("cache") or {}
            prices += [cache.get("read", 0), cache.get("write", 0)]
            if all(isinstance(p, (int, float)) and p == 0 for p in prices):
                found.append(f"{provider.get('id')}/{model_id}")
    return found


#: Where an automatic fallback comes from: OpenCode Zen, whose free models need no
#: account. A model of any other provider is a fallback only when the roster names it.
ZEN = "opencode/"


def choose_default(wanted: list, offered_free: list) -> tuple:
    """(the launch default or None, the notice to show or None).

    The roster's first model when it is offered free; else the roster's next named
    model that is; else the first free Zen model offered; else none at all. Only free
    models are ever chosen. A model other than the first is said: as one of the
    shipped zero-retention defaults when it is one, else as not the zero-retention
    one."""
    if wanted and wanted[0] in offered_free:
        return wanted[0], None
    first = wanted[0] if wanted else opencode_roster.DEFAULT_MODEL
    for model in list(wanted[1:]) + [m for m in offered_free if m.startswith(ZEN)]:
        if model in offered_free:
            said = (SHIPPED_DEFAULT if model in opencode_roster.DEFAULT_MODELS
                    else FALLBACK_MODEL)
            return model, said.format(wanted=first, model=model)
    return None, NO_FREE_MODEL


def models_list_file(env) -> Path | None:
    """The model list OpenCode 1.18.34 reads, `$XDG_CACHE_HOME/opencode/models.json`
    (`~/.cache` by default), or None when `OPENCODE_MODELS_URL` or
    `OPENCODE_MODELS_PATH` points it at another one."""
    if env.get("OPENCODE_MODELS_URL") or env.get("OPENCODE_MODELS_PATH"):
        return None
    cache = env.get("XDG_CACHE_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".cache")
    return Path(cache) / "opencode" / "models.json"


def age_words(seconds: float) -> str:
    """An age in one unit, rounded down: "40 s", "12 min", "5 h", "3 d"."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def list_age(env, now=None) -> str:
    """How old the model list is, for `MODELS_STALE`: "last written <age> ago", or "of
    unknown age" when the file is missing or OpenCode is pointed at another one."""
    path = models_list_file(env)
    try:
        written = path.stat().st_mtime if path is not None else None
    except OSError:
        written = None
    if written is None:
        return "of unknown age"
    return f"last written {age_words((time.time() if now is None else now) - written)} ago"


def launch_config(model: str | None, instructions: str | None) -> dict:
    """What `OPENCODE_CONFIG_CONTENT` carries for comms' server: the launch default on
    both model slots (or none), layer one by absolute path, and answers to the two
    permission asks no one would be there to answer. No credential, key or provider
    entry, ever: the person's own OpenCode config and sign-in are what the server uses
    for the rest."""
    config: dict = {"permission": {"external_directory": "allow", "doom_loop": "deny"}}
    if instructions:
        config["instructions"] = [str(instructions)]
    if model:
        config["model"] = model
        config["small_model"] = model
    return config


def layer_one_text(roster_info, comms: str = BARE_COMMS, *, directors=()) -> str:
    """Layer one for an OpenCode session: the context block, the line naming the
    `comms` to use, then the labeled roster snapshot (or the offline line), the same
    text a Codex thread gets."""
    from ..context import layer_one_block
    from ..roster_view import render_agents
    from .shim_roster import LABEL, OFFLINE, usable

    roster = f"{LABEL}\n{render_agents(roster_info)}" if usable(roster_info) else OFFLINE
    return (f"{layer_one_block(directors)}\n\n{MOXI_COMMS_PATH_LINE.format(comms=comms)}"
            f"\n\n{roster}")


def fetch_roster(socket_path=None, timeout: float = 2.0):
    try:
        client = DaemonClient(socket_path, timeout=timeout).connect()
        try:
            return client.query("agents")
        finally:
            client.close()
    except Exception as e:                                  # noqa: BLE001
        log.warning("roster for layer one unavailable: %s", e)
        return None


def binary_version(binary: str, env=None, timeout: float = 15.0) -> str:
    import subprocess

    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown ({e})"
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0] if text else f"unknown (exit {out.returncode})"


def idle_from_status(status: int, body, session_id: str) -> bool:
    """Whether a `GET /session/status` answer says the session is idle. Only a 200 whose
    body is a JSON object with no entry for the session, or an entry of type idle, does;
    an error, an empty or malformed body, or any other entry is not idle."""
    if status != 200 or not isinstance(body, dict):
        return False
    entry = body.get(session_id)
    if entry is None:
        return True
    return isinstance(entry, dict) and entry.get("type") == "idle"


# --------------------------------------------------------------------------- generations
class Generation:
    """One server process the launcher started, from listening until it is lost.

    A window opens only on a ready generation (the launch's session and its event stream
    reconciled), and is closed when its generation ends."""

    def __init__(self, number: int, url: str):
        self.number = number
        self.url = url
        self.thread_id: str | None = None        # the session id, for the window
        self.ready = threading.Event()
        self.ended = threading.Event()

    def end(self) -> None:
        self.ended.set()

    def __repr__(self) -> str:
        state = "ended" if self.ended.is_set() else (
            "ready" if self.ready.is_set() else "unready")
        return f"<generation {self.number} {state} {self.url} session {self.thread_id}>"


@dataclass
class Pending:
    """A post the server admitted, or may have, and what the stream has shown of it."""

    high: int
    count: int
    admitted: bool = False      # its user message was seen
    seen: bool = False          # an assistant message parented to it was seen
    errored: bool = False       # the session reported an error after it
    uncertain: bool = False     # the post had no answer; the reconcile decides
    #: The digest's payloads: a follow of a window /new puts them back in the queue.
    batch: list = field(default_factory=list)


class OpenCodeAdapter(HarnessAdapter):
    """One identity: daemon binding, OpenCode server, session, cursor."""

    KIND = ADAPTER_KIND
    STATE_NOUN = "session file"
    QUIET_OFFLINE_ON_STOP = True
    HOLDS_UNTIL_TURN_ENDS = True   # a digest's ids are held until its post's turn ends
    log = log

    def __init__(self, agent: opencode_roster.OpenCodeAgent, *, socket_path=None,
                 binary: str | None = None, spawn: bool = True, server=None,
                 password: str | None = None, autoreconnect: bool = True, backoff=None,
                 status_sink=None, clock=time.time, log_path=None,
                 comms_path: str | None = None, layer_one: bool = True,
                 silence: float | None = None, base_env=None):
        super().__init__(agent, socket_path=socket_path, autoreconnect=autoreconnect,
                         backoff=backoff, status_sink=status_sink or self._status,
                         clock=clock)
        self.binary = binary or agent.binary or opencode_roster.BINARY
        self.spawn = spawn
        self.password = password if password is not None else secrets.token_urlsafe(24)
        self.log_path = log_path
        self.comms_path = comms_path or BARE_COMMS
        self.layer_one = layer_one
        self.silence = silence
        self.base_env = base_env
        #: The server: ours to spawn, or one handed in (a test's).
        self.server = server
        self.http: Http | None = None
        self.stream: EventStream | None = None

        self.session_id: str | None = None
        self.default_model: str | None = None
        #: The launch notices, in order: a model list that could not be refreshed, and
        #: a launch default other than the roster's first model.
        self.launch_notices: list = []
        self.generation: Generation | None = None
        self._generations = 0

        self.ready = False              # stream open and reconciled
        self.busy = False               # the session's own status says it is running
        self.turn_open = False          # from comms' post until that turn's idle
        self.fatal: str | None = None   # a refusal a retry can't fix: closed for the launch
        self.pending: dict = {}
        self.ledger = TurnLedger()
        self.last_turn: dict = {}       # the latest assistant message in the session
        self.failures = 0
        self._streak_said = False
        self._reconcile_wanted = False
        self._server_delays = None
        self.trouble: str | None = None
        #: The reply reminder's own post while it holds a turn: its message id, and
        #: whether the server admitted it (its user message was seen).
        self._reminder: str | None = None
        self._reminder_admitted = False

        # the model watch
        self.models_said: set = set()
        self.model_notes: list = []
        self._watch_start_ms = int(time.time() * 1000)
        self._stream_lines: dict = {}
        self._assistants: dict = {}     # session -> {message id: completed?}
        self._completed_new: dict = {}

        # the cache clock's evidence (`session_report`)
        #: Runs `opencode <args>` under the server's environment in the agent's working
        #: directory: (exit code, stdout). A test hands in its own.
        self.run_opencode = self._run_opencode
        self._own_session: str | None = None
        self._own_turned = False
        self._own_busy = False
        self._cache_source: str | None = None
        self._agent_model: dict | None = None
        #: Which launch wrote the session file: `<pid>@<UTC start>`.
        self.launch = launch_stamp()

    @property
    def launch_notice(self) -> str | None:
        """The launch notices as one text, a line each, or None when there are none."""
        return "\n".join(self.launch_notices) or None

    # ------------------------------------------------------------------ status line
    def _status(self, text: str) -> None:
        """The identity's status line: the log, and the session file, where
        `moxi comms explain` reads it while the window covers the terminal."""
        self.log.warning("%s", text)
        try:
            lines = status_lines(self.agent.session_file)
            lines.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "text": text})
            update_record(self.agent.session_file, status=lines[-STATUS_KEEP:])
        except OSError as e:
            self.log.warning("could not write the status line to %s: %s",
                             self.agent.session_file, e)

    # ------------------------------------------------------------------ the clock's evidence
    def _run_opencode(self, args) -> tuple:
        import subprocess

        try:
            out = subprocess.run([self.binary, *args], capture_output=True, text=True,
                                 env=self.server_env(self._probe_model()),
                                 cwd=self.agent.cwd, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("opencode %s failed for %s: %s", " ".join(args),
                        self.agent.qualified, e)
            return -1, ""
        return out.returncode, out.stdout

    def _probe_model(self) -> str | None:
        if self.default_model:
            return self.default_model
        wanted = list(self.agent.models)
        return wanted[0] if wanted else None

    def probe(self) -> None:
        """Ask OpenCode, under the server's environment, where its database is and which
        model the default agent runs on. A command that fails reports nothing, and the
        daemon reads what needed it as `unknown`."""
        code, out = self.run_opencode(["db", "path"])
        path = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
        self._cache_source = path or None
        code, out = self.run_opencode(["debug", "config"])
        named = None
        if code == 0:
            try:
                named = default_agent_model(json.loads(out))
            except ValueError:
                named = None
        self._agent_model = named

    def record_read(self) -> tuple:
        """The base class's record read. No launch calls it: a launch creates its own
        session and reads no id from the session file."""
        return read_outcome(self.agent.session_file)

    def _turned(self, session_id: str) -> bool:
        return not (session_id == self._own_session and not self._own_turned)

    def session_report(self, *, register: bool) -> dict:
        """The session id, the database, the default agent's model and `turned`; on
        `register`, `new_session`. `register` asks OpenCode again each time, so a
        reconnect never carries an agent model read for an older server.

        Every launch creates its own session (`load_session`), so a `register` with no
        session held reports `new_session: true` and reads no session file. One that
        holds the launch's session reports it, `new_session: false`."""
        if register:
            self.probe()
        with self._lock:
            session_id = self.session_id
        if session_id:
            self.claims.note_session(session_id)
        report: dict = {"agent_model": self._agent_model}
        if session_id:
            report.update(session_id=session_id, turned=self._turned(session_id))
            if register:
                report["new_session"] = False
        elif register:
            report["new_session"] = True
        if self._cache_source:
            report["cache_source"] = self._cache_source
        return report

    # ------------------------------------------------------------------ the server
    def server_env(self, model: str | None) -> dict:
        """The server's environment: the launcher's own, scrubbed of comms' variables
        and stamped with this identity, the per-launch password, and the launch config.
        Nothing else of OpenCode's is set: the person's config, sign-in and switches
        come from the environment the launcher was started in."""
        env = self.agent.env(self.base_env)
        env.update(self.claim_env())
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            launch_config(model, self.layer_one_path() if self.layer_one else None))
        return env

    def window_env(self) -> dict:
        """The window's environment: the server's, so `attach` holds the password."""
        return self.server_env(self.default_model)

    def layer_one_path(self) -> Path:
        from ..paths import opencode_runtime_dir

        return opencode_runtime_dir() / f"{self.agent.qualified}.md"

    def write_layer_one(self) -> None:
        path = self.layer_one_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_text(layer_one_text(fetch_roster(self.socket_path), self.comms_path,
                                       directors=getattr(self.agent, "directors", ())))

    def _new_server(self, model: str | None) -> Server:
        return Server(self.binary, env=self.server_env(model), cwd=self.agent.cwd,
                      log_path=self.log_path, on_line=self.on_log_line)

    def _bring_up(self, model: str | None) -> str:
        """Start a server on `model` (or reuse a handed-in one) and return its URL."""
        if self.spawn:
            if self.server is not None:
                self.server.stop()
            self.server = self._new_server(model)
            self.server.start()
        elif self.server is None:
            raise OpenCodeOffline("no server to connect to")
        url = self.server.wait_listening()
        if not url:
            self.trouble = (f"the opencode server never said where it listens (exit "
                            f"{self.server.returncode})")
            raise OpenCodeOffline(self.trouble)
        self.http = Http(url, self.password)
        return url

    def refresh_models(self) -> str | None:
        """Have OpenCode refresh its model list, under the server's environment and
        within `MODELS_REFRESH_TIMEOUT`. None when it did; else the `MODELS_STALE`
        notice, with why and the age of the list the launch then chooses from.

        It refreshed when the command exits 0 in time with no `MODELS_FETCH_FAILED`
        line on stderr. It runs in a process group of its own, so an overrun is killed
        whole. The list is the person's own model cache, which OpenCode rewrites itself
        hourly; a server started within 5 minutes of the write skips its own refresh."""
        import signal
        import subprocess

        env = self.server_env(self._probe_model())
        argv = [self.binary, *MODELS_REFRESH_ARGS]
        reason = None
        try:
            proc = subprocess.Popen(argv, cwd=self.agent.cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        except OSError as e:
            reason = f"opencode did not run: {e}"
        else:
            try:
                _out, err = proc.communicate(timeout=MODELS_REFRESH_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.communicate()
                reason = f"no answer within {MODELS_REFRESH_TIMEOUT:g}s"
            else:
                if proc.returncode != 0:
                    reason = f"exit {proc.returncode}"
                elif MODELS_FETCH_FAILED in (err or ""):
                    reason = MODELS_FETCH_FAILED
        if reason is None:
            log.info("OpenCode's model list refreshed for %s", self.agent.qualified)
            return None
        return MODELS_STALE.format(reason=reason, age=list_age(env))

    def offered_free(self) -> list:
        try:
            status, body = self.http.json("GET", "/config/providers")
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"the provider listing did not answer: {e}") from e
        if status != 200:
            raise OpenCodeOffline(f"the provider listing answered {status}")
        return free_models(body)

    def start_port(self) -> None:
        """Server up, the launch default chosen, the launch's session created, the
        stream open.

        A launch that starts its own server first has OpenCode refresh its model list
        (`refresh_models`), so the server reads a list fetched for this launch. The
        server is started on the roster's model, the offered models are read, and,
        when that model isn't offered free, the server is started again on the default
        chosen from what is. Any failure here ends the launch with its reason; a
        refresh that fails does not, and is said."""
        if self.layer_one:
            self.write_layer_one()
        notices = []
        if self.spawn:
            stale = self.refresh_models()
            if stale:
                notices.append(stale)
        wanted = list(self.agent.models)
        self._bring_up(wanted[0] if wanted else None)
        model, notice = choose_default(wanted, self.offered_free())
        if self.spawn and model != (wanted[0] if wanted else None):
            self._bring_up(model)
        self.default_model = model
        if notice:
            notices.append(notice)
        self.launch_notices = notices
        for text in notices:
            self.status_sink(f"{self.agent.qualified}: {text}")
        # From here until this launch's own `POST /session` the file names no session,
        # so it never pairs this launch's default model with an earlier launch's session.
        update_record(self.agent.session_file, default_model=model, session_id=None,
                      launch=None)
        self.load_session()
        self.open_generation()
        self.report_refresh()

    def load_session(self) -> str:
        """The launch's session: created by this launch's start, kept for the rest of
        the launch.

        With a session held (this launch's own) it is returned unchanged. Otherwise
        `POST /session`, always: no session id is ever read from the session file, so
        a session an earlier launch recorded is never loaded."""
        with self._lock:
            held = self.session_id
        if held:
            return held
        try:
            status, body = self.http.json("POST", "/session",
                                          {"title": self.agent.qualified})
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"creating the session: {e}") from e
        if status != 200 or not isinstance(body, dict) or not body.get("id"):
            raise OpenCodeOffline(f"creating the session answered {status}")
        with self._lock:
            self.session_id = body["id"]
            self._own_session, self._own_turned = self.session_id, False
        update_record(self.agent.session_file, session_id=self.session_id,
                      identity=self.agent.qualified, cwd=self.agent.cwd,
                      launch=self.launch)
        log.info("started OpenCode session %s for %s", self.session_id,
                 self.agent.qualified)
        self.report_ready(SOURCE_STARTUP)
        return self.session_id

    def open_generation(self) -> None:
        """Open the event stream, reconcile, and mark a new generation ready."""
        self._generations += 1
        generation = Generation(self._generations, self.http.url)
        generation.thread_id = self.session_id
        with self._lock:
            stale, self.generation = self.generation, generation
        if stale is not None and stale is not generation:
            stale.end()
        self.connect_stream()
        generation.ready.set()
        log.info("generation %d is ready for a window: %s", generation.number,
                 generation.url)

    def connect_stream(self) -> None:
        with self._lock:
            self.ready = False
        old, self.stream = self.stream, None
        if old is not None:
            old.close("replaced")
        kw = {} if self.silence is None else {"silence": self.silence}
        stream = EventStream(self.http, on_event=self.on_event,
                             on_close=lambda reason, s=None: self._on_stream_close(reason),
                             **kw)
        try:
            stream.start()
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"the event stream did not open: {e}") from e
        self.stream = stream
        self.reconcile()

    def _on_stream_close(self, reason: str) -> None:
        with self._lock:
            self.ready = False
        if not self._stop.is_set():
            log.warning("event stream for %s closed: %s", self.agent.qualified, reason)

    # ------------------------------------------------------------------ reconcile
    def reconcile(self) -> None:
        """After the stream (re)opens, or a post had no answer: read the session's
        status and its messages, settle every pending post from them, then release."""
        sid = self.session_id
        try:
            status, body = self.http.json("GET", "/session/status")
            mstatus, messages = self.http.json("GET", f"/session/{sid}/message")
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"reconciling: {e}") from e
        if status != 200 or not isinstance(body, dict):
            raise OpenCodeOffline(f"the session status read answered {status}")
        idle = idle_from_status(status, body, sid)
        if mstatus == 404:
            self._close_for_launch(404, "this launch's session is no longer on the server")
            return
        if mstatus != 200 or not isinstance(messages, list):
            raise OpenCodeOffline(f"reading the session's messages answered {mstatus}")
        by_id = {}
        last = {}
        for m in messages:
            info = (m or {}).get("info") or {}
            by_id[info.get("id")] = info
            if info.get("role") == "assistant":
                last = info
        settled = []
        lost = []
        with self._lock:
            self._reconcile_wanted = False
            was_busy, self.busy = self.busy, not idle
            if last:
                self.last_turn = {"id": last.get("id"), "finish": last.get("finish"),
                                  "error": last.get("error")}
            if self._reminder is not None and self._reminder in by_id:
                self._reminder_admitted = True
            for mid, p in list(self.pending.items()):
                if mid not in by_id:
                    # Never admitted: a receipt that moved no cursor. Its ids are
                    # `unhandled` on the next `turn` frame, and held until it is sent.
                    self.pending.pop(mid)
                    _, ids = self.ledger.end({"id": mid, "status": "not admitted"})
                    self.marks.pop(str(p.high), None)
                    lost.extend(ids)
                    log.warning("post %s for %s was never admitted; its %d message(s) "
                                "are unhandled on the next turn frame", mid, self.identity,
                                p.count)
                    continue
                p.admitted, p.uncertain = True, False
                p.seen = p.seen or any(i.get("parentID") == mid for i in by_id.values())
            if idle:
                settled = self._settle_locked()
            self.turn_open = bool(self.pending)
            self.ready = True
        self.owe_unhandled(lost)
        if idle and (was_busy or settled):
            # A turn ended while the stream was cut: it is reported as at an idle.
            self._turn_ended(settled)
        elif not idle and not was_busy:
            self.report_turn_start()
        self._wake.set()

    # ------------------------------------------------------------------ events
    def on_event(self, event: dict) -> None:
        kind = event.get("type")
        props = event.get("properties") or {}
        if kind == "message.updated":
            info = props.get("info") or {}
            self._watch_message(info)
            if info.get("sessionID") == self.session_id:
                self._on_message(info)
        elif kind == "session.status":
            if props.get("sessionID") == self.session_id:
                state = (props.get("status") or {}).get("type")
                if state == "idle":
                    self._on_idle()
                else:
                    with self._lock:
                        started, self.busy = not self.busy, True
                        if self.session_id == self._own_session:
                            self._own_busy = True
                    if started:
                        # A turn started, whoever prompted it: the UserPromptSubmit
                        # analogue, `activity` with no kind.
                        self.report_turn_start()
        elif kind == "session.idle":
            if props.get("sessionID") == self.session_id:
                self._on_idle()
        elif kind == "session.compacted":
            if props.get("sessionID") == self.session_id:
                # Manual or automatic: `ready source=compact`. An automatic compaction
                # runs inside the busy loop, so `busy` and `turn_open` stay as they are.
                self.report_ready(SOURCE_COMPACT)
        elif kind == "session.created":
            self._on_session_created(props.get("info") or {})
        elif kind == "session.error":
            if props.get("sessionID") == self.session_id:
                with self._lock:
                    for p in self.pending.values():
                        if p.admitted:
                            p.errored = True

    def _on_message(self, info: dict) -> None:
        mid = info.get("id")
        with self._lock:
            if info.get("role") == "user":
                if mid in self.pending:
                    self.pending[mid].admitted = True
                if mid is not None and mid == self._reminder:
                    self._reminder_admitted = True
                return
            if info.get("role") != "assistant":
                return
            parent = info.get("parentID")
            if parent in self.pending:
                self.pending[parent].seen = True
            if not self.last_turn or str(mid) >= str(self.last_turn.get("id") or ""):
                self.last_turn = {"id": mid, "finish": info.get("finish"),
                                  "error": info.get("error")}

    def _on_idle(self) -> None:
        """The session's run loop ended. An idle that follows a busy (or settles a post)
        is a turn end of the agent, whoever prompted it, and sends one `turn` frame.
        A second idle for the same end (the server publishes both
        `session.status` idle and `session.idle`) finds nothing and sends nothing."""
        with self._lock:
            was_busy, self.busy = self.busy, False
            settled = self._settle_locked()
            self.turn_open = bool(self.pending)
            first_turn_ended = (self.session_id == self._own_session and self._own_busy
                                and not self._own_turned)
            if first_turn_ended:
                self._own_turned = True
        if was_busy or settled:
            self._turn_ended(settled)
        if first_turn_ended:
            self.report_refresh()
        self._wake.set()

    def _settle_locked(self) -> list:
        """At an idle: every admitted pending post is settled. Called under the lock.
        Returns (message id, every id of its digest, completed, the post) for each. A
        settled post that completed is `handled`; any other is `unhandled`, and its ids
        stay held until the frame naming them is sent."""
        settled = []
        turn = self.last_turn or {}
        ended_well = turn.get("finish") == "stop" and not turn.get("error")
        for mid, p in list(self.pending.items()):
            if not p.admitted:
                continue          # an idle from before this post was admitted
            completed = p.seen and not p.errored and ended_well
            _, ids = self.ledger.end(
                {"id": mid, "status": TURN_COMPLETED if completed else "ended"})
            self.pending.pop(mid)
            settled.append((mid, ids, completed, p))
        return settled

    def _aborted(self) -> bool:
        """The session's last assistant message ended in an abort (the window's Esc)."""
        with self._lock:
            error = (self.last_turn or {}).get("error")
        return isinstance(error, dict) and error.get("name") == ABORTED

    def _turn_ended(self, settled: list) -> None:
        """One turn end: the settled posts' ids go `handled` or `unhandled` on one `turn`
        frame, after the cursor write (the base's `report_turn_end`, with the clamp).

        An idle after an abort sends no `turn`: no frame reports the abort itself, as
        none reports Claude Code's Esc, whose Stop hook does not run on a user interrupt
        (code.claude.com/docs/en/hooks, "Stop": "Does not run if the stoppage occurred
        due to a user interrupt"). The aborted turn's ids are `unhandled` on the next
        `turn` frame and held until it is sent, as the Claude shim's are on the frame it
        sends once it reads the Esc in the session's transcript."""
        handled, unhandled = [], []
        for mid, ids, completed, p in settled:
            if completed:
                handled.extend(ids)
                log.info("prompt %s for %s completed: %d message(s) handled", mid,
                         self.identity, len(ids))
                continue
            self.marks.pop(str(p.high), None)
            unhandled.extend(ids)
            why = ("the model never saw it" if not p.seen else
                   "its turn ended in an error" if p.errored else
                   f"its turn ended {self.last_turn.get('finish') or 'without a stop'}")
            log.warning("prompt %s for %s: no cursor advance (%s); its %d message(s) are "
                        "unhandled", mid, self.identity, why, len(ids))
        if self._aborted():
            self.owe_unhandled([*handled, *unhandled])
            log.info("a turn of %s was aborted: no turn frame at the abort; its ids go "
                     "unhandled on the next one", self.identity)
            self._remind_at_turn_end(aborted=True)
            return
        self.report_turn_end(handled=handled, unhandled=unhandled)
        self._remind_at_turn_end()

    # ------------------------------------------------------------------ reply reminder
    def _remind_at_turn_end(self, *, aborted: bool = False) -> None:
        """The reply check at a `turn`. The end of the reminder's own turn counts the
        debts it named; any other turn end voids them first, as a prompt resolves a
        block on the shim. An aborted turn sends no `turn` at the abort: it is checked by
        nothing and ends any reminder."""
        with self._lock:
            reminder_turn = self._reminder is not None and self._reminder_admitted
            self._reminder, self._reminder_admitted = None, False
            blocked = self.blocked
        if aborted or (blocked and not reminder_turn):
            self.clear_block()
        if aborted:
            return
        decision = self.reply_check({})
        if decision.block and self._post_reminder(decision.block):
            self.mark_blocked(decision.names)
            return
        self.clear_block()

    def stop_facts(self, fields: dict) -> dict:
        """No other hook can hold an OpenCode turn open, and an idle is the end of the
        run loop, so no background work runs after it and the session has no crons."""
        return {**fields, "stop_hook_active": False, "task_types": [], "crons": 0}

    def _post_reminder(self, text: str) -> bool:
        """The reminder, posted into the idle session as the adapter's own line (an
        untracked turn). No digest is posted while the post is in flight; after it, the
        status read before every post finds the session busy. True once it is in."""
        mid = mint_message_id()
        with self._lock:
            self._reminder, self._reminder_admitted = mid, False
            self.turn_open = True
        try:
            done = self.deliver_line(text, message_id=mid)
        except Exception:                                         # noqa: BLE001
            log.exception("the reply reminder for %s could not be posted", self.identity)
            done = False
        with self._lock:
            if not done:
                self._reminder = None
            self.turn_open = bool(self.pending)
        self._wake.set()
        return done

    def _on_session_created(self, info: dict) -> None:
        """A top-level session this launcher did not make is the window's /new or
        /clear, and is followed: the posts still pending in the old
        session go back in the queue, in id order, their ids still held; `session_id`,
        the session file and the window's generation name the new session; then
        `ready source=clear`, which names it. A child session (`parentID` set) is not
        followed, and nothing is followed before the launch's own session exists."""
        new = info.get("id")
        if not isinstance(new, str) or not new or info.get("parentID"):
            return
        with self._lock:
            old = self.session_id
            if old is None or new == old:
                return
            requeue, lost = [], []
            for mid, p in sorted(self.pending.items(), key=lambda kv: kv[1].high):
                _, ids = self.ledger.end({"id": mid, "status": "moved"})
                self.marks.pop(str(p.high), None)
                if p.batch:
                    requeue.extend(p.batch)
                else:
                    lost.extend(ids)
            self.pending = {}
            queued = {str(q["message_id"]) for q in self.queue}
            self.queue = sorted(
                [*self.queue, *(q for q in requeue if str(q["message_id"]) not in queued)],
                key=lambda q: int(q["message_id"]))
            self.session_id = new
            self.turn_open = False
            self.busy = False
            self.last_turn = {}
            if self.generation is not None:
                self.generation.thread_id = new
        self.owe_unhandled(lost)
        with self._lock:
            self._reminder, self._reminder_admitted = None, False
        self.owed_session_changed(SOURCE_CLEAR, new)
        update_record(self.agent.session_file, session_id=new, launch=self.launch)
        log.info("the window of %s started session %s (/new or /clear); comms follows it "
                 "from session %s, %d message(s) back in the queue", self.agent.qualified,
                 new, old, len(requeue))
        self.report_ready(SOURCE_CLEAR)
        self._wake.set()

    # ------------------------------------------------------------------ the model watch
    def on_log_line(self, line: str) -> None:
        """Every line the server logs. A request line is counted for its session and
        checked against the launch default."""
        fields = parse_stream_line(line)
        if fields is None:
            return
        session = fields["session"]
        with self._lock:
            self._stream_lines[session] = self._stream_lines.get(session, 0) + 1
        self._check_model(f"{fields['provider']}/{fields['model']}", fields["small"],
                          session)

    def _watch_message(self, info: dict) -> None:
        if info.get("role") != "assistant" or not info.get("modelID"):
            return
        session, mid = info.get("sessionID"), info.get("id")
        created = (info.get("time") or {}).get("created") or 0
        model = f"{info.get('providerID')}/{info.get('modelID')}"
        if created and created < self._watch_start_ms:
            return                  # a message from before this launch, re-read
        blind = False
        with self._lock:
            known = self._assistants.setdefault(session, {})
            done = bool((info.get("time") or {}).get("completed"))
            if done and not known.get(mid):
                known[mid] = True
                self._completed_new[session] = self._completed_new.get(session, 0) + 1
                blind = self._completed_new[session] > self._stream_lines.get(session, 0)
            else:
                known.setdefault(mid, False)
                return
        # Read at completion, not creation: OpenCode creates the assistant message just
        # before the request starts, so by now the request's log line, which also says
        # whether it was a side request, has normally been read and said first.
        self._check_model(model, None, session)
        if blind:
            self._say_model(("unseen", model, session),
                            UNSEEN_REQUEST.format(session=session, model=model))

    def _check_model(self, model: str, small, session: str) -> None:
        default = self.default_model
        if default is not None and model == default:
            return
        small_text = "?" if small is None else ("true" if small else "false")
        if default is None:
            text = NO_DEFAULT_MODEL.format(session=session, model=model, small=small_text)
        else:
            text = OTHER_MODEL.format(session=session, model=model, small=small_text,
                                      default=default)
        self._say_model((model, session), text)

    def _say_model(self, key, text: str) -> None:
        with self._lock:
            if key in self.models_said:
                return
            self.models_said.add(key)
            self.model_notes.append(text)
        self.status_sink(f"{self.agent.qualified}: {text}")

    # ------------------------------------------------------------------ injection
    def mirror_session(self) -> str | None:
        with self._lock:
            return self.session_id

    def can_inject(self) -> bool:
        return (self.ready and self.fatal is None and not self.turn_open
                and not self.busy and self.session_id is not None)

    def retry_delay(self) -> float:
        return FLUSH_RETRY

    def deliver(self, text: str, high: int, identity, count: int):
        """Post the digest into the idle session. `REQUEUE` when nothing was admitted
        (no connection, the session not idle, a 5xx or 429, or a refusal); the receipt
        is comms' message id, also when the post had no answer, since the server may
        have admitted it."""
        sid = self.session_id
        try:
            status, body = self.http.json("GET", "/session/status")
        except (Unreachable, NoAnswer) as e:
            self._failed(f"status read: {e}")
            return REQUEUE
        if not idle_from_status(status, body, sid):
            # Not idle, or not known to be: nothing is posted. The stream's own status
            # events say when the session is idle again; this read never sets it busy,
            # so a read that failed can't hold the session shut.
            return REQUEUE
        mid = mint_message_id()
        with self._lock:
            self.pending[mid] = Pending(high=int(high), count=count,
                                        batch=self.digest_batch(high))
            self.ledger.injected(mid, high, self.digest_ids(high))
            self.turn_open = True
        post = {"messageID": mid, "parts": [{"type": "text", "text": text}]}
        try:
            code, answer = self.http.request("POST", f"/session/{sid}/prompt_async", post)
        except Unreachable as e:
            self._unpost(mid)
            self._failed(f"no connection: {e}")
            return REQUEUE
        except NoAnswer as e:
            with self._lock:
                p = self.pending.get(mid)
                if p is not None:
                    p.uncertain = True
                self._reconcile_wanted = True
            log.warning("post %s for %s had no answer (%s); the session's messages will "
                        "say whether it was admitted", mid, identity, e)
            return mid
        if 200 <= code < 300:
            self._succeeded()
            return mid
        self._unpost(mid)
        if code >= 500 or code == 429:
            self._failed(f"{code}")
            return REQUEUE
        self._close_for_launch(code, answer)
        return REQUEUE

    def deliver_line(self, line: str, *, message_id: str | None = None) -> bool:
        """The adapter's own line (the reply reminder)
        as a prompt of its own into the idle session, the same post a digest takes,
        tracked by nothing: no pending post, no ledger entry, so no cursor moves for it.
        `message_id` is the post's id when the caller follows its turn. False (try
        again) when the session is not idle or the server did not take it; a refusal a
        retry cannot fix gives the line up, which the status line already carries."""
        sid = self.session_id
        try:
            status, body = self.http.json("GET", "/session/status")
        except (Unreachable, NoAnswer) as e:
            log.warning("could not read the session's status to put a line in: %s", e)
            return False
        if not idle_from_status(status, body, sid):
            return False
        post = {"messageID": message_id or mint_message_id(),
                "parts": [{"type": "text", "text": line}]}
        try:
            code, _answer = self.http.request("POST", f"/session/{sid}/prompt_async", post)
        except Unreachable as e:
            log.warning("could not put a line into the session: %s", e)
            return False
        except NoAnswer:
            return True              # it may have been admitted: never post it twice
        if 200 <= code < 300:
            return True
        if code >= 500 or code == 429:
            return False
        log.warning("the server refused a line for %s (%s); it stays on the status line "
                    "only", self.agent.qualified, code)
        return True

    def delivered(self, text: str, high: int, identity, count: int, receipt) -> None:
        if self._daemon is not None:
            self.notify_injected(identity, high, text)
        log.info("injected %d message(s) for %s through %s into prompt %s",
                 count, identity, high, receipt)

    def _unpost(self, mid: str) -> None:
        with self._lock:
            self.pending.pop(mid, None)
            # Its ids stay held: the batch goes back on the queue (`REQUEUE`).
            self.ledger.end({"id": mid, "status": "not admitted"})
            self.turn_open = bool(self.pending)

    def _succeeded(self) -> None:
        with self._lock:
            self.failures = 0
            self._streak_said = False

    def _failed(self, last: str) -> None:
        with self._lock:
            self.failures += 1
            say = self.failures >= FAILURE_STREAK and not self._streak_said
            if say:
                self._streak_said = True
            count = self.failures
        if say:
            self.status_sink(FAILING.format(count=count, identity=self.agent.qualified,
                                            last=last, wait=self.retry_delay()))

    def _close_for_launch(self, code: int, detail) -> None:
        """A refusal a retry can't fix: no more posts this launch, said once."""
        meaning = MEANINGS.get(code, "a refusal a retry cannot fix")
        detail_text = str(detail or "").strip()[:300] or "(no body)"
        text = POST_REFUSED.format(identity=self.agent.qualified, status=code,
                                   meaning=meaning, detail=detail_text)
        if code == 400:
            text += POST_REFUSED_CONTENT
        with self._lock:
            if self.fatal is not None:
                return
            self.fatal = text
        self.status_sink(text)

    # ------------------------------------------------------------------ supervision
    def supervise_port(self) -> bool:
        """Bring a lost server back, reopen a cut or silent stream, and reconcile a
        post that had no answer. Each on the port's own backoff."""
        try:
            server_down = self.server is not None and self.spawn and not self.server.alive
            if server_down:
                with self._lock:
                    self.ready = False
                if self.generation is not None:
                    self.generation.end()
                log.warning("the opencode server for %s exited (%s); starting it again",
                            self.agent.qualified, self.server.returncode)
                # The new server reads its config afresh: withdraw the agent model the
                # daemon holds until it is read again, so a stale one decides nothing.
                self._agent_model = None
                self.report_refresh()
                self._bring_up(self.default_model)
                self.probe()
                self.open_generation()
                self.report_refresh()
            elif self.stream is None or self.stream.closed.is_set():
                self.connect_stream()
            elif self._reconcile_wanted:
                self.reconcile()
            self._server_delays = None
            return True
        except (OpenCodeOffline, Unreachable, NoAnswer) as e:
            self.trouble = str(e)
            log.warning("opencode server for %s not reachable: %s", self.agent.qualified,
                        e)
            if self._server_delays is None:
                self._server_delays = self._backoff or backoff_delays()
            self._stop.wait(next(self._server_delays))
            return False

    # ------------------------------------------------------------------ the window's view
    def generation_answers(self, generation: Generation, timeout: float) -> bool:
        if generation.ended.is_set():
            return False
        try:
            status, _ = Http(generation.url, self.password, timeout=timeout).request(
                "GET", "/global/health")
        except (Unreachable, NoAnswer):
            return False
        return status == 200

    def drop_generation(self, generation: Generation) -> None:
        generation.end()
        if self.stream is not None:
            self.stream.close("the window's server stopped answering")

    def give_up_reason(self) -> str:
        if self.trouble:
            return self.trouble
        if self.generation is None:
            return "the launcher never connected to its opencode server"
        return "the opencode server connection never became ready"

    def stop_before_unbind(self) -> None:
        if self.generation is not None:
            self.generation.end()
        if self.stream is not None:
            self.stream.close()

    def stop_after_unbind(self) -> None:
        if self.spawn and self.server is not None:
            self.server.stop()


# --------------------------------------------------------------------------- main

def build_parser():
    import argparse

    ap = argparse.ArgumentParser(
        prog="moxi-comms-opencode",
        description="an OpenCode session with comms mail, in this terminal: one launcher "
                    "per identity; closing the window takes the identity offline")
    ap.add_argument("identity", help="qualified roster identity, e.g. coder@proj-a")
    ap.add_argument("--opencode", help="the opencode binary (default: the roster's "
                                       "`binary`, else `opencode` on PATH, else "
                                       f"{opencode_roster.INSTALL_DIR}/opencode)")
    ap.add_argument("--ready-wait", type=float, default=READY_WAIT, metavar="SECONDS",
                    help="how long to wait for the session to be ready before giving up "
                         f"with exit 3 (default {READY_WAIT:.0f})")
    ap.add_argument("--notice-hold", type=float, default=NOTICE_HOLD, metavar="SECONDS",
                    help="how long a launch notice stays on screen before the window "
                         f"opens (default {NOTICE_HOLD:.0f})")
    ap.add_argument("--config", metavar="PATH",
                    help="a comms config file, in config.toml's format, whose [opencode] "
                         "table is this launch's roster in place of the box's config "
                         "file's; the file is checked whole, and every other setting "
                         "still comes from the box's config file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan for this identity and exit; spawn nothing")
    ap.add_argument("--log-level", default="INFO")
    return ap


def launcher_log_file(qualified: str) -> Path:
    from ..paths import opencode_sessions_dir

    return opencode_sessions_dir() / f"{qualified}.log"


def server_log_file(qualified: str) -> Path:
    from ..paths import opencode_sessions_dir

    return opencode_sessions_dir() / f"{qualified}.server.log"


def print_plan(agent, roster, *, binary, version, comms_path, out=None) -> None:
    from .opencode_server import serve_argv

    out = out or sys.stdout

    def say(text):
        print(text, file=out)

    say(f"identity   {agent.qualified} (slot {agent.slot}, pinned by the roster)")
    say(f"  roster     {roster.source}")
    say(f"  cwd        {agent.cwd}")
    say(f"  session    {agent.session_file} (a new session at this launch)")
    if agent.directors:
        say(f"  directors  {', '.join(agent.directors)} "
            "(verified messages from these render as directives)")
    say(f"  model      {', then '.join(agent.models)} (the launch default, if offered "
        "free; comms never changes it after launch)")
    say(f"  refresh    {binary or opencode_roster.BINARY} {' '.join(MODELS_REFRESH_ARGS)} "
        f"before the server starts, within {MODELS_REFRESH_TIMEOUT:g}s; the model is "
        "chosen from the refreshed list")
    say(f"  opencode   {binary or 'opencode (does not resolve)'} ({version})")
    say(f"  comms      {comms_path or BARE_COMMS + ' (this environment has no moxi script)'}")
    say("  spawn      " + " ".join(serve_argv(binary or opencode_roster.BINARY)))
    say("  window     " + f"{binary or opencode_roster.BINARY} attach <url> -s <session>")
    say(f"  log        {launcher_log_file(agent.qualified)}")


def main(argv=None) -> int:  # pragma: no cover - exercised end to end by the window tests
    import signal

    from .opencode_window import EXIT_GAVE_UP, GaveUp, OpenCodeWindow
    from .terminal import JOB_CONTROL_SIGNALS, Terminal, in_terminal_foreground, terminal_fd

    args = build_parser().parse_args(argv)
    try:
        roster = opencode_roster.load(path=args.config)
        agent = roster.require(args.identity)
    except ConfigError as e:
        print(f"moxi-comms-opencode: {e}", file=sys.stderr)
        return 2
    binary = opencode_roster.resolve_binary(args.opencode or agent.binary)
    comms_path = own_comms()
    version = binary_version(binary, env=agent.env()) if binary else "not found"
    print_plan(agent, roster, binary=binary, version=version, comms_path=comms_path)
    sys.stdout.flush()
    if binary is None:
        print("moxi-comms-opencode: no opencode binary resolves (the roster's `binary`, "
              "`opencode` on PATH, or the installer's directory); install OpenCode or "
              "name it with --opencode.", file=sys.stderr)
        return 2
    if comms_path is None:
        print("moxi-comms-opencode: this environment has no `moxi` script, so the agent "
              "could not be told which comms to run.", file=sys.stderr)
        return 2
    if args.dry_run:
        return 0
    tty = terminal_fd()
    if not in_terminal_foreground(tty):
        print("moxi-comms-opencode: run this in the foreground of a terminal: the OpenCode "
              "window takes that terminal.", file=sys.stderr)
        return 2
    log_path = launcher_log_file(agent.qualified)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(log_path.parent, 0o700)
    logging.basicConfig(level=args.log_level, filename=str(log_path),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(f"moxi-comms-opencode: logging to {log_path}", file=sys.stderr, flush=True)

    adapter = OpenCodeAdapter(agent, binary=binary, comms_path=comms_path,
                              log_path=server_log_file(agent.qualified))
    stopping = threading.Event()
    hangup = threading.Event()

    def _bye(signum, _frame):
        log.info("signal %d: stopping", signum)
        stopping.set()

    def _hangup(signum, _frame):
        log.info("hangup: stopping")
        hangup.set()
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
        signal.signal(sig, _bye)
    signal.signal(signal.SIGHUP, _hangup)
    for sig in JOB_CONTROL_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    try:
        try:
            adapter.start()
        except (DaemonOffline, DaemonError, OpenCodeOffline, ConfigError) as e:
            print(f"moxi-comms-opencode: {e}", file=sys.stderr, flush=True)
            return 4 if isinstance(e, DaemonOffline) else 2
        log.info("moxi-comms-opencode running for %s", agent.qualified)
        show_notice(adapter, args.notice_hold, stopping)
        session = OpenCodeWindow(adapter, binary=binary, terminal=Terminal(tty),
                                 log_path=log_path, stopping=stopping, hangup=hangup,
                                 ready_wait=args.ready_wait, env=adapter.window_env())
        try:
            return session.run()
        except GaveUp as e:
            log.error("gave up: %s", e)
            if not hangup.is_set():
                reason = str(e).rstrip(". ")
                print(f"moxi-comms-opencode: gave up on {agent.qualified}: {reason}. The log "
                      f"is {log_path}.", file=sys.stderr, flush=True)
            return EXIT_GAVE_UP
    finally:
        adapter.stop()
        if not hangup.is_set():
            say_after(adapter)


def show_notice(adapter, hold: float, stopping=None, out=None) -> None:
    """The launch notices, in the launcher's own terminal before the window opens, held
    on screen for `hold` seconds so it can be read before the window covers it. The
    window runs on its own screen, so the line is there again when the window closes."""
    if not adapter.launch_notices:
        return
    out = out or sys.stderr
    for notice in adapter.launch_notices:
        print(f"moxi-comms-opencode: {adapter.agent.qualified}: {notice}", file=out,
              flush=True)
    if hold > 0:
        print(f"moxi-comms-opencode: the window opens in {hold:g}s", file=out, flush=True)
        if stopping is not None:
            stopping.wait(hold)
        else:
            time.sleep(hold)


def say_after(adapter, out=None) -> None:
    """After the window: what the window covered, said where the person now looks."""
    out = out or sys.stderr
    for note in adapter.model_notes:
        print(f"moxi-comms-opencode: {adapter.agent.qualified}: {note}", file=out, flush=True)
    if adapter.fatal:
        print(f"moxi-comms-opencode: {adapter.fatal}", file=out, flush=True)


__all__ = ["ADAPTER_KIND", "FALLBACK_MODEL", "MODELS_STALE", "NO_FREE_MODEL",
           "SHIPPED_DEFAULT", "OpenCodeAdapter",
           "OpenCodeOffline", "build_parser", "choose_default",
           "free_models", "idle_from_status", "launch_config", "main", "mint_message_id",
           "read_record", "show_notice", "status_lines"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
