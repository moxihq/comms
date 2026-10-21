"""A stub `codex` executable: the fake app-server, and a stub window, behind one binary.

The launcher spawns `codex app-server --listen unix://SOCK` and, under `--window`,
`codex -c check_for_update_on_startup=false resume <thread> --remote unix://SOCK`. This
stub answers both, so the real launcher can be run end to end with no model and no
network. `--version` prints a version line.

Everything it does is driven by the environment it inherits from the launcher:

    FAKE_CODEX_DIR       a directory for its records:
                           events.jsonl   one line per process event (start, stop, exit)
                           calls.jsonl    every request the app-server received
                           threads.json   the threads, so a respawned app-server finds them
                           control.sock   a JSON-lines control socket of the app-server
    FAKE_CODEX_TAG       a label written into every event (which copy of the stub ran)
    FAKE_CODEX_SELF      set by the installed wrapper: the path it was run as
    FAKE_CODEX_PRIMING   how the first turn of a fresh thread ends: complete (default),
                         failed, interrupted, hang (starts, never completes) or
                         never-start (no turn/started at all)
    FAKE_CODEX_WINDOW    comma-separated window behaviours:
                           alt           enter the alternate screen and hide the cursor
                           raw           put the terminal in raw mode
                           exit-on-drop  exit 0 when the app-server connection drops
                           exit-after=S  exit 0 after S seconds, as an operator's quit
                           stop-self=S   after S seconds stop its own process group, as
                                         Codex does on Ctrl-Z, then exit 0 once continued
                           term-exit-0   on SIGTERM exit 0 at once, still on the alternate
                                         screen (a window killed from outside)
                           quit-file     exit 0, as an operator's quit, once a file named
                                         `quit` appears in FAKE_CODEX_DIR (it is removed)
                         Without exit-on-drop it stays up when the connection drops.

Control socket ops (one JSON object per line, one answer per line):
    {"op": "complete", "status": "completed"}   end the live turn of every thread
    {"op": "threads"}                           the threads and their turn statuses
    {"op": "pid"}                               the app-server's pid
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

VERSION = "codex-cli 0.160.0 (stub)"


def record_dir() -> Path | None:
    value = os.environ.get("FAKE_CODEX_DIR")
    return Path(value) if value else None


def event(kind: str, **fields) -> None:
    directory = record_dir()
    if directory is None:
        return
    line = {"event": kind, "tag": os.environ.get("FAKE_CODEX_TAG", ""),
            "self": os.environ.get("FAKE_CODEX_SELF", ""),
            "pid": os.getpid(), "pgid": os.getpgrp(), "t": time.time(), **fields}
    with open(directory / "events.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")


# --------------------------------------------------------------------------- app-server

def shell_hook_runner(argv):
    import subprocess
    import tomllib
    commands = {}
    for i, flag in enumerate(argv[:-1]):
        value = argv[i + 1]
        if flag == "-c" and value.startswith("hooks.") and not value.startswith("hooks.state="):
            key, _, rest = value.partition("=")
            commands[key.split(".", 1)[1]] = \
                tomllib.loads(f"v = {rest}")["v"][0]["hooks"][0]["command"]

    def run(hook_event, payload):
        command = commands.get(hook_event)
        if command:
            subprocess.run(command, shell=True, input=json.dumps(payload), text=True,
                           timeout=10, capture_output=True)
    return run


def app_server(socket_path: str) -> int:
    from fake_app_server import FakeAppServer

    directory = record_dir()
    priming = os.environ.get("FAKE_CODEX_PRIMING", "complete")

    class StubServer(FakeAppServer):
        def _dispatch(self, conn, msg):
            if directory is not None and msg.get("method"):
                with open(directory / "calls.jsonl", "a") as f:
                    f.write(json.dumps({"pid": os.getpid(), "method": msg.get("method"),
                                        "params": msg.get("params") or {}}) + "\n")
            return super()._dispatch(conn, msg)

        def _turn_start(self, params):
            thread = self._thread_of(params)
            first = not thread["turns"]
            if first and priming == "never-start":
                return {"turn": {"id": "phantom-never-started", "items": [],
                                 "status": "inProgress", "error": None}}
            result = super()._turn_start(params)
            if first and priming in ("complete", "completed", "failed", "interrupted"):
                status = "completed" if priming == "complete" else priming
                threading.Timer(0.2, self.complete_turn,
                                args=(thread["id"], status)).start()
            return result

    store = str(directory / "threads.json") if directory is not None else None
    server = StubServer(socket_path, store=store)
    # Codex's hooks: listed and trusted as 0.153 would list them, and each run as Codex
    # runs it, through the shell with its JSON on stdin.
    server.hook_states = FakeAppServer.hook_states_from_argv(sys.argv)
    server.hook_runner = shell_hook_runner(sys.argv)
    server.load_store()
    server.start()
    event("app-server", argv=sys.argv, socket=socket_path)
    if directory is not None:
        threading.Thread(target=control, args=(server, directory / "control.sock"),
                         daemon=True).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(0.1):
        pass
    server.stop()
    with_suppress(os.unlink, socket_path)
    event("app-server-exit")
    return 0


def control(server, path: Path) -> None:
    with_suppress(os.unlink, str(path))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=serve_control, args=(server, conn), daemon=True).start()


def serve_control(server, conn) -> None:
    with conn, conn.makefile("rw") as f:
        for line in f:
            op = json.loads(line)
            answer: dict = {"ok": True}
            if op.get("op") == "complete":
                done = []
                for thread_id in list(server.threads):
                    if server.active_turn_id(thread_id):
                        done.append(server.complete_turn(thread_id,
                                                         op.get("status", "completed")))
                answer["turns"] = done
            elif op.get("op") == "threads":
                answer["threads"] = {tid: [t["status"] for t in thread["turns"]]
                                     for tid, thread in server.threads.items()}
            elif op.get("op") == "pid":
                answer["pid"] = os.getpid()
            f.write(json.dumps(answer) + "\n")
            f.flush()


# --------------------------------------------------------------------------- window

def window(thread_id: str, remote: str) -> int:
    modes = dict(part.partition("=")[::2] for part in
                 os.environ.get("FAKE_CODEX_WINDOW", "").split(",") if part)
    event("window", argv=sys.argv, thread=thread_id,
          foreground=foreground(), modes=modes)
    dropped = threading.Event()
    client = AppServerClient(remote.removeprefix("unix://"), on_close=dropped.set,
                             timeout=5.0)
    try:
        client.connect()
        client.initialize()
        client.call("thread/resume", {"threadId": thread_id})
    except Exception as e:                                   # noqa: BLE001
        print(f"Failed to resume session: thread/resume failed during TUI bootstrap: "
              f"{e}", file=sys.stderr, flush=True)
        event("window-exit", code=1, why=str(e))
        return 1
    if "term-exit-0" in modes:
        def killed(*_):
            event("window-exit", code=0, why="SIGTERM")
            os._exit(0)
        signal.signal(signal.SIGTERM, killed)
    if "raw" in modes and os.isatty(0):
        import tty
        tty.setraw(0)
    if "alt" in modes:
        os.write(1, b"\x1b[?1049h\x1b[?25l")
    started = time.monotonic()
    stop_after = float(modes["stop-self"]) if "stop-self" in modes else None
    exit_after = float(modes["exit-after"]) if "exit-after" in modes else None
    while True:
        time.sleep(0.05)
        elapsed = time.monotonic() - started
        if stop_after is not None and elapsed >= stop_after:
            stop_after = None
            event("window-stopping")
            os.killpg(os.getpgrp(), signal.SIGTSTP)
            event("window-continued", foreground=foreground())
            time.sleep(0.2)
            leave(modes)
            event("window-exit", code=0, why="continued")
            return 0
        if dropped.is_set() and "exit-on-drop" in modes:
            event("window-exit", code=0, why="connection dropped")
            return 0
        quit_file = record_dir() / "quit" if record_dir() is not None else None
        if "quit-file" in modes and quit_file is not None and quit_file.exists():
            with_suppress(os.unlink, str(quit_file))
            leave(modes)
            event("window-exit", code=0, why="quit")
            client.close()
            return 0
        if exit_after is not None and elapsed >= exit_after:
            leave(modes)
            event("window-exit", code=0, why="quit")
            client.close()
            return 0


class AppServerClient:
    """The window's link: a WebSocket over the app-server's Unix socket, enough to
    initialize and resume one thread, and to notice the connection drop."""

    def __init__(self, path, *, on_close, timeout):
        self.path, self.on_close, self.timeout = path, on_close, timeout
        self.ws = None
        self.n = 0

    def connect(self):
        from websockets.sync.client import unix_connect

        self.ws = unix_connect(self.path, uri="ws://localhost/", compression=None,
                               open_timeout=self.timeout)
        threading.Thread(target=self._drain, daemon=True).start()
        return self

    def _drain(self):
        try:
            for _ in self.ws:
                pass
        except Exception:                                    # noqa: BLE001
            pass
        self.on_close()

    def call(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                 "params": params or {}}))

    def initialize(self):
        self.call("initialize", {"clientInfo": {"name": "fake-window", "version": "0"}})

    def close(self):
        if self.ws is not None:
            self.ws.close()


def leave(modes) -> None:
    """A quit the operator types leaves the alternate screen itself."""
    if "alt" in modes:
        os.write(1, b"\x1b[?1049l\x1b[?25h")


def foreground() -> bool | None:
    try:
        return os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        return None


def with_suppress(fn, *args) -> None:
    try:
        fn(*args)
    except OSError:
        pass


def main(argv) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if argv[:1] == ["app-server"]:
        return app_server(argv[argv.index("--listen") + 1].removeprefix("unix://"))
    if "resume" in argv:
        return window(argv[argv.index("resume") + 1], argv[argv.index("--remote") + 1])
    print(f"fake codex: unknown arguments {argv}", file=sys.stderr)
    return 2


def install(directory: Path, *, tag: str = "", name: str = "codex") -> Path:
    """Write an executable `codex` into `directory` that runs this stub."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\nFAKE_CODEX_TAG=${{FAKE_CODEX_TAG:-{tag}}} "
                      f"FAKE_CODEX_SELF=\"$0\" "
                      f"exec {sys.executable} {Path(__file__).resolve()} \"$@\"\n")
    script.chmod(0o755)
    return script


def control_call(directory: Path, op: dict, timeout: float = 5.0) -> dict:
    """One control op against the stub app-server whose records are in `directory`."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(directory / "control.sock"))
        with s.makefile("rw") as f:
            f.write(json.dumps(op) + "\n")
            f.flush()
            return json.loads(f.readline())


def events(directory: Path) -> list:
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def calls(directory: Path, method: str | None = None) -> list:
    path = directory / "calls.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if method is None or r["method"] == method]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
