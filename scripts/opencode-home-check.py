#!/usr/bin/env python3
"""What an OpenCode session comms launches would load from this HOME, read with no model.

    uv run python scripts/opencode-home-check.py [--opencode PATH]

Two reads, each from a throwaway git root, under the HOME you run it in:

  * the **session** environment, as `moxi-comms-opencode` starts a server: none of
    OpenCode's off switches set. The session should load your `~/.claude/CLAUDE.md`
    and your skills.
  * the **test** environment, as comms' tests and smokes run OpenCode: the three off
    switches set (`OPENCODE_DISABLE_CLAUDE_CODE`, `OPENCODE_DISABLE_EXTERNAL_SKILLS`,
    `OPENCODE_DISABLE_PROJECT_CONFIG`). It should load neither.

Each read lists the skills (`opencode debug skill`) and reads what the server would send
a model for one prompt, at a local endpoint that records the request and refuses it, so
no model runs and nothing leaves the machine. The report says only whether your
`CLAUDE.md` and each skill name were present, never their text.

**OpenCode writes its state under HOME even for these reads** (its database, a log, a
lock). Since you may run OpenCode yourself, the check is careful with that state:

  * it runs only if no other `opencode` process is running, and checks again at the end;
  * it snapshots every file under OpenCode's directories in HOME and `/tmp/opencode`
    (size, modification time, inode) before and after;
  * it removes only files that are new in the after snapshot, were written during its
    own run, and are named as OpenCode's own state (`opencode.db` and its `-shm`/`-wal`,
    a log file under `log/`, anything under `locks/`, `~/.opencode/.gitignore`);
  * it lists everything else that is new or changed, and leaves it.

If another `opencode` was running at the end, it removes nothing and lists everything.
Exit 0 when both reads came out as expected, 1 when not, 2 when it refused to run.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

SWITCHES = ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_EXTERNAL_SKILLS",
            "OPENCODE_DISABLE_PROJECT_CONFIG")

#: OpenCode's own state, by path relative to HOME (or to /tmp/opencode, written
#: "/tmp/opencode/..."): the only new files the check may remove.
OWN_STATE = (".local/share/opencode/opencode.db", ".local/share/opencode/opencode.db-shm",
             ".local/share/opencode/opencode.db-wal", ".local/share/opencode/log/*.log",
             ".local/state/opencode/locks/*", ".local/state/opencode/locks",
             ".opencode/.gitignore")


def opencode_dirs(home: Path) -> list:
    return [home / ".local/share/opencode", home / ".local/state/opencode",
            home / ".cache/opencode", home / ".config/opencode", home / ".opencode",
            Path("/tmp/opencode")]


def snapshot(dirs) -> dict:
    """path -> (size, mtime_ns, inode, is_dir) for every file and directory under each."""
    found = {}
    for top in dirs:
        top = Path(top)
        if not top.exists():
            continue
        for root, subdirs, files in os.walk(top):
            for name in subdirs + files:
                p = Path(root) / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                found[str(p)] = (st.st_size, st.st_mtime_ns, st.st_ino, p.is_dir())
    return found


def own_state(path: str, home: Path) -> bool:
    try:
        rel = str(Path(path).relative_to(home))
    except ValueError:
        return False
    return any(fnmatch.fnmatch(rel, pattern) for pattern in OWN_STATE)


def plan_cleanup(before: dict, after: dict, started_ns: int, home: Path) -> tuple:
    """(to remove, new and left, changed and left). A path is removed only when it is
    new, was written during this run, and is OpenCode's own state."""
    remove, new_left, changed = [], [], []
    for path, stat in sorted(after.items()):
        if path not in before:
            if stat[1] >= started_ns and own_state(path, home):
                remove.append(path)
            else:
                new_left.append(path)
        elif before[path][:3] != stat[:3] and not stat[3]:
            changed.append(path)
    return remove, new_left, changed


def remove_paths(paths, after: dict) -> list:
    """Files first, then the directories among them that are empty by then."""
    done = []
    for path in [p for p in paths if not after[p][3]]:
        try:
            os.unlink(path)
            done.append(path)
        except OSError:
            pass
    for path in sorted([p for p in paths if after[p][3]], key=len, reverse=True):
        try:
            os.rmdir(path)
            done.append(path)
        except OSError:
            pass
    return done


def other_opencode(exclude=()) -> list:
    """(pid, command) of every running process whose program is `opencode`."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in exclude:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            exe = os.path.basename(os.readlink(entry / "exe"))
        except OSError:
            continue
        first = os.path.basename(argv[0].decode(errors="replace")) if argv else ""
        if "opencode" in (first, exe):
            found.append((int(entry.name), " ".join(a.decode(errors="replace")
                                                     for a in argv if a)[:120]))
    return found


class Capture:
    """A local OpenAI-compatible endpoint that records each request and refuses it."""

    def __init__(self):
        self.bodies = []
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    capture.bodies.append(json.loads(raw))
                except ValueError:
                    capture.bodies.append({})
                data = b'{"error":{"message":"refused by the check","type":"invalid"}}'
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def text(self) -> str:
        return json.dumps(self.bodies)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def read_once(binary: str, env: dict, work: Path, capture: Capture) -> dict:
    """The skills the server lists (`GET /skill`, else `debug skill`), and the text one
    prompt would send."""
    from moxi_comms.adapters.opencode_server import Http, Server

    skills = subprocess.run([binary, "debug", "skill"], cwd=work, env=env, timeout=120,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        # The listing is one JSON array; OpenCode may print other lines around it (a
        # first run installs its plugin dependencies), so read the array where it starts.
        start = skills.stdout.index("[")
        listed, _ = json.JSONDecoder().raw_decode(skills.stdout[start:])
        unreadable = None
    except ValueError as e:
        listed = []
        unreadable = f"exit {skills.returncode}, {len(skills.stdout)} bytes: {e}"
    capture.bodies.clear()
    config = {"provider": {"check": {
        "npm": "@ai-sdk/openai-compatible", "name": "check",
        "options": {"baseURL": f"http://127.0.0.1:{capture.port}/v1"},
        "models": {"check": {"name": "check"}}}},
        "enabled_providers": ["check"], "model": "check/check", "small_model": "check/check",
        "snapshot": False}         # no git snapshot of the throwaway work dir under HOME
    env = dict(env, OPENCODE_CONFIG_CONTENT=json.dumps(config),
               OPENCODE_SERVER_PASSWORD="opencode-home-check")
    server = Server(binary, env=env, cwd=str(work))
    server.start()
    try:
        url = server.wait_listening(60)
        if not url:
            raise SystemExit("the opencode server never said where it listens")
        http = Http(url, "opencode-home-check")
        status, served = http.json("GET", "/skill", timeout=60)
        if status == 200 and isinstance(served, list):
            listed, unreadable = served, None       # the server's own listing
        _, session = http.json("POST", "/session", {"title": "opencode-home-check"})
        http.request("POST", f"/session/{session['id']}/prompt_async",
                     {"messageID": "msg_" + "0" * 12 + "homecheck00000",
                      "parts": [{"type": "text", "text": "home check"}]})
        deadline = time.monotonic() + 60
        while not capture.bodies and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(1.0)
        http.request("POST", f"/session/{session['id']}/abort", {})
    finally:
        server.stop()
    return {"skills": listed, "sent": capture.text(), "unreadable": unreadable}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--opencode", default=shutil.which("opencode") or
                    str(Path("~/.opencode/bin/opencode").expanduser()))
    ap.add_argument("--home", default=os.environ.get("HOME"),
                    help="the HOME to read (default: this one)")
    args = ap.parse_args(argv)
    home = Path(args.home)
    running = other_opencode(exclude={os.getpid()})
    if running:
        print("refused: another opencode process is running, and its state could not "
              "be told apart from this check's:")
        for pid, cmd in running:
            print(f"  {pid}  {cmd}")
        return 2
    claude_md = home / ".claude" / "CLAUDE.md"
    marker = next((line.strip() for line in claude_md.read_text().splitlines()
                   if len(line.strip()) > 20), None) if claude_md.exists() else None
    skill_names = sorted(p.parent.name for p in
                         list((home / ".claude/skills").glob("*/SKILL.md"))
                         + list((home / ".agents/skills").glob("*/SKILL.md")))
    dirs = opencode_dirs(home)
    before = snapshot(dirs)
    started_ns = time.time_ns()
    capture = Capture()
    results = {}
    base = {k: v for k, v in os.environ.items() if k not in SWITCHES and
            not k.startswith(("MOXI_COMMS_", "OPENCODE_"))}
    base["HOME"] = str(home)
    try:
        with tempfile.TemporaryDirectory(prefix="opencode-home-check-") as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            subprocess.run(["git", "init", "-q", str(work)], check=True,
                           stdin=subprocess.DEVNULL)
            for label, extra in (("session", {}), ("test", {k: "1" for k in SWITCHES})):
                results[label] = read_once(args.opencode, dict(base, **extra), work,
                                           capture)
    finally:
        capture.close()
    still = other_opencode(exclude={os.getpid()})
    after = snapshot(dirs)
    remove, new_left, changed = plan_cleanup(before, after, started_ns, home)
    if still:
        new_left, remove = sorted(new_left + remove), []
    removed = remove_paths(remove, after)

    ok = True
    print(f"HOME {home}")
    session_skills = [s for s in results["session"]["skills"]
                      if s.get("location") != "<built-in>"]
    probes = [json.dumps((s.get("description") or "")[:60])[1:-1]
              for s in session_skills if s.get("description")]
    for label in ("session", "test"):
        r = results[label]
        external = [s for s in r["skills"] if s.get("location") != "<built-in>"]
        has_claude = bool(marker) and json.dumps(marker)[1:-1] in r["sent"]
        sent_skills = sum(1 for probe in probes if probe in r["sent"])
        if r.get("unreadable"):
            print(f"{label}: debug skill's listing could not be read ({r['unreadable']})")
        print(f"{label}: OpenCode lists {len(external)} skill(s) beyond the built-in "
              f"({len(skill_names)} under ~/.claude/skills and ~/.agents/skills); "
              f"CLAUDE.md in what would be sent: {'yes' if has_claude else 'no'}; "
              f"skill descriptions in it: {sent_skills} of {len(probes)}")
        if label == "session":
            ok = ok and (has_claude or not marker) and (
                not skill_names or (external and sent_skills))
        else:
            ok = ok and not has_claude and not external and not sent_skills
    print(f"removed {len(removed)} file(s) OpenCode wrote during the check:")
    for path in removed:
        print(f"  {path}")
    if still:
        print("another opencode process started during the check, so nothing was removed")
    for title, paths in (("new, left in place", new_left),
                         ("existed before and changed, left in place", changed)):
        print(f"{title}: {len(paths)}")
        for path in paths:
            print(f"  {path}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
