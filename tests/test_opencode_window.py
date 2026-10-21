"""`moxi-comms-opencode <identity>` end to end on a pty, as a terminal runs it: the real
launcher, the rig's daemon, and the stub `opencode` (`tests/fake_opencode.py`) as both
the server and the window. No model, no network."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from conftest import wait_until, write_config

import fake_opencode
from moxi_comms.adapters import opencode as oc
from moxi_comms.adapters import opencode_roster
from pty_kit import PtyLauncher, alive

IDENTITY = "coder@proj-a"


class OpenCodePty(PtyLauncher):
    def __init__(self, argv, env):
        import fcntl
        import termios
        import threading

        master, slave = os.openpty()
        self.fd = master
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "moxi_comms.adapters.opencode", *argv], env=env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.started = time.monotonic()
        self.out = bytearray()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()


@pytest.fixture
def oc_rig(rig, tmp_path):
    records = tmp_path / "rec"
    records.mkdir()
    stub = fake_opencode.install(tmp_path / "bin")
    work = tmp_path / "work"
    work.mkdir()
    roster = write_config(None, f'[opencode.agents."{IDENTITY}"]\nslot = 5\ncwd = "{work}"\n')
    launched = []

    class Rig:
        pass

    w = Rig()
    w.records, w.stub, w.roster, w.rig = records, stub, roster, rig

    def launch(*, window="alt,exit-after=1.5", free=None, extra=()):
        env = dict(os.environ)
        env.update({"FAKE_OPENCODE_DIR": str(records), "FAKE_OPENCODE_WINDOW": window})
        if free is not None:
            env["FAKE_OPENCODE_FREE"] = free
        proc = OpenCodePty([IDENTITY, "--opencode", str(stub),
                            "--notice-hold", "0.5", "--ready-wait", "20", *extra], env)
        launched.append(proc)
        return proc

    def events(kind=None):
        return [e for e in fake_opencode.events(records) if kind is None or e["event"] == kind]

    w.launch, w.events = launch, events
    yield w
    for proc in launched:
        proc.close()
    for e in events():
        if e["event"] in ("serve", "window") and alive(e["pid"]):
            os.kill(e["pid"], signal.SIGKILL)


def bound(rig) -> bool:
    return IDENTITY in rig.daemon.bindings


def test_the_launch_opens_the_window_on_its_own_server_and_a_close_takes_it_offline(
        oc_rig):
    w = oc_rig
    proc = w.launch()
    opened = wait_until(lambda: w.events("window"), 20.0)
    assert opened, proc.text()
    (serve,) = w.events("serve")
    kinds = [e["event"] for e in w.events()]
    assert kinds.index("models-refresh") < kinds.index("serve"), \
        "the model list is refreshed before the server starts"
    assert serve["argv"] == ["serve", "--hostname", "127.0.0.1", "--port", "0",
                             "--print-logs", "--log-level", "INFO"]
    assert serve["password"] is True
    assert serve["stamp"] == {"MOXI_COMMS_NAME": "coder", "MOXI_COMMS_SLOT": "5",
                              "MOXI_COMMS_SCOPE": "proj-a"}
    assert serve["config"]["model"] == serve["config"]["small_model"] == \
        "opencode/minimax-m3.1-flash-free"
    assert serve["config"]["permission"] == {"external_directory": "allow",
                                             "doom_loop": "deny"}
    (layer_one,) = serve["config"]["instructions"]
    assert os.path.isabs(layer_one) and "comms" in open(layer_one).read()
    session = oc.read_record(opencode_roster.session_path(IDENTITY))["session_id"]
    event = opened[0]
    assert event["argv"][1:] == ["attach", serve["url"], "-s", session]
    assert event["foreground"] is True, "the window holds the terminal"
    assert bound(w.rig)

    assert proc.wait(20.0) == 0
    assert wait_until(lambda: not bound(w.rig)), "closing the window took it offline"
    assert wait_until(lambda: not alive(serve["pid"]), 10.0), "the server was stopped"
    out = proc.text()
    assert f"`moxi-comms-opencode {IDENTITY}` launches it again, on a new session" in out
    assert "is not offered" not in out and "no free model" not in out
    assert "could not be refreshed" not in out


def test_a_missing_default_is_printed_before_the_window_and_the_server_restarts_on_a_free_one(
        oc_rig):
    w = oc_rig
    proc = w.launch(free="opencode/big-pickle")
    assert wait_until(lambda: w.events("window"), 20.0), proc.text()
    serves = w.events("serve")
    assert [s["config"].get("model") for s in serves] == ["opencode/minimax-m3.1-flash-free",
                                                          "opencode/big-pickle"]
    out_at_window = proc.text()
    assert ("opencode/minimax-m3.1-flash-free is not offered: this session's default is "
            "opencode/big-pickle") in out_at_window, "on screen before the window opened"
    assert "the window opens in 0.5s" in out_at_window
    assert proc.wait(20.0) == 0
    lines = oc.status_lines(opencode_roster.session_path(IDENTITY))
    assert any("is not offered" in row["text"] for row in lines)


def test_with_no_free_model_the_launch_says_so_and_sets_none(oc_rig):
    w = oc_rig
    proc = w.launch(free="")
    assert wait_until(lambda: w.events("window"), 20.0), proc.text()
    assert "model" not in w.events("serve")[-1]["config"]
    assert oc.NO_FREE_MODEL in proc.text()
    assert proc.wait(20.0) == 0


def test_a_lost_server_closes_the_window_and_it_reopens_on_the_next_one(oc_rig):
    w = oc_rig
    proc = w.launch(window="alt")
    assert wait_until(lambda: w.events("window"), 20.0), proc.text()
    first = w.events("serve")[-1]
    os.kill(first["pid"], signal.SIGKILL)
    assert wait_until(lambda: len(w.events("window")) == 2, 30.0), proc.text()
    second = w.events("serve")[-1]
    assert second["pid"] != first["pid"]
    assert w.events("window")[-1]["url"] == second["url"]
    windows = w.events("window")
    assert windows[1]["argv"][-1] == windows[0]["argv"][-1], \
        "the reopened window's -s names the same session as the first"
    assert windows[0]["argv"][-2] == "-s"
    assert "the OpenCode server was lost" in proc.text()
    assert bound(w.rig), "the identity stayed online through it"
    proc.signal(signal.SIGTERM)
    assert proc.wait(20.0) == 0


def test_a_stale_session_file_is_ignored_and_the_window_attaches_to_the_new_session(oc_rig):
    w = oc_rig
    stale = "ses_ef0000000000000000000000000"
    oc.update_record(opencode_roster.session_path(IDENTITY), session_id=stale)
    proc = w.launch()
    opened = wait_until(lambda: w.events("window"), 20.0)
    assert opened, proc.text()
    session = oc.read_record(opencode_roster.session_path(IDENTITY))["session_id"]
    assert session != stale
    assert opened[0]["argv"][-2:] == ["-s", session]
    assert stale not in proc.text()
    assert proc.wait(20.0) == 0


def test_config_names_the_comms_config_file_this_launch_reads_its_roster_from(
        oc_rig, tmp_path):
    """`--config PATH`: the candidate file's [opencode] table is this launch's roster."""
    w = oc_rig
    candidate = tmp_path / "candidate.toml"
    candidate.write_text(f'[opencode.agents."{IDENTITY}"]\nslot = 6\n'
                         f'cwd = "{tmp_path / "work"}"\n')
    proc = w.launch(extra=("--dry-run", "--config", str(candidate)))
    assert proc.wait(20.0) == 0
    out = proc.text()
    assert f"identity   {IDENTITY} (slot 6, pinned by the roster)" in out
    assert f"roster     {candidate}" in out
    assert w.events() == []


def test_dry_run_prints_the_plan_and_spawns_nothing(oc_rig):
    w = oc_rig
    proc = w.launch(extra=("--dry-run",))
    assert proc.wait(20.0) == 0
    out = proc.text()
    assert f"identity   {IDENTITY} (slot 5, pinned by the roster)" in out
    assert (f"{', then '.join(opencode_roster.DEFAULT_MODELS)} (the launch default"
            in out), "a row with no model gets the shipped list"
    assert "models --refresh" in out and "before the server starts" in out
    assert "attach <url> -s <session>" in out
    assert w.events() == []
    assert json.dumps(out)
