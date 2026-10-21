"""`moxi-comms-codex` over turns: real daemon, real sockets, a stand-in turns session.

The daemon and its socket are the ordinary rig; the session is `fake_turns.FakeSession`,
shaped as turns' public `Session`, whose turns the test starts and ends as turns' Codex
host would report them. `test_codex_turns_host.py` runs the launcher over turns' real
host. Nothing here spawns codex and nothing here touches the network.
"""

from __future__ import annotations

import itertools

import pytest

from conftest import clock_reads_warm, cursor_of, wait_until
from moxi_comms import codex_threads, owed
from moxi_comms.adapters.codex import CodexAdapter, layer_one_hash, scrub_own_environ
from moxi_comms.adapters.codex_roster import CodexAgent
from moxi_comms.adapters.session import INHERITED_COMMS_ENV
from moxi_comms.claimhold import CLAIM_SOCKET_ENV

from fake_turns import FakeSession, launcher as fake_launcher


@pytest.fixture(autouse=True)
def _gate_reads_warm(monkeypatch):
    """Past the daemon's cold-cache gate, on a clock that reads warm: this suite tests
    the adapter's own mechanics."""
    clock_reads_warm(monkeypatch, "codex")


IDENTITY = "planner@proj-a"


@pytest.fixture
def launch(rig, tmp_path):
    """One launcher for `planner@proj-a` over a fresh `FakeSession`."""
    made: list = []

    def build(*, name="planner", scope="proj-a", slot=3, directors=(), start=True,
              session=None, autoreconnect=False, backoff=None, window=False,
              sandbox="workspace-write", permissions=None, profile=None,
              writable_roots=(), network_access=True, model=None):
        qualified = f"{name}@{scope}" if scope != "global" else name
        agent = CodexAgent(qualified=qualified, name=name, scope=scope, slot=slot,
                           cwd=str(tmp_path), thread_file=codex_threads.path(qualified),
                           sandbox=sandbox, permissions=permissions,
                           profile=dict(profile or {}), writable_roots=list(writable_roots),
                           network_access=network_access, model=model,
                           directors=list(directors))
        session = session or FakeSession()
        exits: list = []
        adapter = CodexAdapter(agent, launch=fake_launcher(session), window=window,
                               autoreconnect=autoreconnect, backoff=backoff,
                               clock=rig.clock, started_wait=0.2,
                               status_sink=lambda text: adapter.statuses.append(text),
                               exit_hook=lambda: exits.append(True))
        adapter.statuses = []
        adapter.exits = exits
        adapter.fake = session
        made.append(adapter)
        if start:
            adapter.start()
        return adapter

    yield build
    for adapter in made:
        adapter.stop()


def send(rig, text, *, to=IDENTITY, slot=1, name="main", scope="proj-a"):
    code, _ = rig.cli(["send", "--posture", "none", to, text], slot=slot, name=name,
                      scope=scope)
    assert code == 0


def delivered(session, count=1):
    return wait_until(lambda: len(session.delivered) >= count and session.delivered)


# --------------------------------------------------------------------------- the launch

def test_it_registers_with_the_pinned_slot_and_the_codex_kind(rig, launch):
    adapter = launch()
    binding = rig.daemon.bindings[IDENTITY]
    assert (binding.adapter, binding.slot_id, binding.tier) == ("codex", 3, "chosen")
    assert adapter.identity == IDENTITY


def test_the_launchers_death_is_its_deregistration(rig, launch):
    adapter = launch()
    adapter.stop()
    assert wait_until(lambda: IDENTITY not in rig.daemon.bindings)


def test_the_session_is_launched_through_turns_with_stamp_layer_one_and_options(
        rig, launch, tmp_path):
    """comms hands turns the identity, layer one and roster-owned thread settings."""
    extra = tmp_path / "extra"
    adapter = launch(window=True, writable_roots=[extra], network_access=False,
                     model="sample-model")
    (call,) = adapter._launch.calls
    assert call["cwd"] == str(tmp_path) and call["window"] is True
    env = call["env"]
    assert (env["MOXI_COMMS_NAME"], env["MOXI_COMMS_SLOT"], env["MOXI_COMMS_SCOPE"]) == \
        ("planner", "3", "proj-a")
    assert env[CLAIM_SOCKET_ENV] == str(adapter.claims.socket_path)
    assert "MOXI_COMMS_DIRECTORS" not in env
    assert "moxi comms" in call["instructions"]
    assert call["options"] == {
        "sandbox": "workspace-write",
        "config": {
            "agents": {"enabled": False},
            "sandbox_workspace_write": {
                "writable_roots": [str(extra), str(adapter.write_root)],
                "network_access": False,
            },
        },
        "model": "sample-model",
    }
    assert not {"cwd", "developerInstructions", "approvalPolicy"} & call["options"].keys()


def test_the_launch_verifier_checks_each_load_answer(rig, launch, tmp_path):
    adapter = launch(writable_roots=[tmp_path / "extra"], network_access=True)
    verify = adapter._launch.calls[0]["verify"]
    accepted = {"cwd": str(tmp_path), "sandbox": {
        "type": "workspaceWrite", "networkAccess": True,
        "writableRoots": [str(tmp_path / "extra"), str(adapter.write_root)],
    }}
    assert verify(accepted) is None
    rejected = {**accepted, "sandbox": {**accepted["sandbox"], "networkAccess": False}}
    assert verify(rejected) == "network access is off, not on"


def test_the_thread_record_names_the_launch_and_its_layer_one(rig, launch):
    adapter = launch()
    record = codex_threads.read_meta(IDENTITY)
    assert record["thread_id"] == "thread-1" == adapter.thread_id
    assert record["launch"] == adapter.launch
    assert record["layer_one"] == layer_one_hash(adapter.comms_path)


def test_a_thread_named_after_the_launch_is_adopted_from_the_first_event(rig, launch):
    """A no-window launch whose first load failed returns with no thread id. The first
    event that names the thread gives comms the thread record and the daemon its id."""
    session = FakeSession(session_id=None)
    adapter = launch(session=session)
    assert adapter.thread_id is None and codex_threads.read(IDENTITY) is None
    assert "session_id" not in adapter.session_report(register=False)
    assert rig.daemon.bindings[IDENTITY].session_id is None
    session.session_id = "thread-9"
    session.ready("startup")
    session.start_turn()
    session.end_turn("answer")
    assert wait_until(lambda: adapter.session_report(register=False).get("turned"))
    assert adapter.thread_id == "thread-9"
    assert codex_threads.read(IDENTITY) == "thread-9"
    record = codex_threads.read_meta(IDENTITY)
    assert record["launch"] == adapter.launch
    assert record["layer_one"] == layer_one_hash(adapter.comms_path)
    assert adapter.session_report(register=False)["session_id"] == "thread-9"
    assert wait_until(lambda: rig.daemon.bindings[IDENTITY].session_id == "thread-9")


def test_a_later_event_naming_another_thread_does_not_replace_the_launchs(rig, launch):
    adapter = launch()
    adapter.fake.session_id = "thread-other"
    adapter.fake.ready("compact")
    adapter.fake.start_turn()
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: adapter.session_report(register=False)["turned"])
    assert adapter.thread_id == "thread-1" == codex_threads.read(IDENTITY)


def test_the_launch_no_longer_warns_that_roster_options_are_not_applied(rig, launch):
    adapter = launch()
    assert not any("NOT applied" in s for s in adapter.statuses), adapter.statuses


def test_the_holder_is_taken_at_the_launch(rig, launch):
    adapter = launch()
    assert adapter.fake.holder is not None
    adapter.stop()
    assert adapter.fake.holder.closed


def test_the_launchers_environment_drops_a_stamp_the_identity_does_not_carry():
    environ = {var: "inherited" for var in INHERITED_COMMS_ENV}
    environ["PATH"] = "/bin"
    scrub_own_environ({"MOXI_COMMS_NAME": "planner"}, environ)
    assert "MOXI_COMMS_DIRECTORS" not in environ
    assert environ["MOXI_COMMS_NAME"] == "inherited", "the stamp itself overrides it"
    assert environ["PATH"] == "/bin"


# --------------------------------------------------------------------------- delivery

def test_a_message_goes_in_steer_first_and_settles_handled_at_its_turns_end(rig, launch):
    adapter = launch()
    send(rig, "hello planner")
    (did, text, steer) = delivered(adapter.fake)[0]
    assert steer is True and "hello planner" in text
    assert wait_until(lambda: adapter.held_ids)
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: not adapter.held_ids)
    assert wait_until(lambda: cursor_of(IDENTITY) is not None)


def test_a_digest_into_a_running_turn_is_steered_into_it(rig, launch):
    session = FakeSession(turn_open=True)
    adapter = launch(session=session)
    send(rig, "while you work")
    rows = wait_until(lambda: len(session.turn_deliveries.get("turn-1", ())) >= 1
                      and session.delivered)
    assert all(steer for _, _, steer in rows)
    assert wait_until(lambda: adapter.held_ids)
    session.end_turn("answer")
    assert wait_until(lambda: not adapter.held_ids)


def test_an_aborted_turn_leaves_its_digest_unhandled_and_the_cursor_unmoved(rig, launch):
    adapter = launch()
    send(rig, "will be interrupted")
    delivered(adapter.fake)
    assert wait_until(lambda: adapter.held_ids)
    adapter.fake.end_turn("aborted")
    assert wait_until(lambda: not adapter.held_ids)
    assert cursor_of(IDENTITY) is None


def test_a_refused_delivery_is_put_back_and_handed_again(rig, launch, monkeypatch):
    adapter = launch()
    monkeypatch.setattr(adapter, "retry_delay", lambda: 0.05)
    adapter.fake.answer = "refuse"
    send(rig, "try again")
    delivered(adapter.fake)
    assert adapter.queue or adapter.held_ids
    adapter.fake.answer = "start"
    rows = delivered(adapter.fake, 2)
    assert rows[-1][2] is True
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: not adapter.held_ids)


def test_a_lost_delivery_is_unhandled_on_the_next_turn_frame(rig, launch):
    adapter = launch()
    adapter.fake.answer = "lose"
    send(rig, "into the void")
    delivered(adapter.fake)
    assert wait_until(lambda: adapter.unhandled_owed)
    adapter.fake.answer = "start"
    adapter.fake.start_turn()
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: not adapter.unhandled_owed and not adapter.held_ids)


def test_a_delivery_with_no_answer_in_time_is_unhandled_too(rig, launch):
    adapter = launch()
    adapter.fake.answer = "hang"
    send(rig, "no answer")
    delivered(adapter.fake)
    assert wait_until(lambda: adapter.unhandled_owed)


def test_a_turn_that_ends_before_its_delivery_is_recorded_still_settles(rig, launch):
    adapter = launch()
    adapter.started_wait = 0.2
    adapter._turn_ended("turn-x", "answer", ["d1"])
    send(rig, "fast turn")
    delivered(adapter.fake)
    assert wait_until(lambda: not adapter.held_ids)


def test_held_mail_waits_for_the_running_turns_end(rig, launch):
    session = FakeSession(turn_open=True)
    adapter = launch(session=session)
    with adapter._lock:
        adapter.queue.append({"message_id": "999", "held": True})
    assert not adapter.may_take_held()
    session.end_turn("answer")
    assert wait_until(lambda: adapter.may_take_held())


# --------------------------------------------------------------------------- the session's events

def test_a_resume_and_a_compaction_are_reported_ready(rig, launch, monkeypatch):
    adapter = launch()
    seen: list = []
    monkeypatch.setattr(adapter, "report_ready", seen.append)
    adapter.fake.ready("resume")
    adapter.fake.ready("compact")
    adapter.fake.ready("startup")
    assert wait_until(lambda: seen == ["resume", "compact"])


def test_the_first_turn_end_sets_turned(rig, launch):
    adapter = launch()
    assert adapter.session_report(register=False)["turned"] is False
    adapter.fake.start_turn()
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: adapter.session_report(register=False)["turned"])


def test_a_register_before_the_launch_reports_a_new_session(rig, launch):
    adapter = launch(start=False)
    assert adapter.session_report(register=True)["new_session"] is True


def test_the_session_going_away_ends_the_launch(rig, launch):
    adapter = launch()
    send(rig, "open when it went")
    delivered(adapter.fake)
    adapter.fake.gone()
    assert wait_until(lambda: adapter.exits)
    assert adapter.gone_reason.startswith("the Codex window closed")
    assert wait_until(lambda: not adapter.held_ids)
    assert not adapter.can_inject()


# --------------------------------------------------------------------------- the holder

def test_a_stop_with_nothing_owed_lets_the_turn_end(rig, launch):
    adapter = launch()
    adapter.fake.start_turn()
    assert adapter.fake.stop(stop_hook_active=False, task_types=(), crons=0) is None


def test_a_stop_that_owes_a_reply_holds_the_turn_with_the_reminder(rig, launch,
                                                                    monkeypatch):
    adapter = launch()
    asked: list = []

    def check(fields):
        asked.append(fields)
        return owed.Decision(block="reply to main", names=("main",))

    monkeypatch.setattr(adapter, "reply_check", check)
    turn = adapter.fake.start_turn()
    assert adapter.fake.stop(stop_hook_active=False, task_types=("background-terminal",),
                             crons=0) == "reply to main"
    assert asked == [{"stop_hook_active": False, "task_types": ["background-terminal"],
                      "crons": 0}]
    assert adapter.blocked and adapter._blocked_turn == turn
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: not adapter.blocked)


def test_a_new_turn_resolves_the_reminder(rig, launch, monkeypatch):
    adapter = launch()
    monkeypatch.setattr(adapter, "reply_check",
                        lambda fields: owed.Decision(block="reply", names=("main",)))
    adapter.fake.start_turn()
    adapter.fake.stop(stop_hook_active=False, task_types=(), crons=0)
    assert adapter.blocked
    adapter.fake.start_turn()
    assert wait_until(lambda: not adapter.blocked)


def test_a_launch_into_a_held_name_waits_for_it(rig, launch):
    first = launch()
    second = launch(autoreconnect=True, backoff=itertools.repeat(0.05))
    assert second.identity is None and second.claims.state == "waiting"
    assert first.identity == IDENTITY


# --------------------------------------------------------------------------- the launcher's exit

@pytest.fixture
def launcher_main(monkeypatch, tmp_path):
    """`codex.main` up to the launch, with a launch that raises what the test says: no
    terminal, signal handler, environment scrub or logging setup of this process is
    touched."""
    import logging
    import signal

    from moxi_comms.adapters import codex, terminal

    config = tmp_path / "roster.toml"
    config.write_text(f'[codex.agents."{IDENTITY}"]\nslot = 3\ncwd = "{tmp_path}"\n')
    monkeypatch.setattr(terminal, "in_terminal_foreground", lambda fd: True)
    monkeypatch.setattr(terminal, "terminal_fd", lambda: 0)
    monkeypatch.setattr(codex, "own_comms", lambda: "/env/bin/moxi comms")
    monkeypatch.setattr(codex, "scrub_own_environ", lambda *a, **k: None)
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(logging, "basicConfig", lambda **k: None)

    def run(error, *flags):
        def start(self, *a, **k):
            raise error
        monkeypatch.setattr(codex.CodexAdapter, "start", start)
        return codex.main([IDENTITY, "--config", str(config), *flags])

    return run


def test_a_window_launch_that_turns_gives_up_on_exits_3_naming_the_log(
        launcher_main, capsys):
    code = launcher_main(RuntimeError("the Codex window did not load within 180 s."),
                         "--window")
    err = capsys.readouterr().err
    assert code == 3, err
    assert (f"moxi-comms-codex: gave up on {IDENTITY}: the Codex window did not load "
            f"within 180 s. The log is ") in err
    assert err.rstrip().endswith(".log."), err


def test_untrusted_hooks_and_a_launch_with_no_window_still_exit_2(launcher_main, capsys):
    class HooksUntrusted(RuntimeError):
        pass

    assert launcher_main(HooksUntrusted("Codex does not trust the hooks"), "--window") == 2
    assert "was not launched: Codex does not trust the hooks" in capsys.readouterr().err
    assert launcher_main(RuntimeError("no codex on PATH")) == 2
    assert "was not launched: no codex on PATH" in capsys.readouterr().err
