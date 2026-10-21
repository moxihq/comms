"""`moxi comms claude` forks the session's adapter before it execs `claude` (design §5.9),
and the daemon's no-endpoint state as a sender and the doctor read it.

The launch runs for real in a child process, with a stand-in `claude` that records what
it found when it started and then exits: the adapter is up, bound and listening on the
claim socket before `claude` starts (the launcher waits for its ready byte), its claim's
root is the `claude` process (the launcher's pid, kept across the exec), it reports no
endpoint while the session has none, and it deregisters and cleans up when `claude`
exits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest
from conftest import wait_until

from moxi_comms import claimfile
from moxi_comms.adapters import session

NAME = "launched"
#: The id the launch names to `claude` (RULE D6), and its session dir's name.
SID = "5e55a1d0-0000-4000-8000-0000000000f1"


def binding(rig, qualified=NAME):
    with rig.daemon._lock:
        return rig.daemon.bindings.get(qualified)


@pytest.fixture
def fake_claude(tmp_path):
    out = tmp_path / "claude.out"
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ -S "$MOXI_COMMS_CLAIM_SOCKET" ]; then echo socket-ready >> {out}; fi
        echo "pid $$" >> {out}
        echo "args $*" >> {out}
        while [ ! -e {tmp_path}/release ]; do sleep 0.05; done
        """))
    script.chmod(0o755)
    return script, out, tmp_path / "release"


def test_the_launch_forks_a_bound_adapter_before_claude_and_it_ends_with_claude(
        rig, fake_claude):
    script, out, release = fake_claude
    code = ("from moxi_comms.adapters import session; raise SystemExit(session.main("
            f"['--name', {NAME!r}, '--session-id', {SID!r}, '--claude', {str(script)!r}, "
            "'--', '--model', 'haiku']))")
    proc = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_until(lambda: out.exists() and "args" in out.read_text(), timeout=15)
        lines = out.read_text().splitlines()
        assert lines[0] == "socket-ready", "the claim socket listened before claude started"
        pid = int(lines[1].split()[1])
        assert pid == proc.pid, "claude keeps the launcher's pid"
        assert lines[2] == f"args --session-id {SID} --model haiku -n {NAME}", \
            "the launch names its id to claude"
        b = binding(rig)
        assert b is not None, "bound before claude started"
        assert b.endpoint is False, "the session has no turns endpoint"
        assert b.session_id == SID, "the adapter states the named id from its bind"
        record = claimfile.read(SID)
        assert [r["pid"] for r in record["roots"]] == [pid], "claude is the claim's root"
        sock = session.sessions_dir() / SID / "claim.sock"
        assert sock.is_socket()
    finally:
        release.touch()
    proc.wait(timeout=15)
    assert proc.returncode == 0, proc.stderr.read()
    assert wait_until(lambda: binding(rig) is None, timeout=10), "the name deregisters"
    assert wait_until(lambda: not (session.sessions_dir() / SID).exists(), timeout=5)
    assert claimfile.read(SID) is None
    log = session.log_dir() / f"claude-{SID}.log"
    assert "claude pid" in log.read_text()


def test_the_launcher_waits_a_bounded_time_and_then_starts_claude(capsys, monkeypatch):
    """A ready byte that never comes holds `claude` up by `ready_wait` and no more."""
    s = session.Session("fk2", name=NAME, project="proj-a")
    s.write()
    r, w = os.pipe()
    try:
        assert session._await_ready(r, 0.05) is False
        os.write(w, b"1")
        assert session._await_ready(r, 0.05) is True
    finally:
        os.close(r)
        os.close(w)


# ------------------------------------------------------------- the id the launch names

OTHER = "5e55a1d0-0000-4000-8000-0000000000f2"


def dry_run(capsys, *passthrough, own=None) -> tuple[int, str, list[str], str]:
    """(exit, the session line's id, the argv after `claude` minus `-n`, stderr) of a
    dry-run launch with this passthrough."""
    argv = ["--name", NAME, "--dry-run"]
    if own is not None:
        argv += ["--session-id", own]
    code = session.main([*argv, "--", *passthrough])
    cap = capsys.readouterr()
    if code != 0:
        return code, "", [], cap.err
    lines = cap.out.splitlines()
    sid = lines[0].split()[1]
    launch = next(ln for ln in lines if ln.startswith("  launch")).split()[1:]
    assert launch[0] == "claude" and launch[-2:] == ["-n", NAME]
    return code, sid, launch[1:-2], cap.err


def is_uuid(value: str) -> bool:
    import uuid
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def test_a_launch_that_names_no_id_gets_one(capsys):
    code, sid, argv, _ = dry_run(capsys, "--model", "haiku")
    assert code == 0 and is_uuid(sid)
    assert argv == ["--session-id", sid, "--model", "haiku"], "a fresh UUID, named"
    code, sid, argv, _ = dry_run(capsys, own=SID)
    assert (sid, argv) == (SID, ["--session-id", SID]), "the launcher's own id, named"


@pytest.mark.parametrize("passthrough", [
    ["--session-id", SID], [f"--session-id={SID}"],
    ["--resume", SID], ["-r", SID], [f"--resume={SID}"],
], ids=["session-id", "session-id=", "resume", "-r", "resume="])
def test_an_id_the_passthrough_names_is_the_id(capsys, passthrough):
    code, sid, argv, _ = dry_run(capsys, *passthrough)
    assert (code, sid, argv) == (0, SID, passthrough), "passed through, nothing added"
    code, sid, argv, _ = dry_run(capsys, *passthrough, own=SID)
    assert (code, sid, argv) == (0, SID, passthrough), "the launcher's own id agrees"


@pytest.mark.parametrize("passthrough", [
    ["--resume", SID, "--fork-session"], ["--continue", "--fork-session"],
], ids=["resume-fork", "continue-fork"])
def test_a_fork_is_named_a_new_id(capsys, passthrough):
    code, sid, argv, _ = dry_run(capsys, *passthrough)
    assert code == 0 and is_uuid(sid) and sid != SID
    assert argv == ["--session-id", sid, *passthrough]
    code, sid, argv, _ = dry_run(capsys, *passthrough, own=OTHER)
    assert (sid, argv) == (OTHER, ["--session-id", OTHER, *passthrough])


@pytest.mark.parametrize("passthrough", [
    ["--continue"], ["-c"], ["--resume"], ["-r"], ["--resume", "cache-bug"],
    ["--resume", "--model", "haiku"], ["--from-pr", "12"], ["--from-pr=12"],
    ["--teleport"], ["--teleport=abc"],
], ids=["continue", "-c", "resume", "-r", "resume-search", "resume-then-flag",
        "from-pr", "from-pr=", "teleport", "teleport="])
def test_an_unnamed_resume_passes_through_and_names_no_id(capsys, passthrough):
    code, sid, argv, _ = dry_run(capsys, *passthrough)
    assert code == 0 and argv == passthrough, "unchanged, no --session-id added"
    stated, added = session.launch_session_id(passthrough)
    assert (stated, added) == (None, []), "the binding states no id until the look"
    code, sid, argv, _ = dry_run(capsys, *passthrough, own=SID)
    assert (code, sid, argv) == (0, SID, passthrough), \
        "the launcher's own id names the session dir and nothing else"
    assert session.launch_session_id(passthrough, SID) == (None, [])


@pytest.mark.parametrize("passthrough", [
    ["--resume", SID.upper()], ["-r", SID.upper()], ["--session-id", SID.upper()],
], ids=["resume", "-r", "session-id"])
def test_an_uppercase_id_is_stated_in_lowercase(capsys, passthrough):
    """`claude` and turns report the id in lowercase and the transcript is named by it,
    so the binding states that spelling. The passthrough is not rewritten."""
    code, sid, argv, _ = dry_run(capsys, *passthrough)
    assert (code, sid, argv) == (0, SID, passthrough)
    code, sid, argv, _ = dry_run(capsys, *passthrough, own=SID.upper())
    assert (code, sid, argv) == (0, SID, passthrough)


def test_an_uppercase_own_id_is_named_and_stated_in_lowercase(capsys):
    code, sid, argv, _ = dry_run(capsys, own=SID.upper())
    assert (code, sid, argv) == (0, SID, ["--session-id", SID])
    code, sid, argv, _ = dry_run(capsys, "--continue", own=SID.upper())
    assert (code, sid, argv) == (0, SID, ["--continue"]), "the session dir, in lowercase"


def test_a_second_launch_of_a_running_session_id_is_refused(capsys):
    """Two launches of one id would share one session dir and one claim socket. A
    claim socket that answers refuses the second; a stale socket file does not."""
    import socket
    path = session.Session(SID, name=NAME).claim_socket
    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
        srv.listen(1)
        code, _, _, err = dry_run(capsys, "--resume", SID)
        assert code == 2 and "already running under comms" in err, err
    finally:
        srv.close()
    assert path.exists(), "the socket file stays behind, stale"
    code, sid, _, _ = dry_run(capsys, "--resume", SID)
    assert (code, sid) == (0, SID)


def test_flags_after_claudes_own_double_dash_are_not_read():
    stated, added = session.launch_session_id(["-p", "--", "--continue"])
    assert stated is not None and added == ["--session-id", stated]


@pytest.mark.parametrize("own,passthrough,says", [
    ("fk1", [], "is not a UUID"),
    ("fk1", ["--continue"], "is not a UUID"),
    (OTHER, ["--session-id", SID], "differs from the id the passthrough names"),
    (OTHER, ["--resume", SID], "differs from the id the passthrough names"),
    (None, ["--session-id", "fk1"], "after -- is not a UUID"),
], ids=["own-not-uuid", "own-not-uuid-continue", "differs-session-id", "differs-resume",
        "passthrough-not-uuid"])
def test_the_launcher_refuses_an_id_it_cannot_name(capsys, own, passthrough, says):
    code, _, _, err = dry_run(capsys, *passthrough, own=own)
    assert code == 2 and says in err, err


def test_the_adapter_states_the_named_id_from_its_bind():
    """RULE D6: the adapter's shim carries the launch's id before any look; an unnamed
    resume's carries none."""
    for stated in (SID, None):
        s = session.Session(stated or "fk3", name=NAME, project="proj-a")
        s.stated_id = stated
        shim = session.adapter_shim(s, os.getpid())
        try:
            assert shim.session_id == stated and shim.turned is False
        finally:
            shim.stop(remove_claim_file=True)


# --------------------------------------------------------------------------- the daemon

def test_the_session_query_and_the_roster_carry_the_endpoint_state(rig):
    from moxi_comms.adapters.claude_adapter import ClaudeAdapter
    shim = rig.shim(name="reviewer", scope="proj-a", start=False)
    a = ClaudeAdapter(shim, 1, look_interval=60).start(tolerate_offline=False)
    try:
        assert wait_until(lambda: binding(rig, "reviewer@proj-a") is not None)
        reply = rig.daemon._session_query({"identity": "reviewer@proj-a"})
        assert reply["endpoint"] is False and reply["endpoint_for"] >= 0
        agents = rig.daemon._query({"kind": "agents"})["bindings"]
        (row,) = [b for b in agents if b["qualified"] == "reviewer@proj-a"]
        assert row["endpoint"] is False and row["endpoint_since"] is not None
        shim.notify_endpoint(True)
        assert wait_until(lambda: binding(rig, "reviewer@proj-a").endpoint is True)
    finally:
        a.stop()
    plain = rig.shim(name="other", scope="proj-a")
    assert wait_until(lambda: binding(rig, "other@proj-a") is not None)
    assert rig.daemon._session_query({"identity": "other@proj-a"})["endpoint"] is None, \
        "an adapter that reports no endpoint state reads as before"
    assert plain.endpoint is None
    unbound = rig.daemon._session_query({"identity": "nobody@proj-a"})
    assert unbound["endpoint"] is None and unbound["endpoint_for"] is None


def test_the_doctor_names_a_bound_session_with_no_endpoint_and_turns_rows(rig):
    from moxi_comms.adapters.claude_adapter import ClaudeAdapter
    shim = rig.shim(name="reviewer", scope="proj-a", start=False)
    a = ClaudeAdapter(shim, 1, look_interval=60).start(tolerate_offline=False)
    try:
        assert wait_until(lambda: binding(rig, "reviewer@proj-a") is not None)
        code, out = rig.cli(["doctor"])
    finally:
        a.stop()
    assert code == 0, out
    waiting = [ln for ln in out.splitlines() if ln.startswith("waiting")]
    assert len(waiting) == 1 and "reviewer@proj-a: bound, no turns endpoint for" in \
        waiting[0], out
    turns = [ln for ln in out.splitlines() if ln.startswith("turns")]
    assert any("listener comms: registered for ready, prompt" in ln for ln in turns), out
    assert any("endpoints: 0 live" in ln for ln in turns), out


def test_the_doctor_says_none_are_waiting(rig):
    code, out = rig.cli(["doctor"])
    assert code == 0, out
    assert any(ln.startswith("waiting") and "none" in ln for ln in out.splitlines()), out


def test_a_register_carries_the_endpoint_state(rig, monkeypatch):
    from moxi_comms.client import DaemonClient
    seen = []
    real = DaemonClient.request

    def request(self, op, **kw):
        if op == "register":
            seen.append(kw)
        return real(self, op, **kw)

    monkeypatch.setattr(DaemonClient, "request", request)
    shim = rig.shim(name="reviewer", scope="proj-a", start=False)
    shim.endpoint = False
    shim.start()
    assert seen[-1]["endpoint"] is False
    plain = rig.shim(name="other", scope="proj-a")
    assert "endpoint" not in seen[-1]
    del plain
    json.dumps(seen[0])


def test_a_look_releases_held_mail_on_a_passing_read_and_is_no_turn(rig, monkeypatch):
    """`activity kind=look`: the adapter's first look at an idle session. It releases
    what the daemon holds for the name, as a turn end does, and leaves `turned` alone."""
    from conftest import clock_reads_warm, own_activity, queued
    clock_reads_warm(monkeypatch, "claude-shim")
    rig.shim(slot=1, name="planner", scope="proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    code, out = rig.cli(["send", "--posture", "none", "--held", "reviewer@proj-a", "later"],
                        slot=1, name="planner", scope="proj-a")
    assert code == 0, out
    assert wait_until(lambda: queued(rig.daemon, "reviewer@proj-a"))
    assert reviewer.queue == [], "a [held] message waits for a release point"
    with rig.daemon._lock:
        rig.daemon.bindings["reviewer@proj-a"].turned = False
        rig.daemon.turned_ids.discard(reviewer.session_id)
    reviewer.turned = False
    own_activity(reviewer, kind="look")
    assert wait_until(lambda: len(reviewer.queue) == 1), "the look released it"
    assert binding(rig, "reviewer@proj-a").turned is False, "a look is no turn"
