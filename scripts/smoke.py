#!/usr/bin/env python3
"""End-to-end CLI smoke run against the mock gateway -- real processes, real sockets.

Boots a file-backed mock record, provisions it, starts a real `moxi-comms-daemon`, two real
`moxi-comms-shim` adapters, and drives `moxi comms`. Proves the basic loop outside pytest:

    send -> route -> digest -> inject -> turn completes -> cursor advances -> reply.

Each shim runs under its own session root, a small process that starts the shim and
then runs that identity's `moxi comms` commands as its own children. Each run sends
through its shim's claim socket from inside that shim's session. The run also checks
the refusals: a stamp alone, this script (outside every session) carrying a claim
socket, and one session carrying the other's socket.

The environment is built from nothing comms-related: no `MOXI_COMMS_*` and no Claude
Code session id from the shell that runs it, so the smoke never reaches a live
session's claim socket or daemon.

    uv run python scripts/smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: The bin directory of the environment running this script: the workspace's `.venv`
#: (in a Moxi workspace it sits at the workspace root, not in this repo).
BIN = Path(sys.executable).parent


#: A session root: starts the shim named in argv[1] (JSON) as its child, so the shim's
#: parent, this process, is the session's tree root. Then it runs each command it reads
#: on stdin, one JSON line `{"argv": [...], "env": {...}}`, as its own child, inside the
#: session, and writes one JSON line back. At EOF it stops the shim and exits.
SESSION_ROOT = r"""
import json, subprocess, sys
shim = subprocess.Popen(json.loads(sys.argv[1]), stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
try:
    for line in sys.stdin:
        req = json.loads(line)
        out = subprocess.run(req["argv"], env=req["env"], capture_output=True, text=True)
        sys.stdout.write(json.dumps({"returncode": out.returncode, "stdout": out.stdout,
                                     "stderr": out.stderr}) + "\n")
        sys.stdout.flush()
finally:
    shim.terminate()
    try:
        shim.wait(timeout=5)
    except subprocess.TimeoutExpired:
        shim.kill()
"""

#: What the smoke never inherits from the shell that runs it.
FOREIGN = ("MOXI_COMMS_",)
FOREIGN_NAMES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR")


def clean_environ() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(FOREIGN) and k not in FOREIGN_NAMES}


class Session:
    """One identity's session: its root process and the shim under it."""

    def __init__(self, shim_argv, env):
        self.proc = subprocess.Popen([sys.executable, "-c", SESSION_ROOT,
                                      json.dumps([str(a) for a in shim_argv])],
                                     env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)

    def run(self, argv, env) -> subprocess.CompletedProcess:
        self.proc.stdin.write(json.dumps({"argv": [str(a) for a in argv],
                                          "env": env}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("smoke FAILED: a session root died")
        got = json.loads(line)
        return subprocess.CompletedProcess(argv, got["returncode"], got["stdout"],
                                           got["stderr"])

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def wait(pred, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise SystemExit(f"smoke FAILED: timed out waiting for {what}")


def touch_until(boundary, pred, timeout=10.0, every=1.0, what="condition"):
    """A Stop hook at `boundary`, again every `every` seconds until `pred` holds. Mail
    reaches the shim after `moxi comms send` returns, so one touch can come before it. The
    spacing is far longer than the shim's poll, so a touch that did inject is seen
    before the next one, which would complete the turn."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        boundary.touch()
        step = time.monotonic() + every
        while time.monotonic() < min(step, deadline):
            value = pred()
            if value:
                return value
            time.sleep(0.05)
    raise SystemExit(f"smoke FAILED: timed out waiting for {what}")


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    home = Path(tempfile.mkdtemp(prefix="comms-smoke-"))
    runtime = Path(tempfile.mkdtemp(prefix="cms"))
    (runtime / "comms").mkdir()         # comms' runtime directory, as a login has it

    store = home / "gateway.json"
    for key in list(os.environ):
        if key.startswith(FOREIGN) or key in FOREIGN_NAMES:
            del os.environ[key]
    env = {**clean_environ(),
           "MOXI_COMMS_HOME": str(home), "MOXI_COMMS_DEVICE": "smokebox",
           "MOXI_COMMS_RUNTIME_DIR": str(runtime), "MOXI_COMMS_PROJECT": "smoke",
           "MOXI_COMMS_MOCK_GATEWAY": str(store),
           "PYTHONPATH": str(REPO / "src")}
    # The cold-cache check reads each recipient's clock from its transcript, by the
    # session id and the transcript root its shim reports (`CLAUDE_CONFIG_DIR/projects`).
    # Each smoke session gets an empty transcript there, which reads `fresh` (read
    # whole, no request in it) whether or not a Stop hook has reported a turn, so the
    # smoke's sends are live deliveries and never held. Without one, a session that
    # has turned reads `unknown` and the gate holds its mail.
    claude_dir = home / "claude"
    os.environ.update(env)

    from moxi_comms.mock_gateway import MockGateway
    from moxi_comms.paths import cursors_dir, directory_db
    from moxi_comms.provision import provision

    procs, sessions = [], {}
    try:
        provision(MockGateway(store), directory_db(), slots=4,
                  standing=[("main", "smoke")], operator=("900001", "operator"))
        print(f"provisioned -> {directory_db()}")

        procs.append(subprocess.Popen([BIN / "moxi-comms-daemon",
                                       "--gateway-store", str(store)],
                                      env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL))
        sock = runtime / "comms" / "moxi-comms-daemon.sock"
        wait(lambda: sock.exists(), what="the daemon socket")

        inbox, claim = {}, {}
        for slot, name in ((1, "planner"), (2, "reviewer")):
            inject = home / f"{name}.inject"
            boundary = home / f"{name}.turn"
            inbox[name] = (inject, boundary)
            # Each shim serves its session's claim socket, and the session's root is the
            # shim's parent: its own session root, never this script, so the two
            # sessions share no tree root and this script is inside neither. The
            # `moxi comms` runs for an identity are its root's children. Only a live
            # session holding a name sends under it: a stamp alone is refused. Every
            # socket is inside comms' runtime directory, the only place comms uses one.
            claim[name] = runtime / "comms" / f"comms-claim-smoke-{name}.sock"
            session_id = str(uuid.uuid4())
            transcripts = claude_dir / "projects" / f"smoke-{name}"
            transcripts.mkdir(parents=True)
            (transcripts / f"{session_id}.jsonl").touch()
            sessions[name] = Session(
                [BIN / "moxi-comms-shim", "--comm-slot", str(slot), "--name", name,
                 "--scope", "smoke", "--inject-file", str(inject),
                 "--boundary-file", str(boundary), "--claim-socket", str(claim[name])],
                {**env, "CLAUDE_CODE_SESSION_ID": session_id,
                 "CLAUDE_CONFIG_DIR": str(claude_dir)})

        def show(argv, out, where):
            print(f"  [{where}] $ moxi comms {' '.join(argv)}\n{out.stdout.rstrip()}")
            if out.returncode != 0 and out.stderr.strip():
                print(out.stderr)
            return out

        def run(argv, **extra):
            """`moxi comms` inside the session whose claim socket `extra` carries."""
            name = next(n for n, path in claim.items()
                        if str(path) == extra.get("MOXI_COMMS_CLAIM_SOCKET"))
            return show(argv, sessions[name].run([BIN / "moxi", "comms", *argv],
                                                 {**env, **extra}), name)

        def run_outside(argv, **extra):
            """`moxi comms` from this script, which is inside no session."""
            return show(argv, subprocess.run([BIN / "moxi", "comms", *argv],
                                             env={**env, **extra},

                                             capture_output=True, text=True), "outside")

        planner_env = {"MOXI_COMMS_SLOT": "1", "MOXI_COMMS_NAME": "planner", "MOXI_COMMS_SCOPE": "smoke",
                       "MOXI_COMMS_CLAIM_SOCKET": str(claim["planner"])}
        reviewer_env = {"MOXI_COMMS_SLOT": "2", "MOXI_COMMS_NAME": "reviewer", "MOXI_COMMS_SCOPE": "smoke",
                        "MOXI_COMMS_CLAIM_SOCKET": str(claim["reviewer"])}

        wait(lambda: "planner@smoke" in run(["agents"], **planner_env).stdout,
             what="both shims to register")

        out = run(["send", "reviewer", "does the loop hold end to end?", "--posture", "once"],
                  **planner_env)
        assert out.returncode == 0, out.stderr
        message_id = out.stdout.split()[1]

        inject, boundary = inbox["reviewer"]
        touch_until(boundary, lambda: inject.exists() and inject.read_text().strip(),
                    what="the injected digest")           # Stop hook: inject the digest
        digest = json.loads(inject.read_text().splitlines()[0])["digest"]
        assert "[comms] from planner@smoke · msg" in digest, digest
        assert "does the loop hold end to end?" in digest
        assert "::to" not in digest, "the machine line must never be injected"
        print(f"  injected digest:\n      {digest.replace(chr(10), chr(10) + '      ')}")

        cursor = cursors_dir() / "reviewer@smoke.json"
        assert not cursor.exists(), "the cursor must not advance before the turn completes"
        time.sleep(0.2)
        boundary.touch()                                    # the turn runs to completion
        wait(lambda: cursor.exists(), what="the cursor advance")
        assert json.loads(cursor.read_text())["cursor"] == message_id

        out = run(["reply", message_id, "it holds."], **reviewer_env)
        assert out.returncode == 0, out.stderr
        p_inject, p_boundary = inbox["planner"]
        touch_until(p_boundary, lambda: p_inject.exists() and p_inject.read_text().strip(),
                    what="the threaded reply")
        # The injector writes one JSON line per digest, and `json.dumps` escapes the
        # frame line's `·` to `·` -- so the assertions below have to run against the
        # DECODED digest, exactly as line 97 already does for the first one. Asserting
        # against the raw file made `· re: <id>` unfindable and the smoke red.
        replied = json.loads(p_inject.read_text().splitlines()[0])["digest"]
        assert "it holds." in replied
        # the correlation crossed two processes inside the record itself: the reply verb
        # wrote `re:<id>` onto line zero and the daemon parsed the payload back out of it.
        assert f"· re: {message_id}" in replied, replied
        assert f"re:{message_id}" not in replied, "the wire token itself stays transport"

        run(["send", "curator", "you are not bound yet", "--posture", "none"], **planner_env)
        stamp_only = {k: v for k, v in planner_env.items() if k != "MOXI_COMMS_CLAIM_SOCKET"}
        out = run_outside(["send", "reviewer", "a stamp alone", "--posture", "none"],
                          **stamp_only)
        assert out.returncode == 2, out
        assert ("this process is not inside the session that holds planner@smoke — "
                "nothing was sent.") in out.stdout, out
        # A claim socket answers only inside its own session: not this script, and not
        # the other shim's session, which has its own root.
        out = run_outside(["send", "reviewer", "from outside", "--posture", "none"],
                          **planner_env)
        assert out.returncode == 2 and "not inside the session" in out.stdout, out
        out = sessions["reviewer"].run([BIN / "moxi", "comms", "send", "reviewer", "borrowed",
                                        "--posture", "none"],
                                       {**env, **planner_env})
        show(["send", "reviewer", "borrowed (planner's socket)"], out, "reviewer")
        assert out.returncode == 2 and "not inside the session" in out.stdout, out
        run(["agents"], **planner_env)
        run(["history", "reviewer@smoke"], **planner_env)
        print("\nSMOKE OK: send -> route -> digest -> inject -> complete -> cursor -> reply")
        return 0
    finally:
        for session in sessions.values():
            session.close()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
