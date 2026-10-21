"""Codex-specific behavior where the turns caller meets ``HarnessAdapter``."""

from __future__ import annotations

import itertools

import pytest
from conftest import clock_reads_warm, wait_until

from moxi_comms import codex_threads
from moxi_comms.adapters.codex import CodexAdapter
from moxi_comms.adapters.codex_roster import CodexAgent

from fake_turns import FakeSession, launcher


IDENTITY = "planner@proj-a"


def make_adapter(rig, tmp_path, session=None, **kwargs):
    row = CodexAgent(qualified=IDENTITY, name="planner", scope="proj-a", slot=3,
                     cwd=str(tmp_path), thread_file=codex_threads.path(IDENTITY))
    fake = session or FakeSession()
    adapter = CodexAdapter(row, launch=launcher(fake), clock=rig.clock,
                           started_wait=0.05, **kwargs)
    adapter.fake = fake
    return adapter


@pytest.fixture(autouse=True)
def _warm(monkeypatch):
    clock_reads_warm(monkeypatch, "codex")


def test_the_adapters_own_stop_closes_the_daemon_quietly(rig, tmp_path):
    statuses = []
    adapter = make_adapter(rig, tmp_path, autoreconnect=False,
                           status_sink=statuses.append)
    adapter.start()
    adapter.stop()
    assert statuses == []
    assert wait_until(lambda: IDENTITY not in rig.daemon.bindings)


def test_a_launch_failure_escapes_before_any_service_thread_starts(rig, tmp_path):
    adapter = make_adapter(rig, tmp_path, autoreconnect=False)

    def fail(*args, **kwargs):
        raise RuntimeError("host did not load")

    adapter._launch = fail
    with pytest.raises(RuntimeError, match="host did not load"):
        adapter.start()
    assert adapter._threads == []
    adapter.stop()


def test_a_refused_turns_delivery_is_requeued_and_retried(rig, tmp_path):
    fake = FakeSession()
    fake.answer = "refuse"
    adapter = make_adapter(rig, tmp_path, fake, autoreconnect=False)
    adapter.retry_delay = lambda: 0.01
    adapter.start()
    try:
        code, _ = rig.cli(["send", "--posture", "none", IDENTITY, "again"],
                          slot=1, name="main", scope="proj-a")
        assert code == 0
        assert wait_until(lambda: fake.delivered)
        fake.answer = "start"
        assert wait_until(lambda: len(fake.delivered) >= 2)
        fake.end_turn("answer")
        assert wait_until(lambda: not adapter.held_ids)
    finally:
        adapter.stop()


@pytest.mark.parametrize("answer", ["lose", "hang"])
def test_a_delivery_turns_cannot_place_is_not_injected_twice(rig, tmp_path, answer):
    fake = FakeSession()
    fake.answer = answer
    adapter = make_adapter(rig, tmp_path, fake, autoreconnect=False)
    adapter.start()
    try:
        code, _ = rig.cli(["send", "--posture", "none", IDENTITY, "once"],
                          slot=1, name="main", scope="proj-a")
        assert code == 0
        assert wait_until(lambda: adapter.unhandled_owed)
        assert len(fake.delivered) == 1
    finally:
        adapter.stop()


def test_the_daemon_supervisor_survives_an_unexpected_error(rig, tmp_path):
    statuses = []
    adapter = make_adapter(rig, tmp_path, autoreconnect=True,
                           backoff=itertools.repeat(0.01), status_sink=statuses.append)
    calls = []

    def connect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        adapter.offline = False

    adapter.connect_daemon = connect
    thread = __import__("threading").Thread(target=adapter._supervise, daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: len(calls) >= 2 and not adapter.offline)
        assert thread.is_alive()
        assert any("RuntimeError: boom" in line for line in statuses)
    finally:
        adapter._stop.set()
        thread.join(2)
