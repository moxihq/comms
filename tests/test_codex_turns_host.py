"""The real turns Codex host behind comms, with the fake app-server binary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

from conftest import clock_reads_warm, cursor_of, wait_until

from fake_codex import calls, control_call, events, install


IDENTITY = "hosted@proj-a"


def launcher(records, tmp_path):
    """`moxi-comms-codex` for IDENTITY over turns' real host and the fake `codex`."""
    binary = install(tmp_path / "bin")
    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "candidate.toml"
    config.write_text(f'[codex.agents."{IDENTITY}"]\nslot = 4\ncwd = "{work}"\n')
    env = dict(os.environ)
    env.update({
        "PATH": f"{binary.parent}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_CODEX_DIR": str(records),
        "MOXI_TURNS_RUNTIME_DIR": env["MOXI_COMMS_RUNTIME_DIR"],
    })
    return subprocess.Popen(
        [sys.executable, "-m", "moxi_comms.adapters.codex", IDENTITY,
         "--config", str(config)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def digest_with(records, message_id) -> list:
    """The `turn/start` and `turn/steer` calls whose input names the message."""
    return [c["method"] for c in calls(records)
            if c["method"] in ("turn/start", "turn/steer")
            and any(f"msg {message_id}" in (item.get("text") or "")
                    for item in c["params"].get("input") or [])]


def test_mail_sent_through_the_daemon_reaches_the_hosted_thread_and_settles(
        rig, tmp_path, monkeypatch):
    """comms' own path over turns' real host: `moxi comms send`, the daemon, the
    launcher, turns' host and the app-server. The cursor moves when the turn ends."""
    clock_reads_warm(monkeypatch, "codex")
    records = tmp_path / "records"
    records.mkdir()
    proc = launcher(records, tmp_path)
    try:
        assert wait_until(lambda: IDENTITY in rig.daemon.bindings, 20), \
            proc.stdout.read() if proc.poll() is not None else "no binding"
        assert wait_until(lambda: rig.daemon.bindings[IDENTITY].session_id, 20)
        code, out = rig.cli(["send", "--posture", "none", IDENTITY, "through the daemon"],
                            slot=1, name="main", scope="proj-a")
        assert code == 0, out
        mid = out.split()[1]
        assert wait_until(lambda: digest_with(records, mid), 20), calls(records)
        assert cursor_of(IDENTITY) != mid, "not before the turn ends"
        control_call(records, {"op": "complete"})
        assert wait_until(lambda: cursor_of(IDENTITY) == mid, 15)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=20)[0]
    assert proc.returncode == 0, output


def test_the_launcher_runs_turns_real_host_and_passes_the_roster_options(
        rig, tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    binary = install(tmp_path / "bin")
    work = tmp_path / "work"
    work.mkdir()
    writable = tmp_path / "writable"
    writable.mkdir()
    config = tmp_path / "candidate.toml"
    config.write_text(
        f'[codex.agents."{IDENTITY}"]\n'
        'slot = 4\n'
        f'cwd = "{work}"\n'
        'sandbox = "workspace-write"\n'
        f'writable_roots = ["{writable}"]\n'
        'network_access = true\n'
        'model = "gpt-sample"\n'
    )
    env = dict(os.environ)
    env.update({
        "PATH": f"{binary.parent}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_CODEX_DIR": str(records),
        "MOXI_TURNS_RUNTIME_DIR": env["MOXI_COMMS_RUNTIME_DIR"],
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "moxi_comms.adapters.codex", IDENTITY,
         "--config", str(config)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert wait_until(lambda: calls(records, "thread/start"), 20), \
            proc.stdout.read() if proc.poll() is not None else "thread/start did not arrive"
        assert wait_until(lambda: IDENTITY in rig.daemon.bindings, 10)
        params = calls(records, "thread/start")[0]["params"]
        assert params["cwd"] == str(work)
        assert params["approvalPolicy"] == "never"
        assert params["developerInstructions"]
        assert params["model"] == "gpt-sample"
        assert params["sandbox"] == "workspace-write"
        assert params["config"]["agents"] == {"enabled": False}
        settings = params["config"]["sandbox_workspace_write"]
        assert settings["network_access"] is True
        assert str(writable) in settings["writable_roots"]
        # A launch with no window is primed too, with no mail sent, so its thread has a
        # rollout that a later `thread/resume` can load; and the daemon has its id.
        assert wait_until(lambda: calls(records, "turn/start"), 10)
        thread_id = calls(records, "turn/start")[0]["params"]["threadId"]
        assert wait_until(
            lambda: rig.daemon.bindings[IDENTITY].session_id == thread_id, 10)
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=20)[0]
    assert proc.returncode == 0, output
    assert wait_until(lambda: IDENTITY not in rig.daemon.bindings, 5)
    assert wait_until(lambda: any(row["event"] == "app-server-exit"
                                  for row in events(records)), 5)
