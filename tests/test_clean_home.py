"""The clean-home install test.

Builds comms' wheel and the core's (`moxi`, from its checkout beside this one: Moxi's
members declare no sources), installs both into a fresh venv inside a temp HOME, and
runs everything from that install with an environment built from nothing: HOME and every
XDG root in the temp home, TMPDIR there too, no `MOXI_COMMS_*`, so nothing in the
developer's shell can pick the backend or a root.

Part one is the documented install and nothing else. `moxi comms doctor` runs first and must
say the `local` backend by default, with no slots yet.
`moxi comms service install` writes the user unit; a temp home has no user systemd manager,
so a stand-in `systemctl` on PATH records the calls, and the test then runs the unit's
own `ExecStart` in the foreground with the unit's own `Environment=` lines, as the
manager would. The unit must set no backend or root. The daemon provisions
itself on first start, and the test asserts the 6 slots it made, through `moxi comms doctor`
and the directory. Part one sets no backend or root and runs no provisioning.

Part two adds the fixtures the exchange needs, each named: an OpenCode roster for two
identities on slots part one asserted, and a stub `opencode` on PATH (`fake_opencode.py`,
with a turn that runs `moxi comms send`). Neither sets the backend or the slots. Then:

  * two identities, each launched with the installed `moxi-comms-opencode` on its own pty,
    exchange a message. Only a live session holding a name sends under it, so the test
    itself sends from the operator's lane, its own terminal outside every session: the
    operator asks alice to ping bob, alice's turn (run by her stub server, which her
    launcher spawned inside her session) sends with `moxi comms send`, bob's turn answers the
    same way, and alice's server gets the answer. A launch stamp alone is refused, and
    the test checks that too;
  * the daemon exits cleanly (status 0, socket gone) on SIGTERM;
  * no file is written outside the temp home. Every Python process the install runs
    carries an audit hook (a `sitecustomize` in the venv) that records each write-side
    event -- open for writing, mkdir, rename, remove, chmod, a sqlite database, a Unix
    socket bind, a temp file -- and the test fails on any path outside the home.

It needs `uv` and builds a wheel, so it runs only with `--clean-home`.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import sqlite3
import signal
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
#: The core's checkout, beside this one in ~/moxi and in a build layout alike.
CORE = REPO.parent / "moxi"
UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="the clean-home test needs uv")

AUDIT_HOOK = r'''
import os, sys

_path = os.environ.get("CLEAN_HOME_AUDIT")
if _path:
    _fd = os.open(_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    _busy = []
    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def _note(event, target):
        if isinstance(target, bytes):
            target = os.fsdecode(target)
        if not isinstance(target, (str, os.PathLike)):
            return
        target = os.fspath(target)
        if not target or target == ":memory:":
            return
        try:
            target = os.path.abspath(target)
        except Exception:
            return
        line = "%s\t%d\t%s\t%s\n" % (event, os.getpid(), os.path.basename(sys.argv[0]),
                                    target)
        os.write(_fd, line.encode("utf-8", "surrogateescape"))

    def _hook(event, args):
        if _busy:
            return
        _busy.append(1)
        try:
            if event == "open":
                path, mode, flags = (list(args) + [None, None, None])[:3]
                writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                    (isinstance(flags, int) and flags & _WRITE_FLAGS)
                if writes:
                    _note(event, path)
            elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod",
                           "os.truncate", "os.utime", "os.chown", "sqlite3.connect",
                           "tempfile.mkstemp", "tempfile.mkdtemp", "os.mkfifo",
                           "os.mknod"):
                _note(event, args[0])
            elif event in ("os.rename", "os.link", "os.symlink", "shutil.copyfile",
                           "shutil.move"):
                _note(event, args[0])
                _note(event, args[1])
            elif event == "socket.bind":
                address = args[1]
                if isinstance(address, (str, bytes)) and address:
                    _note(event, address)
        finally:
            _busy.pop()

    sys.addaudithook(_hook)
'''

#: The stub `opencode`'s turn. The stub server (`fake_opencode.py`, run by the test's own
#: interpreter, so it is no part of the install) admits each posted prompt; this turn
#: logs the prompt's text, runs `moxi comms send` when the text asks for it, and only then
#: ends, as an agent's tool call runs inside its turn. The server is the launcher's child
#: with the session's claim socket in its environment, so the `moxi comms send` it runs is
#: inside that session and sends as the session's identity.
STUB_TURN = r'''
import os, subprocess, sys, threading
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv.pop(1))
import fake_opencode

admit = fake_opencode.FakeOpenCode.prompt


def prompt(self, sid, mid, body=None, *, source="post", text=None):
    said = text if text is not None else " ".join(
        p.get("text", "") for p in (body or {}).get("parts") or [])
    admit(self, sid, mid, body, source=source, text=text)
    running = self.sessions[sid]["running"]
    if running is not None and running.get("parentID") == mid:
        threading.Thread(target=turn, args=(self, sid, said)).start()


def turn(fake, sid, said):
    name = os.environ.get("MOXI_COMMS_NAME", "?")
    home = os.environ["HOME"]
    with open(os.path.join(home, "opencode-%s.log" % name), "a") as f:
        f.write(said.replace("\n", " ") + "\n")
    for who, asked, to, text in (("alice", "please ping bob", "bob", "ping from alice"),
                                 ("bob", "ping from alice", "alice", "pong from bob")):
        if name == who and asked in said:
            sent = subprocess.run(["moxi", "comms", "send", "--posture", "none", to, text],
                                  capture_output=True, text=True)
            with open(os.path.join(home, "opencode-%s.sent" % name), "a") as f:
                f.write("exit %d\n%s%s" % (sent.returncode, sent.stdout, sent.stderr))
    fake.finish(sid)


fake_opencode.FakeOpenCode.prompt = prompt
sys.exit(fake_opencode.main(sys.argv[1:]))
'''


#: What the stub's `opencode debug config` prints: a person's config that pins the
#: default agent (`build`) to a free model. The daemon reads the agent's next request
#: from it, and a free model is a clock with no cache to lose, so a send to the agent
#: goes through though the stub keeps no `opencode.db`.
DEBUG_CONFIG = json.dumps({"agent": {"build": {"model": "opencode/minimax-m3.1-flash-free"}}})


class OnPty:
    """One `moxi-comms-opencode` on its own pty, as a terminal runs it: the window takes the
    launcher's terminal, so it will not run without one."""

    def __init__(self, argv, env, cwd):
        master, slave = os.openpty()
        self.fd = master
        self.proc = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.out = bytearray()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.out.extend(chunk)

    def text(self) -> str:
        return bytes(self.out).decode(errors="replace")

    def send_signal(self, sig):
        self.proc.send_signal(sig)

    def wait(self, timeout=None):
        try:
            return self.proc.wait(timeout=timeout)
        finally:
            if self.proc.poll() is not None:
                self._reader.join(timeout=2)
                try:
                    os.close(self.fd)
                except OSError:
                    pass

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()


#: The stand-in for the user's systemd manager, which a temp home does not have. It
#: records each call and succeeds, so `moxi comms service install` runs its whole course;
#: the test then does what the manager would, from the unit file.
FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$HOME/systemctl.calls"
exit 0
"""


def run(argv, env, **kw):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, **kw)


def wait_for(predicate, timeout=20.0, what="it"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what() if callable(what) else what}")


def read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


@pytest.fixture
def installed(tmp_path_factory, request):
    if not request.config.getoption("--clean-home"):
        pytest.skip("builds the wheel and a venv; run with --clean-home")
    # A short base: the daemon's socket lives under the home, and a Unix socket path is
    # capped at 108 bytes.
    base = Path(subprocess.run(["mktemp", "-d", "/tmp/och-XXXXXX"], capture_output=True,
                               text=True, check=True).stdout.strip())
    home = base / "home"
    home.mkdir()
    dist = base / "dist"
    build = subprocess.run([UV, "build", "--wheel", "--out-dir", str(dist), str(REPO)],
                           capture_output=True, text=True, timeout=300)
    assert build.returncode == 0, build.stderr
    [wheel] = dist.glob("moxi_comms-*.whl")
    core = subprocess.run([UV, "build", "--wheel", "--out-dir", str(dist), str(CORE)],
                          capture_output=True, text=True, timeout=300)
    assert core.returncode == 0, core.stderr
    [core_wheel] = dist.glob("moxi-*.whl")
    venv = home / ".local" / "share" / "moxi-comms-venv"
    made = subprocess.run([UV, "venv", "--python", sys.executable, str(venv)],
                          capture_output=True, text=True, timeout=120)
    assert made.returncode == 0, made.stderr
    py = venv / "bin" / "python"
    got = subprocess.run([UV, "pip", "install", "--python", str(py), str(wheel),
                          str(core_wheel)],
                         capture_output=True, text=True, timeout=300)
    assert got.returncode == 0, got.stderr
    site = subprocess.run([str(py), "-c", "import sysconfig; "
                           "print(sysconfig.get_path('purelib'))"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (Path(site) / "sitecustomize.py").write_text(AUDIT_HOOK)

    fakebin = home / "fakebin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text(FAKE_SYSTEMCTL)
    (fakebin / "systemctl").chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{venv / 'bin'}:{fakebin}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TMPDIR": str(home / "tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "CLEAN_HOME_AUDIT": str(base / "audit.tsv"),
    }
    (home / "tmp").mkdir()
    (home / "run").mkdir(mode=0o700)
    assert not [k for k in env if k.startswith("MOXI_")]
    yield {"base": base, "home": home, "venv": venv, "py": py, "env": env,
           "wheel": wheel}
    shutil.rmtree(base, ignore_errors=True)


def unit_lines(path: Path, key: str) -> list[str]:
    return [line.partition("=")[2] for line in path.read_text().splitlines()
            if line.startswith(key + "=")]


def test_a_stranger_installs_and_two_agents_talk(installed):
    home, env, venv = installed["home"], installed["env"], installed["venv"]
    procs = []
    stub_pids = []

    def start(*argv, environ=None):
        argv = [str(venv / "bin" / argv[0]) if "/" not in argv[0] else argv[0], *argv[1:]]
        proc = subprocess.Popen(argv, env=environ or env, cwd=home,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        procs.append(proc)
        return proc

    def stop(proc):
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=20)
        procs.remove(proc)
        return code

    try:
        # The install is the package and the core, nothing else: no websockets (the
        # `codex` extra's).
        mods = run([str(installed["py"]), "-c", "import importlib.util as u; print("
                    "[m for m in ('moxi', 'websockets') if u.find_spec(m)])"],
                   env)
        assert mods.stdout.strip() == "['moxi']", mods

        # ---- Part one: the documented install, and nothing else -------------------
        # 1. `moxi comms doctor`, before anything touches the home: the local backend
        #    by default, with no slots yet.
        doctor = run(["moxi", "comms", "doctor"], env, cwd=home)
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        lines = doctor.stdout.splitlines()
        assert "backend     local (the default)" in lines, doctor.stdout
        assert ("slots       none yet (the daemon provisions 6 on its first start)"
                in lines), doctor.stdout
        assert f"{home}/.local/state/moxi/" in doctor.stdout
        assert "acme" not in doctor.stdout

        # 2. `moxi comms service install`: the unit, then the manager's two calls.
        installed_unit = run(["moxi", "comms", "service", "install"], env, cwd=home)
        assert installed_unit.returncode == 0, installed_unit.stdout + installed_unit.stderr
        unit = home / ".config" / "systemd" / "user" / "moxi-comms-daemon.service"
        assert unit.is_file()
        calls = read(home / "systemctl.calls").splitlines()
        assert calls == ["--user daemon-reload",
                         "--user enable --now moxi-comms-daemon.service"], calls

        # 3. The unit sets no backend or root.
        [exec_start] = unit_lines(unit, "ExecStart")
        environment = unit_lines(unit, "Environment")
        argv = shlex.split(exec_start)
        assert argv == [str(venv / "bin" / "moxi-comms-daemon")], exec_start
        assert not [e for e in environment if "MOXI_" in e or "XDG_" in e], environment

        # 4. Run the unit's own ExecStart in the foreground, with its Environment= lines.
        unit_env = dict(env)
        for entry in environment:
            for pair in shlex.split(entry):
                key, _, value = pair.partition("=")
                unit_env[key] = value
        sock = home / "run" / "moxi" / "comms" / "moxi-comms-daemon.sock"
        daemon = start(*argv, environ=unit_env)
        wait_for(sock.exists, what="the daemon's socket")

        # 5. The daemon provisioned on its first start: 6 slots, by the one definition
        #    of provisioned (doctor's slots line), and in the directory itself.
        doctor = run(["moxi", "comms", "doctor"], env, cwd=home)
        assert "slots       6 provisioned" in doctor.stdout.splitlines(), doctor.stdout
        assert "daemon      answers at " in doctor.stdout
        db = home / ".local" / "share" / "moxi" / "comms" / "directory.db"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            slots = [row[0] for row in conn.execute("SELECT slot_id FROM slots")]
        finally:
            conn.close()
        assert sorted(slots) == [1, 2, 3, 4, 5, 6]

        # ---- Part two: the named fixtures the exchange needs ------------------------
        # The OpenCode roster for two identities on slots 1 and 2, which part one
        # asserted, and the stub `opencode` on PATH, where the launcher looks for it.
        # Neither names a backend or makes a slot.
        config = home / ".config" / "moxi" / "comms"
        config.mkdir(parents=True, exist_ok=True)
        work = home / "work"
        work.mkdir()
        (config / "config.toml").write_text("".join(
            f'[opencode.agents."{name}"]\nslot = {slot}\ncwd = "{work}"\n\n'
            for name, slot in (("alice", 1), ("bob", 2))))
        (home / "opencode_stub.py").write_text(STUB_TURN)
        stub = home / "fakebin" / "opencode"
        stub.write_text(f"#!/bin/sh\nexec {sys.executable} {home / 'opencode_stub.py'} "
                        f"{TESTS} \"$@\"\n")
        stub.chmod(0o755)
        assert "backend" not in (config / "config.toml").read_text()

        # 6. Two OpenCode identities exchange a message through their stub servers. Each
        #    `moxi comms send` an agent makes runs inside its own session: its launcher spawned
        #    the server with the session's claim socket. The test is the operator at
        #    their own terminal, outside every session, with the id the daemon's first
        #    start provisioned, read from the directory as step 5 read the slots.
        def launch(name):
            records = home / "opencode-records" / name
            proc = OnPty([str(venv / "bin" / "moxi-comms-opencode"), name, "--notice-hold", "0"],
                         {**env, "FAKE_OPENCODE_DIR": str(records),
                          "FAKE_OPENCODE_AUTO": "0", "FAKE_OPENCODE_WINDOW": "",
                          "FAKE_OPENCODE_CONFIG": DEBUG_CONFIG}, home)
            procs.append(proc)
            return proc, records

        def server_events(records):
            path = records / "events.jsonl"
            return [json.loads(line) for line in read(path).splitlines() if line.strip()]

        (alice, alice_rec), (bob, bob_rec) = launch("alice"), launch("bob")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            [(operator_id,)] = conn.execute("SELECT user_id FROM operators").fetchall()
        finally:
            conn.close()
        operator_env = {**env, "MOXI_COMMS_OPERATOR": operator_id}
        wait_for(lambda: {"alice", "bob"} <= set(
                     run(["moxi", "comms", "agents"], env, cwd=home).stdout.split()),
                 timeout=60, what=lambda: "alice's and bob's bindings: "
                 + alice.text()[-2000:] + bob.text()[-2000:])
        for records in (alice_rec, bob_rec):
            wait_for(lambda: any(e["event"] == "window" for e in server_events(records)),
                     what="the windows")
            stub_pids.extend(e["pid"] for e in server_events(records)
                             if e["event"] in ("serve", "window"))
        # A launch stamp alone is no live session holding a name: refused, nothing sent.
        stamped = run(["moxi", "comms", "send", "bob", "a stamp alone"],
                      {**env, "MOXI_COMMS_NAME": "alice", "MOXI_COMMS_SLOT": "1",
                       "MOXI_COMMS_GLOBAL": "1"}, cwd=home)
        assert stamped.returncode == 2, stamped.stdout + stamped.stderr
        assert ("this process is not inside the session that holds alice — nothing was "
                "sent.") in stamped.stdout, stamped.stdout
        sent = run(["moxi", "comms", "send", "alice", "please ping bob"], operator_env, cwd=home)
        assert sent.returncode == 0, sent.stdout + sent.stderr
        wait_for(lambda: "please ping bob" in read(home / "opencode-alice.log"),
                 what="alice's server to get the operator's message")
        wait_for(lambda: "ping from alice" in read(home / "opencode-bob.log"),
                 what=lambda: "bob's server to get alice's message: "
                 + read(home / "opencode-alice.sent"))
        wait_for(lambda: "pong from bob" in read(home / "opencode-alice.log"),
                 what=lambda: "alice's server to get bob's answer: "
                 + read(home / "opencode-bob.sent"))
        assert "a stamp alone" not in read(home / "opencode-bob.log")
        # Each sent as its own identity: the frame names the sender, not just the body
        # its turn typed (s157.1).
        assert "[comms] directive from operator" in read(home / "opencode-alice.log")
        assert "[comms] from alice · msg" in read(home / "opencode-bob.log")
        assert "[comms] from bob · msg" in read(home / "opencode-alice.log")

        # 7. A clean exit on SIGTERM, and everything stops.
        for proc in (alice, bob):
            assert stop(proc) == 0, proc.text()[-2000:]
        assert stop(daemon) == 0, "the daemon exits 0 on SIGTERM"
        assert not sock.exists(), "and takes its socket with it"
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()
        for pid in stub_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # 8. Nothing outside the home. The audit log is the hook's own output, kept beside it.
    audit = installed["base"] / "audit.tsv"
    events = [line.split("\t") for line in audit.read_text().splitlines()]
    assert len(events) > 20, "the hook ran in the installed processes"
    allowed = (str(home) + os.sep, "/dev/")
    outside = sorted({(e[0], e[2], e[3]) for e in events
                      if not e[3].startswith(allowed) and e[3] != os.devnull})
    assert outside == [], json.dumps(outside, indent=1)
    assert not [e for e in events if "acme" in e[3]]
    programs = {e[2] for e in events}
    assert {"moxi-comms-daemon", "moxi-comms-opencode", "moxi"} <= programs

    assert "moxi-comms-provision" not in programs, "the test ran no provisioning"
    assert (home / ".local" / "state" / "moxi").is_dir()
