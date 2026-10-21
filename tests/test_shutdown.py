"""A systemd stop is a clean stop, and a restart's replay is not shown twice.

Two failures this pins: the daemon dying on SIGTERM before `Daemon.stop()` ran, and the
shim queueing a replayed message it already held.
"""

import os
import signal
import subprocess
import sys
import time

import pytest
from conftest import wait_for_queue

from moxi_comms.paths import daemon_socket


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_runs_the_clean_shutdown(tmp_path, sig):
    proc = subprocess.Popen(
        [sys.executable, "-c", "from moxi_comms.daemon import main; raise SystemExit(main())",
         "--gateway", "mock", "--gateway-store", str(tmp_path / "store.json")],
        env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sock = daemon_socket()
    deadline = time.time() + 20
    while not sock.exists():
        assert proc.poll() is None, proc.stdout.read()
        assert time.time() < deadline, "the daemon never bound its socket"
        time.sleep(0.05)
    proc.send_signal(sig)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    assert not sock.exists(), "stop() removes the socket it bound, so it ran"


def _deliver(shim, message_id):
    shim._on_push({"op": "deliver", "payload": {"message_id": message_id, "text": "x"}})


def test_a_replayed_message_already_queued_is_dropped(rig):
    shim = rig.shim(slot=1, name="planner", scope="proj-a")
    _deliver(shim, "329")
    _deliver(shim, "329")
    assert [p["message_id"] for p in shim.queue] == ["329"]


def _one_real_delivery(rig):
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    code, _ = rig.cli(["send", "--posture", "none", "reviewer@proj-a", "the parser diff"],
                      slot=planner.slot_id, name="planner", scope="proj-a")
    assert code == 0
    wait_for_queue(reviewer, 1)
    return reviewer, dict(reviewer.queue[0])


def test_a_replayed_message_injected_but_not_completed_is_dropped(rig):
    reviewer, payload = _one_real_delivery(rig)
    assert reviewer.on_turn_boundary() is not None      # injected; the turn is running
    reviewer._on_push({"op": "deliver", "payload": payload})   # the restart's replay
    assert reviewer.queue == []
    reviewer.on_turn_boundary()                         # completes; nothing to inject
    assert len(reviewer.digests) == 1


def test_after_completion_the_same_id_is_no_longer_held(rig):
    reviewer, payload = _one_real_delivery(rig)
    reviewer.on_turn_boundary()
    reviewer.on_turn_boundary()
    assert reviewer.held_ids == set()
