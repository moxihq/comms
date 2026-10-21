"""Reply reminders on Codex through turns, and on OpenCode's existing delivery."""

from __future__ import annotations

import itertools

import pytest
from conftest import clock_reads_warm, frame_kinds, wait_until

from moxi_comms import codex_threads, owed
from moxi_comms.adapters import opencode as oc
from moxi_comms.adapters import opencode_roster
from moxi_comms.adapters.codex import CodexAdapter
from moxi_comms.adapters.codex_roster import CodexAgent
from moxi_comms.adapters.opencode import OpenCodeAdapter
from moxi_comms.adapters.opencode_roster import OpenCodeAgent
from fake_opencode import FakeOpenCode
from fake_turns import FakeSession, launcher

CODEX = "planner@proj-a"
OPENCODE = "coder@proj-a"
PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _gate_reads_warm(monkeypatch):
    clock_reads_warm(monkeypatch, "codex", "opencode")


def injected(rig, identity, msg) -> None:
    assert wait_until(lambda: rig.daemon.injected_through.get(identity) == msg.id)


def unanswered(rig):
    with rig.daemon._lock:
        return {key[1]: dict(value) for key, value in rig.daemon.unanswered.items()}


@pytest.fixture
def codex(rig, tmp_path):
    session = FakeSession()
    row = CodexAgent(qualified=CODEX, name="planner", scope="proj-a", slot=3,
                     cwd=str(tmp_path), thread_file=codex_threads.path(CODEX))
    adapter = CodexAdapter(row, launch=launcher(session), autoreconnect=False,
                           clock=rig.clock, started_wait=0.2)
    adapter.fake = session
    adapter.start()
    yield adapter
    adapter.stop()


def codex_owes(rig, adapter, text="ship it"):
    msg = rig.post_as_operator([CODEX], text)
    assert wait_until(lambda: msg.id in adapter.reply_ledger.debts)
    assert wait_until(lambda: adapter.fake.open_turn is not None)
    injected(rig, CODEX, msg)
    return msg


def test_an_owing_stop_with_no_work_is_held_by_turns_holder(rig, codex):
    msg = codex_owes(rig, codex)
    debt = codex.reply_ledger.debts[msg.id]
    answer = codex.fake.stop(stop_hook_active=False, task_types=(), crons=0)
    assert answer == owed.reminder_text([debt], rig.clock())
    assert codex.blocked and debt.pending


def test_the_next_stop_counts_the_reminder_and_the_turn_end_reports_unanswered(
        rig, codex, frames):
    msg = codex_owes(rig, codex)
    codex.fake.stop(stop_hook_active=False, task_types=(), crons=0)
    assert codex.fake.stop(stop_hook_active=True, task_types=(), crons=0) is None
    codex.fake.end_turn("answer")
    assert wait_until(lambda: msg.id in unanswered(rig))
    assert unanswered(rig)[msg.id]["owing"] == CODEX
    assert codex.reply_ledger.debts == {} and not codex.blocked
    assert frame_kinds(frames, "turn")[-1]["handled"] == [msg.id]


def test_background_work_lets_the_stop_pass_without_a_reminder(rig, codex):
    msg = codex_owes(rig, codex)
    assert codex.fake.stop(stop_hook_active=False,
                           task_types=("background-terminal",), crons=0) is None
    codex.fake.end_turn("answer")
    assert wait_until(lambda: msg.id in codex.reply_ledger.debts)
    assert not codex.blocked and unanswered(rig) == {}


def test_a_new_turn_voids_a_pending_reminder(rig, codex):
    msg = codex_owes(rig, codex)
    codex.fake.stop(stop_hook_active=False, task_types=(), crons=0)
    assert codex.blocked
    codex.fake.start_turn()
    assert wait_until(lambda: not codex.blocked)
    debt = codex.reply_ledger.debts[msg.id]
    assert not debt.pending and not debt.counted


def test_an_interrupted_turn_voids_the_reminder_and_marks_the_digest_unhandled(
        rig, codex, frames):
    msg = codex_owes(rig, codex)
    codex.fake.stop(stop_hook_active=False, task_types=(), crons=0)
    codex.fake.end_turn("aborted")
    assert wait_until(lambda: not codex.blocked)
    debt = codex.reply_ledger.debts[msg.id]
    assert not debt.pending and not debt.counted
    assert frame_kinds(frames, "turn")[-1]["unhandled"] == [msg.id]
    assert unanswered(rig) == {}


# OpenCode keeps comms' own delivery until B8.

class Handle:
    def __init__(self, fake):
        self.fake = fake
        self.alive = True
        self.returncode = None

    def wait_listening(self, timeout=None):
        return self.fake.url

    def stop(self):
        pass


@pytest.fixture
def fake():
    server = FakeOpenCode(password=PASSWORD, default_model="opencode/minimax-m3.1-flash-free",
                          heartbeat=0.5, auto=False).start()
    yield server
    server.stop()


@pytest.fixture
def opencode(rig, fake, tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "FLUSH_RETRY", 0.05)
    agent = OpenCodeAgent(qualified=OPENCODE, name="coder", scope="proj-a", slot=5,
                          cwd=str(tmp_path),
                          session_file=opencode_roster.session_path(OPENCODE),
                          models=["opencode/minimax-m3.1-flash-free"])
    adapter = OpenCodeAdapter(agent, spawn=False, server=Handle(fake), password=PASSWORD,
                              autoreconnect=True, backoff=itertools.repeat(0.05),
                              clock=rig.clock)
    fake.on_log = adapter.on_log_line
    adapter.start()
    yield adapter
    adapter.stop()


def texts(fake):
    return [" ".join(part.get("text", "") for part in (body or {}).get("parts") or [])
            for method, path, body in fake.requests
            if method == "POST" and path.endswith("/prompt_async")]


def opencode_owes(rig, adapter, fake, text="ship it"):
    msg = rig.post_as_operator([OPENCODE], text)
    assert wait_until(lambda: msg.id in adapter.reply_ledger.debts)
    assert wait_until(lambda: adapter.busy)
    injected(rig, OPENCODE, msg)
    return msg


def reminders(fake):
    return [text for text in texts(fake) if text.startswith("comms: you are ending this turn")]


def test_an_owing_turn_end_with_no_work_posts_the_reminder_as_the_adapters_line(
        rig, opencode, fake, frames):
    msg = opencode_owes(rig, opencode, fake)
    sid = opencode.session_id
    fake.finish(sid)
    assert wait_until(lambda: reminders(fake)), texts(fake)
    debt = opencode.reply_ledger.debts[msg.id]
    assert reminders(fake) == [owed.reminder_text([debt], rig.clock())]
    assert opencode.blocked and opencode.turn_blocks == 1 and debt.pending
    assert frame_kinds(frames, "turn")[-1]["handled"] == [msg.id]
    assert wait_until(lambda: opencode.busy)
    assert opencode.pending == {}


def test_the_reminder_turns_end_counts_it_and_unanswered_is_sent(
        rig, opencode, fake, frames):
    msg = opencode_owes(rig, opencode, fake)
    sid = opencode.session_id
    fake.finish(sid)
    assert wait_until(lambda: opencode.blocked and opencode.busy)
    turns = len(frame_kinds(frames, "turn"))
    fake.finish(sid)
    assert wait_until(lambda: msg.id in unanswered(rig))
    assert unanswered(rig)[msg.id]["owing"] == OPENCODE
    assert wait_until(lambda: not opencode.blocked)
    assert opencode.reply_ledger.debts == {} and opencode.turn_blocks == 0
    assert len(reminders(fake)) == 1
    ended = frame_kinds(frames, "turn")
    assert len(ended) == turns + 1
    assert ended[-1]["handled"] == [] and ended[-1]["unhandled"] == []


def test_another_turn_end_before_the_reminders_voids_it(rig, opencode, fake, monkeypatch):
    msg = opencode_owes(rig, opencode, fake)
    sid = opencode.session_id
    real = opencode.deliver_line
    taken = []

    def lost(line, *, message_id=None):
        taken.append(line)
        return True

    monkeypatch.setattr(opencode, "deliver_line", lost)
    fake.finish(sid)
    assert wait_until(lambda: opencode.blocked)
    assert len(taken) == 1
    monkeypatch.setattr(opencode, "deliver_line", real)
    fake.control("type", {"session": sid, "text": "his own prompt"})
    assert wait_until(lambda: opencode.busy)
    fake.finish(sid)
    assert wait_until(lambda: reminders(fake))
    assert not opencode.reply_ledger.debts[msg.id].counted
    assert unanswered(rig) == {}


def test_a_window_clear_empties_the_debts_and_ends_the_reminder(rig, opencode, fake):
    opencode_owes(rig, opencode, fake)
    sid = opencode.session_id
    fake.finish(sid)
    assert wait_until(lambda: opencode.blocked)
    fake.control("new-session", {})
    assert wait_until(lambda: opencode.session_id != sid)
    assert opencode.reply_ledger.debts == {} and not opencode.blocked
    assert unanswered(rig) == {}
