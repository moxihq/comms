"""A fake `codex app-server` that speaks the VERIFIED protocol semantics.

Every behaviour below was measured on codex-cli 0.160.0, except the phantom turn id
(measured on 0.146.0); none of it is invented. The ones that matter:

  * **WebSocket on a unix socket, and `permessage-deflate` is REFUSED.** A client that
  offers the extension gets no connection at all.
  * **`initialize` is mandatory**: any other call first is `-32600 "Not initialized"`.
  * **resume is the subscription.** A connection that has not started or resumed a
    thread receives NO notifications for it -- not `turn/started`, not `item/*`, not
    `turn/completed`. A bare injector sees exactly two `thread/status/changed` frames and
    nothing else.
  * **`turn/start` during an active turn returns a PHANTOM id.** The call
    succeeds, the input is absorbed into the RUNNING turn (its items carry the running
    turn's id), and the returned id never appears anywhere again.
  * **stale/bogus/malformed steer while idle** is `-32600 "no active turn to steer"`,
    and **nothing is injected**.
  * **wrong `expectedTurnId` while active** is `-32600 "expected active turn id `X` but
    found `Y`"`, and nothing is injected.
  * **turns are elastic:** N steers into one turn append N `userMessage`
    items and produce exactly ONE `turn/completed` at the end.
  * **`turn/completed` is not a success signal:** `status` is one of
    `completed | interrupted | failed`.
  * **The first load of a thread fixes its sandbox** (codex-cli 0.160.0). `thread/start`
    loads a thread with the sandbox it carries; a `thread/resume` of a thread that is
    not loaded in this process loads it with the sandbox the resume carries, and with
    none it loads read-only with the network off. A later resume of a loaded thread
    changes nothing. Both responses report the loaded `sandbox` policy.
  * **A resume of a thread with no rollout is refused** (`-32600 no rollout found for
    thread id <id>`): a thread that is not loaded and never ran a turn has nothing on
    disk to load.

Codex's own hooks (design §4.2a), as checked live on codex-cli 0.153.0, run through
`hook_runner(event, input)` when a test sets one:

  * **SessionStart** is not run at `thread/start`: the source is queued and the hook runs
    when the thread's next turn starts (`startup` for a new thread, `resume` for one a
    resume loaded from disk, `compact` after a compaction). Its input has no `turn_id`.
  * **UserPromptSubmit** at each turn's start and again for input steered into it, with
    the running turn's `turn_id`.
  * **Stop** before a `completed` turn's `turn/completed`; **Interrupt** before an
    `interrupted` one's (`complete_turn(..., hook=False)` is an abort with no hook). A
    `failed` turn runs no hook.
  * **PostCompact** at a compaction (`compact`), with a `turn_id`: a manual one runs as a
    turn of its own (started, compacted, completed, with no prompt and no Stop); an
    automatic one inside the running turn, followed at once by SessionStart `compact`.

  * A Stop hook that prints `{"decision":"block","reason":...}` holds the turn open:
    the reason is recorded in the turn as a hook prompt (no UserPromptSubmit runs for
    it), no `turn/completed` comes, and the turn's next Stop carries
    `stop_hook_active: true` (codex-rs core/src/session/turn.rs:511-546).

`hooks/list` answers from `hook_states` (`{key: (trustStatus, currentHash)}`).
`thread/backgroundTerminals/list` answers from `background_terminals`
(`{thread id: [terminal]}`), or with an error while `background_error` is set.

The model is not simulated: a turn completes when the test says so, which is the only
part of a real app-server that costs tokens and time.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http import HTTPStatus

from websockets.sync.server import unix_serve

INVALID_REQUEST = -32600


def _uuid() -> str:
    return str(uuid.uuid4())


class _Conn:
    def __init__(self, ws):
        self.ws = ws
        self.initialized = False
        self.subscriptions: set = set()
        self.lock = threading.Lock()

    def send(self, frame: dict) -> None:
        with self.lock:
            try:
                self.ws.send(json.dumps(frame))
            except Exception:
                pass


class FakeAppServer:
    """A real WebSocket server on a real unix socket. No network, no codex binary."""

    def __init__(self, socket_path, store=None):
        self.socket_path = str(socket_path)
        #: Optional JSON file the threads persist in, so a second process (a respawned
        #: app-server) finds them on "disk", not loaded.
        self.store = store
        self.threads: dict = {}
        self.calls: list = []
        self.conns: list = []
        self.phantom_ids: list = []
        #: `hook_runner(event, input)` runs one of Codex's hooks; None runs none.
        self.hook_runner = None
        #: What `hooks/list` reports: `{key: (trustStatus, currentHash)}`.
        self.hook_states: dict = {}
        self.hook_calls: list = []
        #: `{thread id: [terminal]}` for `thread/backgroundTerminals/list`.
        self.background_terminals: dict = {}
        #: Set: `thread/backgroundTerminals/list` answers an error.
        self.background_error = False
        #: (turn id, reason) of every Stop block the hook printed.
        self.blocks: list = []
        self._lock = threading.RLock()
        self._server = None
        self._thread = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FakeAppServer":
        self._server = unix_serve(self._handle, path=self.socket_path,
                                  compression=None, process_request=self._no_deflate)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @staticmethod
    def _no_deflate(connection, request):
        """The real server rejects permessage-deflate; a compressing client never
        connects (`InvalidMessage: did not receive a valid HTTP response`)."""
        offered = request.headers.get("Sec-WebSocket-Extensions", "")
        if "permessage-deflate" in offered:
            return connection.respond(HTTPStatus.BAD_REQUEST, "no compression\n")
        return None

    # ------------------------------------------------------------------ the wire
    def _handle(self, ws) -> None:
        conn = _Conn(ws)
        with self._lock:
            self.conns.append(conn)
        try:
            for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                for line in raw.splitlines():
                    if line.strip():
                        self._dispatch(conn, json.loads(line))
        except Exception:
            pass
        finally:
            with self._lock:
                if conn in self.conns:
                    self.conns.remove(conn)

    def _dispatch(self, conn: _Conn, msg: dict) -> None:
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        with self._lock:
            self.calls.append((method, params))
        if rid is None:
            return                                    # a client notification
        if not conn.initialized and method != "initialize":
            return conn.send({"id": rid, "error": {"code": INVALID_REQUEST,
                                                   "message": "Not initialized"}})
        try:
            result = self._handle_request(conn, method, params)
        except _Error as e:
            return conn.send({"id": rid, "error": {"code": e.code, "message": e.message}})
        conn.send({"id": rid, "result": result})

    def _handle_request(self, conn: _Conn, method: str, params: dict) -> dict:
        if method == "initialize":
            conn.initialized = True
            return {"userAgent": "fake-app-server/0.160.0", "codexHome": "/dev/null",
                    "platformFamily": "unix", "platformOs": "linux"}
        if method == "thread/start":
            return self._thread_start(conn, params)
        if method == "thread/resume":
            return self._thread_resume(conn, params)
        if method == "thread/turns/list":
            thread = self._thread_of(params)
            return {"data": list(reversed(thread["turns"])), "nextCursor": None}
        if method == "thread/loaded/list":
            with self._lock:
                return {"data": list(self.threads), "nextCursor": None}
        if method == "turn/start":
            return self._turn_start(params)
        if method == "turn/steer":
            return self._turn_steer(params)
        if method == "turn/interrupt":
            self.complete_turn(params.get("threadId"), status="interrupted")
            return {}
        if method == "thread/backgroundTerminals/list":
            if self.background_error:
                raise _Error(-32603, "background terminals unavailable")
            thread = self._thread_of(params)
            with self._lock:
                data = list(self.background_terminals.get(thread["id"], ()))
            return {"data": data, "nextCursor": None}
        if method == "hooks/list":
            hooks = [{"key": key, "enabled": True, "trustStatus": trust,
                      "currentHash": current}
                     for key, (trust, current) in self.hook_states.items()]
            return {"data": [{"cwd": (params.get("cwds") or [None])[0], "hooks": hooks,
                              "warnings": [], "errors": []}]}
        raise _Error(-32601, f"method not found: {method}")

    # ------------------------------------------------------------------ threads
    def _thread_of(self, params: dict) -> dict:
        with self._lock:
            thread = self.threads.get(params.get("threadId"))
        if thread is None:
            raise _Error(INVALID_REQUEST, f"thread not found: {params.get('threadId')}")
        return thread

    @staticmethod
    def sandbox_policy(params: dict) -> dict:
        """The policy a start or a first resume loads a thread with."""
        mode = params.get("sandbox")
        settings = (params.get("config") or {}).get("sandbox_workspace_write") or {}
        if mode == "workspace-write":
            return {"type": "workspaceWrite",
                    "writableRoots": list(settings.get("writable_roots") or []),
                    "networkAccess": bool(settings.get("network_access")),
                    "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
        if mode == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if params.get("permissions"):
            return {"type": "externalSandbox", "networkAccess": "enabled"}
        return {"type": "readOnly", "networkAccess": False}

    def unload_all(self) -> None:
        """What a new app-server process sees: every thread on disk, none loaded."""
        with self._lock:
            for thread in self.threads.values():
                thread["loaded"] = None

    def _save(self) -> None:
        if not self.store:
            return
        import os
        with self._lock:
            data = {tid: {k: v for k, v in t.items() if k != "loaded"}
                    for tid, t in self.threads.items()}
        tmp = f"{self.store}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.store)

    def load_store(self) -> None:
        if not self.store:
            return
        import os
        if os.path.exists(self.store):
            with open(self.store) as f:
                data = json.load(f)
            for thread in data.values():
                thread["loaded"] = None
                for turn in thread["turns"]:
                    if turn["status"] == "inProgress":
                        turn["status"] = "interrupted"   # the process that ran it is gone
                thread["status"] = {"type": "idle"}
            with self._lock:
                self.threads.update(data)

    def _thread_view(self, thread: dict) -> dict:
        return {"id": thread["id"], "status": thread["status"], "cwd": thread["cwd"],
                "createdAt": thread["created_at"], "turns": []}

    def _thread_start(self, conn: _Conn, params: dict) -> dict:
        thread = {"id": _uuid(), "status": {"type": "idle"}, "turns": [],
                  "cwd": params.get("cwd"), "created_at": int(time.time()),
                  "start_params": dict(params), "loaded": self.sandbox_policy(params),
                  "session_start": "startup"}
        with self._lock:
            self.threads[thread["id"]] = thread
        self._save()
        conn.subscriptions.add(thread["id"])     # starting subscribes this connection
        self._notify(thread["id"], "thread/started", {"thread": self._thread_view(thread)})
        return {"thread": self._thread_view(thread),
                "approvalPolicy": params.get("approvalPolicy"),
                "sandbox": thread["loaded"],
                "cwd": params.get("cwd"), "model": params.get("model")}

    def _thread_resume(self, conn: _Conn, params: dict) -> dict:
        thread = self._thread_of(params)
        with self._lock:
            if thread.get("loaded") is None:
                if not thread["turns"]:
                    raise _Error(INVALID_REQUEST,
                                 f"no rollout found for thread id {thread['id']}")
                thread["loaded"] = self.sandbox_policy(params)
                thread["session_start"] = "resume"   # loaded from its rollout
        conn.subscriptions.add(thread["id"])     # THIS is the subscription
        # Note what is NOT here: any active-turn id. The only live signal is
        # thread.status, which is why a reconnecting adapter needs thread/turns/list.
        return {"thread": self._thread_view(thread),
                "approvalPolicy": thread["start_params"].get("approvalPolicy"),
                "sandbox": thread["loaded"], "cwd": thread["cwd"],
                "initialTurnsPage": None, "turnsBackwardsCursor": None}

    # ------------------------------------------------------------------ turns
    def _active(self, thread: dict):
        for turn in thread["turns"]:
            if turn["status"] == "inProgress":
                return turn
        return None

    def _user_item(self, thread: dict, turn: dict, text: str) -> None:
        item = {"type": "userMessage", "id": _uuid(), "clientId": None,
                "content": [{"type": "text", "text": text, "text_elements": []}]}
        turn["items"].append(item)
        for event in ("item/started", "item/completed"):
            self._notify(thread["id"], event, {"item": item, "threadId": thread["id"],
                                               "turnId": turn["id"]})

    def _text_of(self, params: dict) -> str:
        return "".join(part.get("text", "") for part in params.get("input") or [])

    def _turn_start(self, params: dict) -> dict:
        thread = self._thread_of(params)
        active = self._active(thread)
        if active is not None:
            # THE PHANTOM: the call succeeds, the input is absorbed into the
            # RUNNING turn, and this id never appears in a started/item/completed again.
            phantom = _uuid()
            with self._lock:
                self.phantom_ids.append(phantom)
            self._user_item(thread, active, self._text_of(params))
            self._hook("UserPromptSubmit", thread, turn_id=active["id"],
                       prompt=self._text_of(params))
            return {"turn": {"id": phantom, "items": [], "status": "inProgress",
                             "error": None}}
        turn = {"id": _uuid(), "items": [], "status": "inProgress", "error": None,
                "startedAt": int(time.time()), "completedAt": None, "durationMs": None}
        thread["turns"].append(turn)
        thread["status"] = {"type": "active", "activeFlags": []}
        self._notify(thread["id"], "thread/status/changed",
                     {"threadId": thread["id"], "status": thread["status"]})
        self._notify(thread["id"], "turn/started",
                     {"threadId": thread["id"], "turn": dict(turn)})
        self._session_start(thread)
        self._hook("UserPromptSubmit", thread, turn_id=turn["id"],
                   prompt=self._text_of(params))
        self._user_item(thread, turn, self._text_of(params))
        return {"turn": dict(turn)}

    def _turn_steer(self, params: dict) -> dict:
        thread = self._thread_of(params)
        active = self._active(thread)
        if active is None:
            # Stale, bogus and malformed ids are ALL this one error, and nothing lands.
            raise _Error(INVALID_REQUEST, "no active turn to steer")
        if params.get("expectedTurnId") != active["id"]:
            raise _Error(INVALID_REQUEST,
                         f"expected active turn id `{params.get('expectedTurnId')}` "
                         f"but found `{active['id']}`")
        self._user_item(thread, active, self._text_of(params))
        self._hook("UserPromptSubmit", thread, turn_id=active["id"],
                   prompt=self._text_of(params))
        return {"turnId": active["id"]}            # elastic: still ONE completion

    # ------------------------------------------------------------------ hooks
    def _hook(self, event: str, thread: dict, **fields):
        """Run one hook; what it printed (a Stop's block), or None."""
        payload = {"session_id": thread["id"], "hook_event_name": event,
                   "cwd": thread.get("cwd"), "transcript_path": None, "model": "fake",
                   **fields}
        with self._lock:
            self.hook_calls.append((event, dict(payload)))
            runner = self.hook_runner
        if runner is not None:
            return runner(event, payload)
        return None

    def _session_start(self, thread: dict) -> None:
        with self._lock:
            source, thread["session_start"] = thread.get("session_start"), None
        if source:
            self._hook("SessionStart", thread, source=source)

    @staticmethod
    def hook_states_from_argv(argv) -> dict:
        """What Codex 0.153 lists for the session-flag hooks on an app-server argv: each
        `-c hooks.<Event>=[...]` handler under `/<session-flags>/config.toml:<label>:0:0`,
        `trusted` when `-c hooks.state={...}` holds its hash, else `untrusted`."""
        import hashlib
        import re
        import tomllib
        values = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]
        hooks, state = {}, {}
        for value in values:
            key, _, rest = value.partition("=")
            if key == "hooks.state":
                state = tomllib.loads(f"v = {rest}")["v"]
            elif key.startswith("hooks."):
                hooks[key.split(".", 1)[1]] = tomllib.loads(f"v = {rest}")["v"]
        listed = {}
        for event, groups in hooks.items():
            label = re.sub(r"(?<!^)([A-Z])", r"_\1", event).lower()
            handler = groups[0]["hooks"][0]
            blob = json.dumps({"event_name": label, "hooks": [handler]}, sort_keys=True,
                              separators=(",", ":"))
            current = "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
            key = f"/<session-flags>/config.toml:{label}:0:0"
            trusted = (state.get(key) or {}).get("trusted_hash") == current
            listed[key] = ("trusted" if trusted else "untrusted", current)
        return listed

    # ------------------------------------------------------------------ test control
    def operator_turn(self, thread_id: str, text: str = "typed into the TUI") -> str:
        """A turn the OPERATOR started in a viewer. A resumed adapter sees its whole
        lifecycle including the completion, and must not advance a cursor for it."""
        return self._turn_start({"threadId": thread_id,
                                 "input": [{"type": "text", "text": text}]})["turn"]["id"]

    def complete_turn(self, thread_id: str, status: str = "completed", *,
                      hook: bool = True, stop_hook_active: bool | None = None) -> str | None:
        """Finish the live turn: its hook first (Stop for `completed`, Interrupt for
        `interrupted`, none for `failed` or with `hook=False`), then `turn/completed`.

        A Stop that blocks holds the turn open: its reason goes into the turn as a hook
        prompt, nothing completes, and None is returned. `stop_hook_active` is the
        Stop's field; by default true once a Stop of this turn blocked."""
        thread = self._thread_of({"threadId": thread_id})
        turn = self._active(thread)
        if turn is None:
            return None
        if hook and status == "completed" and not turn.get("compaction"):
            active = (bool(turn.get("stop_hook_active")) if stop_hook_active is None
                      else stop_hook_active)
            printed = self._hook("Stop", thread, turn_id=turn["id"],
                                 stop_hook_active=active, last_assistant_message="ok")
            if (isinstance(printed, dict) and printed.get("decision") == "block"
                    and isinstance(printed.get("reason"), str)):
                item = {"type": "hookPrompt", "id": _uuid(), "text": printed["reason"]}
                turn["items"].append(item)
                turn["stop_hook_active"] = True
                self.blocks.append((turn["id"], printed["reason"]))
                return None
        elif hook and status == "interrupted":
            self._hook("Interrupt", thread, turn_id=turn["id"])
        turn["status"] = status
        turn["completedAt"] = int(time.time())
        thread["status"] = {"type": "idle"}
        self._save()
        self._notify(thread_id, "turn/completed",
                     {"threadId": thread_id, "turn": dict(turn)})
        self._notify(thread_id, "thread/status/changed",
                     {"threadId": thread_id, "status": thread["status"]})
        return turn["id"]

    def compact(self, thread_id: str, *, auto: bool = False) -> str:
        """A compaction. Automatic: inside the running turn, PostCompact (`auto`) then
        SessionStart `compact` at once. Manual: a turn of its own, started and completed
        around PostCompact (`manual`), and SessionStart `compact` at the next turn."""
        thread = self._thread_of({"threadId": thread_id})
        if auto:
            turn = self._active(thread)
            self._hook("PostCompact", thread, turn_id=turn["id"], trigger="auto")
            self._hook("SessionStart", thread, source="compact")
            return turn["id"]
        turn = {"id": _uuid(), "items": [], "status": "inProgress", "error": None,
                "startedAt": int(time.time()), "completedAt": None, "durationMs": None,
                "compaction": True}
        thread["turns"].append(turn)
        thread["status"] = {"type": "active", "activeFlags": []}
        self._notify(thread_id, "turn/started", {"threadId": thread_id, "turn": dict(turn)})
        self._hook("PostCompact", thread, turn_id=turn["id"], trigger="manual")
        with self._lock:
            thread["session_start"] = "compact"
        self.complete_turn(thread_id)
        return turn["id"]

    def active_turn_id(self, thread_id: str) -> str | None:
        turn = self._active(self._thread_of({"threadId": thread_id}))
        return turn["id"] if turn else None

    def user_texts(self, thread_id: str) -> list:
        thread = self._thread_of({"threadId": thread_id})
        return [part["text"] for turn in thread["turns"] for item in turn["items"]
                if item["type"] == "userMessage" for part in item["content"]]

    def turn_texts(self, thread_id: str, turn_id: str) -> list:
        thread = self._thread_of({"threadId": thread_id})
        for turn in thread["turns"]:
            if turn["id"] == turn_id:
                return [part["text"] for item in turn["items"]
                        if item["type"] == "userMessage" for part in item["content"]]
        return []

    def only_thread(self) -> str:
        with self._lock:
            return next(iter(self.threads))

    def methods(self) -> list:
        with self._lock:
            return [m for m, _ in self.calls]

    # ------------------------------------------------------------------ notifications
    def _notify(self, thread_id: str, method: str, params: dict) -> None:
        """Subscribers ONLY. A connection that never resumed this thread hears nothing."""
        frame = {"jsonrpc": "2.0", "method": method, "params": params,
                 "emittedAtMs": int(time.time() * 1000)}
        with self._lock:
            targets = [c for c in self.conns if thread_id in c.subscriptions]
        for conn in targets:
            conn.send(frame)


class _Error(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["FakeAppServer"]
