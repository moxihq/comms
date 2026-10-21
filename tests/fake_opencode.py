"""A fake OpenCode server, in-process or as a stub `opencode` executable.

It speaks the parts of OpenCode 1.18.34's server that comms uses, with the semantics
measured on the real one: HTTP basic auth with user `opencode`; `POST /session` mints the
id; `prompt_async` keeps the caller's `msg_` id, answers 204, and a re-post of an id it
already has runs no turn; an unknown session is a 404, an id without the `ses` prefix a
500; `GET /session/status` is `{}` when idle and names a running session as busy; the
event stream (`GET /event`) carries `server.connected`, `server.heartbeat`,
`message.updated` (the user message, then each assistant message parented to it, its
final update sent twice), `session.status` and `session.idle`; and one
`message=stream ...` log line per model request, at INFO.

As an executable (`install`), it answers:

    opencode serve --hostname H --port 0 --print-logs --log-level INFO
    opencode attach <url> -s <session>      a stub window
    opencode --version
    opencode db path                        the database path, from FAKE_OPENCODE_DB
                                            (exit 1 when it is unset)
    opencode debug config                   FAKE_OPENCODE_CONFIG, a JSON object (exit 1
                                            when it is "fail")
    opencode models --refresh ...           the model-list refresh: exit 0 and "Models
                                            cache refreshed" on stderr, as 1.18.34 does,
                                            and FAKE_OPENCODE_DIR/refreshed marked

driven by the environment:

    FAKE_OPENCODE_DIR      records: events.jsonl, sessions.json (kept across restarts)
    FAKE_OPENCODE_WINDOW   comma-separated window behaviours: alt, exit-after=S,
                           quit-file (exit once FAKE_OPENCODE_DIR/quit appears)
    FAKE_OPENCODE_FREE     comma-separated provider/model ids offered free
                           (default: opencode/minimax-m3.1-flash-free,opencode/big-pickle)
    FAKE_OPENCODE_AUTO     "0" to leave turns running until a control call ends them
    FAKE_OPENCODE_FREE_REFRESHED
                           what a server offers free once a refresh has run (default:
                           FAKE_OPENCODE_FREE): the list OpenCode fetched replaces the
                           one it had cached
    FAKE_OPENCODE_REFRESH  how the refresh fails, as 1.18.34 can: "fetch-fail" (exit 0,
                           the `Failed to fetch models.dev` line on stderr, the list
                           unchanged), "exit" (exit 1) or "slow" (sleeps 30 s)

and a control route on the server itself, `POST /__fake/<op>` with a JSON body.
"""

from __future__ import annotations

import base64
import json
import os
import random
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "1.18.34 (fake)"
FREE_DEFAULT = ["opencode/minimax-m3.1-flash-free", "opencode/big-pickle"]


def mint(prefix: str) -> str:
    ms = int(time.time() * 1000)
    tail = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(14))
    return f"{prefix}_{(ms * 0x1000 + random.randint(1, 4000)) & 0xFFFFFFFFFFFF:012x}{tail}"


def stream_line(provider, model, session, small=False, agent="build") -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return (f"timestamp={stamp} level=INFO run=fake0001 message=stream "
            f"providerID={provider} modelID={model} session.id={session} "
            f"small={'true' if small else 'false'} agent={agent} mode=primary\n")


class FakeOpenCode:
    """One fake server. Thread-safe; every state change is one event or more."""

    def __init__(self, *, password="pw", free=None, paid=(), default_model=None,
                 heartbeat=10.0, auto=True, on_log=None, store=None):
        self.password = password
        self.free = list(free if free is not None else FREE_DEFAULT)
        self.paid = list(paid)
        self.default_model = default_model
        self.heartbeat = heartbeat
        self.auto = auto
        self.on_log = on_log or (lambda line: None)
        self.store = Path(store) if store else None
        self.lock = threading.RLock()
        self.sessions: dict = {}
        self.clients: list = []
        self.requests: list = []           # (method, path, body) of every request
        self.stalled = False
        self.post_answers: list = []       # (status, body) to answer the next posts with
        self.status_answers: list = []     # (status, raw body) for the next status reads
        self.admit_delay = 0.0
        self._stop = threading.Event()
        self.httpd = None
        self.url = None
        self._load()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "FakeOpenCode":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._heartbeats, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.close_streams()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def _heartbeats(self):
        while not self._stop.wait(self.heartbeat):
            self.emit("server.heartbeat", {})

    def _load(self):
        if self.store and self.store.exists():
            self.sessions = json.loads(self.store.read_text())

    def _save(self):
        if self.store:
            self.store.write_text(json.dumps(self.sessions))

    # ------------------------------------------------------------- events
    def emit(self, kind: str, props: dict) -> None:
        data = json.dumps({"id": mint("evt"), "type": kind, "properties": props})
        with self.lock:
            if self.stalled:
                return
            clients = list(self.clients)
        for c in clients:
            c.put(data)

    def close_streams(self) -> None:
        with self.lock:
            clients, self.clients = list(self.clients), []
        for c in clients:
            c.put(None)

    # ------------------------------------------------------------- the http side
    def authorized(self, handler) -> bool:
        if not self.password:
            return True
        want = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        return handler.headers.get("authorization") == f"Basic {want}"

    def reply(self, handler, status: int, body=None, raw: str | None = None):
        data = (raw if raw is not None else
                ("" if body is None else json.dumps(body))).encode()
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def handle(self, handler, method: str):
        path = handler.path.split("?", 1)[0]
        length = int(handler.headers.get("content-length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        with self.lock:
            self.requests.append((method, path, body))
        if path.startswith("/__fake/"):
            return self.reply(handler, 200, self.control(path[len("/__fake/"):], body or {}))
        if not self.authorized(handler):
            return self.reply(handler, 401)
        if method == "GET" and path == "/event":
            return self.serve_events(handler)
        if method == "GET" and path == "/global/health":
            return self.reply(handler, 200, {"healthy": True, "version": VERSION})
        if method == "GET" and path == "/config/providers":
            return self.reply(handler, 200, self.providers())
        if method == "GET" and path == "/session/status":
            with self.lock:
                if self.status_answers:
                    status, text = self.status_answers.pop(0)
                    return self.reply(handler, status, raw=text)
                busy = {sid: {"type": s["status"]} for sid, s in self.sessions.items()
                        if s["status"] != "idle"}
            return self.reply(handler, 200, busy)
        if method == "POST" and path == "/session":
            return self.reply(handler, 200, self.create_session((body or {}).get("title")))
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "session":
            sid = parts[1]
            if not sid.startswith("ses"):
                return self.reply(handler, 500, {"name": "UnknownError", "data": {
                    "message": "Unexpected server error."}})
            with self.lock:
                session = self.sessions.get(sid)
            if session is None:
                return self.reply(handler, 404, {"name": "NotFoundError", "data": {
                    "message": f"Session not found: {sid}"}})
            if method == "GET" and len(parts) == 2:
                return self.reply(handler, 200, {"id": sid, "title": session["title"]})
            if method == "GET" and parts[2:] == ["message"]:
                with self.lock:
                    return self.reply(handler, 200, list(session["messages"]))
            if method == "POST" and parts[2:] == ["prompt_async"]:
                with self.lock:
                    if self.post_answers:
                        status, answer = self.post_answers.pop(0)
                        return self.reply(handler, status, answer)
                mid = (body or {}).get("messageID")
                if not mid or not str(mid).startswith("msg"):
                    return self.reply(handler, 400, {"name": "BadRequest", "data": {
                        "message": "Expected a string starting with \"msg\""}})
                if self.admit_delay:
                    time.sleep(self.admit_delay)
                self.prompt(sid, mid, body, source="post")
                return self.reply(handler, 204)
        return self.reply(handler, 404, {"name": "NotFoundError", "data": {
            "message": f"no route {method} {path}"}})

    def serve_events(self, handler):
        import queue

        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.end_headers()
        try:
            handler.wfile.write(("data: " + json.dumps(
                {"id": mint("evt"), "type": "server.connected", "properties": {}})
                + "\n\n").encode())
            handler.wfile.flush()
            while True:
                item = q.get()
                if item is None:
                    break
                handler.wfile.write(f"data: {item}\n\n".encode())
                handler.wfile.flush()
        except OSError:
            pass
        finally:
            with self.lock:
                if q in self.clients:
                    self.clients.remove(q)
            handler.close_connection = True

    # ------------------------------------------------------------- the model side
    def providers(self) -> dict:
        found: dict = {}
        for ids, price in ((self.free, 0), (self.paid, 1)):
            for full in ids:
                provider, model = full.split("/", 1)
                p = found.setdefault(provider, {"id": provider, "models": {}})
                p["models"][model] = {"id": model, "cost": {
                    "input": price, "output": price, "cache": {"read": 0, "write": 0}}}
        return {"providers": list(found.values()), "default": {}}

    def create_session(self, title=None, parent=None) -> dict:
        """`session.create`: published as `session.created` with the session's info, a
        child's carrying `parentID` (opencode/src/session/session.ts:535)."""
        sid = mint("ses")
        with self.lock:
            self.sessions[sid] = {"title": title or f"New session - {time.time()}",
                                  "messages": [], "status": "idle", "model": None,
                                  "running": None}
            self._save()
        info = {"id": sid, **({"parentID": parent} if parent else {})}
        self.emit("session.created", {"sessionID": sid, "info": info})
        if not title:
            self.request_log(sid, small=True, agent="title")
        return {"id": sid, "title": self.sessions[sid]["title"]}

    def model_of(self, sid) -> str:
        with self.lock:
            return (self.sessions[sid].get("model") or self.default_model
                    or (self.free[0] if self.free else "opencode/big-pickle"))

    def request_log(self, sid, small=False, agent="build", model=None):
        model = model or self.model_of(sid)
        provider, model_id = model.split("/", 1)
        self.on_log(stream_line(provider, model_id, sid, small=small, agent=agent))

    def _message(self, sid, info, text=""):
        with self.lock:
            msgs = self.sessions[sid]["messages"]
            for m in msgs:
                if m["info"]["id"] == info["id"]:
                    m["info"] = dict(info)
                    break
            else:
                msgs.append({"info": dict(info),
                             "parts": [{"type": "text", "text": text}] if text else []})
            self._save()
        self.emit("message.updated", {"sessionID": sid, "info": dict(info)})

    def prompt(self, sid, mid, body=None, *, source="post", text=None):
        """Admit a prompt: its user message, busy, and an assistant message parented to
        it. A running turn is steered: it ends at `finish=tool-calls`."""
        with self.lock:
            session = self.sessions[sid]
            if any(m["info"]["id"] == mid for m in session["messages"]):
                info = next(m["info"] for m in session["messages"] if m["info"]["id"] == mid)
                duplicate = True
            else:
                duplicate = False
                running = session["running"]
        if duplicate:
            self.emit("message.updated", {"sessionID": sid, "info": dict(info)})
            self.emit("session.status", {"sessionID": sid, "status": {"type": "busy"}})
            self.emit("session.status", {"sessionID": sid, "status": {"type": "idle"}})
            self.emit("session.idle", {"sessionID": sid})
            return
        text = text if text is not None else " ".join(
            p.get("text", "") for p in (body or {}).get("parts") or [])
        now = int(time.time() * 1000)
        self._message(sid, {"id": mid, "role": "user", "sessionID": sid,
                            "time": {"created": now}}, text)
        if running is not None:
            # steer: the running turn ends at its next step, its reply never written
            done = dict(running, finish="tool-calls",
                        time={"created": running["time"]["created"], "completed": now})
            self._message(sid, done)
        with self.lock:
            session["status"] = "busy"
        self.emit("session.status", {"sessionID": sid, "status": {"type": "busy"}})
        model = self.model_of(sid)
        provider, model_id = model.split("/", 1)
        assistant = {"id": mint("msg"), "role": "assistant", "sessionID": sid,
                     "parentID": mid, "providerID": provider, "modelID": model_id,
                     "time": {"created": int(time.time() * 1000)}}
        with self.lock:
            session["running"] = assistant
        self._message(sid, assistant)
        self.request_log(sid)
        if self.auto:
            threading.Timer(0.05, self.finish, args=(sid,)).start()

    def finish(self, sid, finish="stop", error=None, idle=True):
        """End the running turn: its assistant message completes (sent twice, as the
        real server does), then idle."""
        with self.lock:
            session = self.sessions[sid]
            running, session["running"] = session["running"], None
        if running is not None:
            done = dict(running, finish=finish,
                        time={"created": running["time"]["created"],
                              "completed": int(time.time() * 1000)})
            if error:
                done["error"] = error
            self._message(sid, done)
            self.emit("message.updated", {"sessionID": sid, "info": done})
        if idle:
            self.go_idle(sid)

    def go_idle(self, sid):
        with self.lock:
            self.sessions[sid]["status"] = "idle"
        self.emit("session.status", {"sessionID": sid, "status": {"type": "idle"}})
        self.emit("session.idle", {"sessionID": sid})

    def abort(self, sid):
        """The window's Esc: the running turn's assistant message ends with
        `MessageAbortedError`, then idle."""
        self.finish(sid, finish=None, error={"name": "MessageAbortedError"})

    def compacted(self, sid):
        """A compaction ended, manual or automatic (compaction.ts:554). Status untouched:
        an automatic one runs inside the busy loop."""
        self.emit("session.compacted", {"sessionID": sid})

    def error(self, sid, message="boom"):
        self.emit("session.error", {"sessionID": sid, "error": {
            "name": "UnknownError", "data": {"message": message}}})
        self.finish(sid, finish=None, error={"name": "UnknownError"})

    # ------------------------------------------------------------- control
    def control(self, op: str, body: dict) -> dict:
        sid = body.get("session")
        if op == "finish":
            self.finish(sid, finish=body.get("finish", "stop"))
        elif op == "sessions":
            with self.lock:
                return {"sessions": {k: {"status": v["status"],
                                         "messages": [m["info"] for m in v["messages"]]}
                                     for k, v in self.sessions.items()}}
        elif op == "close-streams":
            self.close_streams()
        elif op == "stall":
            with self.lock:
                self.stalled = bool(body.get("on", True))
        elif op == "pick":
            with self.lock:
                self.sessions[sid]["model"] = body["model"]
        elif op == "abort":
            self.abort(sid)
        elif op == "compacted":
            self.compacted(sid)
        elif op == "new-session":
            # The window's /new: the TUI creates the session at its first prompt.
            created = self.create_session(parent=body.get("parent"))
            if body.get("text") is not None:
                self.prompt(created["id"], mint("msg"), text=body["text"], source="window")
            return {"ok": True, "session": created["id"]}
        elif op == "type":
            self.prompt(sid, mint("msg"), text=body.get("text", "typed"), source="window")
        return {"ok": True}


# --------------------------------------------------------------------------- as a binary

def record_dir() -> Path | None:
    value = os.environ.get("FAKE_OPENCODE_DIR")
    return Path(value) if value else None


def event(kind: str, **fields) -> None:
    directory = record_dir()
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "events.jsonl").open("a") as f:
        f.write(json.dumps({"event": kind, "pid": os.getpid(), "t": time.time(),
                            **fields}) + "\n")


def serve(argv) -> int:
    config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
    value = os.environ.get("FAKE_OPENCODE_FREE")
    directory = record_dir()
    if directory is not None and (directory / "refreshed").exists():
        value = os.environ.get("FAKE_OPENCODE_FREE_REFRESHED", value)
    free = FREE_DEFAULT if value is None else [m for m in value.split(",") if m]

    def out(line):
        sys.stdout.write(line)
        sys.stdout.flush()

    fake = FakeOpenCode(password=os.environ.get("OPENCODE_SERVER_PASSWORD"), free=free,
                        default_model=config.get("model"), on_log=out,
                        auto=os.environ.get("FAKE_OPENCODE_AUTO", "1") != "0",
                        store=(directory / "sessions.json") if directory else None)
    fake.start()
    event("serve", argv=argv, url=fake.url, config=config,
          stamp={k: os.environ.get(k) for k in ("MOXI_COMMS_NAME", "MOXI_COMMS_SLOT", "MOXI_COMMS_SCOPE")},
          password=bool(os.environ.get("OPENCODE_SERVER_PASSWORD")))
    out(f"timestamp=0 level=INFO message=\"opencode server listening on {fake.url}\"\n")
    out(f"opencode server listening on {fake.url}\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


def window(url: str, session_id: str) -> int:
    import urllib.error
    import urllib.request

    modes = dict(part.partition("=")[::2] for part in
                 os.environ.get("FAKE_OPENCODE_WINDOW", "").split(",") if part)
    password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    req = urllib.request.Request(f"{url}/session/{session_id}",
                                 headers={"authorization": f"Basic {token}"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except urllib.error.HTTPError as e:
        print(f"Error: opencode server GET {url}/session/{session_id} → {e.code}",
              file=sys.stderr, flush=True)
        event("window-exit", code=1, why=str(e.code))
        return 1
    try:
        foreground = os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        foreground = None
    event("window", argv=sys.argv, url=url, session=session_id, foreground=foreground,
          modes=modes)
    if "alt" in modes:
        os.write(1, b"\x1b[?1049h\x1b[?25l")
    started = time.monotonic()
    exit_after = float(modes["exit-after"]) if "exit-after" in modes else None
    while True:
        time.sleep(0.05)
        quit_file = record_dir() / "quit" if record_dir() is not None else None
        if (("quit-file" in modes and quit_file is not None and quit_file.exists())
                or (exit_after is not None and time.monotonic() - started >= exit_after)):
            if quit_file is not None and quit_file.exists():
                quit_file.unlink()
            if "alt" in modes:
                os.write(1, b"\x1b[?1049l\x1b[?25h")
            event("window-exit", code=0, why="quit")
            return 0


def refresh(argv) -> int:
    """`opencode models --refresh`: what 1.18.34 does, the ways it fails included. Its
    "Models cache refreshed" goes to stderr whatever happened; a failed fetch is told only
    by its log line."""
    mode = os.environ.get("FAKE_OPENCODE_REFRESH", "")
    event("models-refresh", argv=argv, cwd=os.getcwd(), mode=mode,
          password=bool(os.environ.get("OPENCODE_SERVER_PASSWORD")))
    if mode == "slow":
        time.sleep(30)
    if mode == "exit":
        print("fake opencode: refresh crashed", file=sys.stderr)
        return 1
    if mode == "fetch-fail":
        print('timestamp=0 level=ERROR run=fake0001 message="Failed to fetch models.dev" '
              'cause="Cause([Fail(HttpClientError: Transport error)])"', file=sys.stderr)
    else:
        directory = record_dir()
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "refreshed").touch()
    print("Models cache refreshed", file=sys.stderr)
    return 0


def main(argv) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if argv[:1] == ["serve"]:
        return serve(argv)
    if argv[:1] == ["attach"]:
        return window(argv[1], argv[argv.index("-s") + 1])
    if argv[:2] == ["models", "--refresh"]:
        return refresh(argv)
    if argv[:2] in (["db", "path"], ["debug", "config"]):
        # Recorded with what says it ran under the server's environment and where.
        event(" ".join(argv[:2]), cwd=os.getcwd(),
              password=bool(os.environ.get("OPENCODE_SERVER_PASSWORD")),
              config=os.environ.get("OPENCODE_CONFIG_CONTENT"))
        if argv[0] == "db":
            path = os.environ.get("FAKE_OPENCODE_DB")
            if not path:
                print("fake opencode: no database", file=sys.stderr)
                return 1
            print(path)
            return 0
        config = os.environ.get("FAKE_OPENCODE_CONFIG", '{"agent": {}}')
        if config == "fail":
            print("fake opencode: config error", file=sys.stderr)
            return 1
        print(config)
        return 0
    print(f"fake opencode: unknown arguments {argv}", file=sys.stderr)
    return 2


def install(directory: Path, name: str = "opencode") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).resolve()} \"$@\"\n")
    script.chmod(0o755)
    return script


def control(url: str, op: str, **body) -> dict:
    import urllib.request

    req = urllib.request.Request(f"{url}/__fake/{op}", data=json.dumps(body).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}")


def events(directory: Path) -> list:
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
