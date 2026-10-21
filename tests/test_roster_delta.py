"""Roster deltas: comms' listener at each prompt, and the session's roster memory.

Joins/leaves ride turns that
happen anyway -- diffed recipient-side against `roster.json` in the session dir,
one parenthetical line, self excluded, every tier included, silent skip when the
daemon is unreachable (the file does NOT advance, so the next successful diff
carries the accumulated truth), and nothing changed -> no context at all.
"""

import json
import os

from conftest import wait_until

import moxi_turns
from moxi_comms import listener
from moxi_comms.adapters import session, shim_roster
from moxi_comms.paths import daemon_socket

#: What the memory holds for a chosen claude-shim name since reply-enforcement:
#: the tier and the adapter (a memory written before holds the bare tier).
CHOSEN_SHIM = {"tier": "chosen", "adapter": "claude-shim"}


def launched(sid="rd1", name="watcher"):
    """A launched session's environment (its stamps and its claim socket) and its roster
    memory file."""
    s = session.Session(sid, name=name, scope="proj-a", project="proj-a")
    s.write()
    env = s.env()
    return env, listener.memory_path(env)


def ask(name, env):
    return listener.answer(moxi_turns.SessionEvent(event=name, harness="claude",
                                                   session_id="s-1", env=dict(env)))


def seed(path, roster):
    """A baseline planted the way the SessionStart seed plants one."""
    assert shim_roster.write_state(path, roster)
    return path


# ------------------------------------------------------------------------- the diff

def test_a_join_is_announced_with_its_tier(rig, tmp_path):
    state = seed(tmp_path / "roster.json", {})
    rig.shim(slot=1, name="reviewer", scope="proj-a")
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online
    assert envelope == ("(comms roster update: reviewer@proj-a [chosen] is now "
                        "reachable — `moxi comms agents` is always current.)")
    assert "\n" not in envelope, "one line"


def test_a_leave_is_just_the_name(rig, tmp_path):
    state = seed(tmp_path / "roster.json", {"agent-3": "ephemeral"})
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online
    assert envelope == ("(comms roster update: agent-3 left — `moxi comms agents` is "
                        "always current.)")
    assert "[ephemeral]" not in envelope, "the tier of someone gone is not news"


def test_join_and_leave_share_one_line(rig, tmp_path):
    state = seed(tmp_path / "roster.json", {"agent-3": "ephemeral"})
    rig.shim(slot=1, name="reviewer", scope="proj-a")
    envelope, _ = shim_roster.delta(daemon_socket(), state, env={})
    assert envelope == ("(comms roster update: reviewer@proj-a [chosen] is now "
                        "reachable; agent-3 left — `moxi comms agents` is always current.)")


def test_self_is_excluded_scoped(rig, tmp_path):
    """The own-binding race: the seed may predate the session's own
    registration, and the first prompt must not announce 'you joined'."""
    state = seed(tmp_path / "roster.json", {})
    rig.shim(slot=1, name="planner", scope="proj-a")
    envelope, online = shim_roster.delta(
        daemon_socket(), state, env={"MOXI_COMMS_NAME": "planner",
                                     "MOXI_COMMS_SCOPE": "proj-a"})
    assert online and envelope is None


def test_self_is_excluded_global(rig, tmp_path):
    """Global identities render bare in the roster; the self filter must match
    the bare qualified form, not `name@global`."""
    state = seed(tmp_path / "roster.json", {})
    rig.shim(slot=1, name="steward", global_=True)
    envelope, online = shim_roster.delta(
        daemon_socket(), state, env={"MOXI_COMMS_NAME": "steward", "MOXI_COMMS_GLOBAL": "1",
                                     "MOXI_COMMS_SCOPE": "global"})
    assert online and envelope is None
    # ...and self leaving its own memory is filtered from the leave side too
    state2 = seed(tmp_path / "r2.json", {"steward": "standing", "other@p": "chosen"})
    envelope, _ = shim_roster.delta(
        daemon_socket(), state2, env={"MOXI_COMMS_NAME": "steward", "MOXI_COMMS_GLOBAL": "1"})
    assert envelope == ("(comms roster update: other@p left — `moxi comms agents` is "
                        "always current.)")


def test_ephemerals_are_announced(rig, tmp_path):
    """Every tier, ephemerals included -- an appearing worker is signal."""
    state = seed(tmp_path / "roster.json", {})
    s = rig.shim(slot=2)
    auto = s.identity
    envelope, _ = shim_roster.delta(daemon_socket(), state, env={})
    assert envelope == (f"(comms roster update: {auto} [ephemeral] is now reachable "
                        "— `moxi comms agents` is always current.)")


def test_no_change_says_nothing(rig, tmp_path):
    rig.shim(slot=1, name="planner", scope="proj-a")
    state = seed(tmp_path / "roster.json", {"planner@proj-a": "chosen"})
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online and envelope is None


def test_slot_change_under_an_unchanged_name_is_not_announced(rig, tmp_path):
    """The name is the identity: the diff is keyed by qualified name alone, so
    slot/adapter churn under an unchanged name cannot even reach it (the memory keeps
    the adapter since reply-enforcement, to render a send-only session's leave)."""
    s = rig.shim(slot=1, name="planner", scope="proj-a")
    state = tmp_path / "roster.json"
    shim_roster.delta(daemon_socket(), state, env={})       # missing file: seeds
    s.stop()
    wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)
    rig.shim(slot=2, name="planner", scope="proj-a")        # same name, new slot
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online and envelope is None


def test_a_delta_carries_no_seat_context(rig, tmp_path):
    """Nothing injected into an agent carries a slot.
    The delta was already clean by design -- a join shows tier, a leave shows the
    name, and the memory is qualified->tier -- and this pins it rather than trusting
    that no one adds a column later. The state file is checked too: it is written
    into the session's runtime dir, so a seat there would be seat context on disk."""
    state = seed(tmp_path / "roster.json", {})
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online
    assert "slot" not in envelope and "seat" not in envelope
    assert "[chosen]" in envelope, "the tier does ride along"
    assert "slot" not in state.read_text()
    assert json.loads(state.read_text()) == {"reviewer@proj-a": CHOSEN_SHIM}


# ------------------------------------------------------------------------- the seed

def test_the_snapshot_seeds_the_state_file_on_success(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    env, state = launched()
    assert ask("ready", env).startswith(shim_roster.BLOCK)
    assert json.loads(state.read_text()) == {"planner@proj-a": CHOSEN_SHIM}


def test_offline_snapshot_seeds_nothing(rig, capsys):
    """'The roster is empty' and 'comms was down' must not be confused in memory:
    an offline envelope leaves NO baseline, so the first delta seeds quietly."""
    rig.daemon.stop()
    env, state = launched()
    assert ask("ready", env) == shim_roster.with_block(
        shim_roster.OFFLINE, comms=shim_roster.comms_command(env))
    capsys.readouterr()
    assert not state.exists()


def test_missing_state_file_seeds_and_announces_nothing(rig, tmp_path):
    """An unknown baseline must not announce the whole world as newly joined."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    state = tmp_path / "roster.json"
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online and envelope is None
    assert json.loads(state.read_text()) == {"planner@proj-a": CHOSEN_SHIM,
                                             "reviewer@proj-a": CHOSEN_SHIM}


def test_a_corrupt_state_file_is_an_unknown_baseline(rig, tmp_path):
    rig.shim(slot=1, name="planner", scope="proj-a")
    state = tmp_path / "roster.json"
    state.write_text("not json{")
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online and envelope is None
    assert json.loads(state.read_text()) == {"planner@proj-a": CHOSEN_SHIM}


# ------------------------------------------------------------ no advance on failure

def test_failed_query_does_not_advance_and_the_next_delta_catches_up(rig, tmp_path):
    """Silent skip, no advance -- at-least-once replayed against a
    roster. The accumulated change arrives on the next successful diff."""
    state = seed(tmp_path / "roster.json", {})
    rig.shim(slot=1, name="reviewer", scope="proj-a")

    envelope, online = shim_roster.delta("/nonexistent/daemon.sock", state, env={})
    assert not online and envelope is None
    assert json.loads(state.read_text()) == {}, "the file did not advance"

    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online
    assert "reviewer@proj-a [chosen] is now reachable" in envelope


def test_failed_advance_emits_nothing(rig, tmp_path, capsys):
    """Advance-then-emit: a delta that could not be remembered is not announced --
    announcing it would repeat every prompt until a write succeeded."""
    import pytest

    state = seed(tmp_path / "roster.json", {})
    rig.shim(slot=1, name="reviewer", scope="proj-a")

    def refuse(src, dst):
        raise OSError("disk says no")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shim_roster.os, "replace", refuse)
        envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert online and envelope is None
    assert "state write failed" in capsys.readouterr().err

    assert json.loads(state.read_text()) == {}, "no advance"
    envelope, _ = shim_roster.delta(daemon_socket(), state, env={})
    assert "reviewer@proj-a [chosen] is now reachable" in envelope


# ----------------------------------------------------------------------- atomicity

def test_state_writes_are_temp_plus_rename_in_the_same_directory(tmp_path,
                                                                 monkeypatch):
    renames = []
    real = os.replace

    def spy(src, dst):
        renames.append((str(src), str(dst)))
        return real(src, dst)
    monkeypatch.setattr(shim_roster.os, "replace", spy)

    state = tmp_path / "roster.json"
    assert shim_roster.write_state(state, {"a": "chosen"})
    (src, dst), = renames
    assert dst == str(state)
    assert os.path.dirname(src) == str(tmp_path), \
        "temp in the SAME directory: rename must stay one-filesystem atomic"
    assert json.loads(state.read_text()) == {"a": "chosen"}


def test_a_failed_write_leaves_the_old_state_and_no_debris(tmp_path, monkeypatch,
                                                           capsys):
    state = seed(tmp_path / "roster.json", {"a": "chosen"})

    def refuse(src, dst):
        raise OSError("disk says no")
    monkeypatch.setattr(shim_roster.os, "replace", refuse)
    assert not shim_roster.write_state(state, {"b": "chosen"})
    assert "state write failed" in capsys.readouterr().err
    assert json.loads(state.read_text()) == {"a": "chosen"}, "no partial file"
    assert os.listdir(tmp_path) == ["roster.json"], "the temp file was cleaned up"


# ---------------------------------------------------------------------- the listener

def test_the_prompt_context_is_exactly_the_delta(rig, tmp_path):
    env, state = launched()
    seed(state, {})
    rig.shim(slot=1, name="reviewer", scope="proj-a")
    expected, _ = shim_roster.delta(daemon_socket(), seed(tmp_path / "twin.json", {}),
                                    env=env)
    assert expected and ask("prompt", env) == expected


def test_no_change_gives_no_context_at_all(rig):
    """None, not an empty text: most prompts get nothing."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    env, state = launched()
    seed(state, {"planner@proj-a": "chosen"})
    assert ask("prompt", env) is None


def test_offline_delta_gives_nothing_and_keeps_the_memory(rig, capsys):
    """No per-prompt offline nag -- the session-start line carries the honesty -- and
    the memory does not advance, so the next good diff carries the change."""
    env, state = launched()
    seed(state, {"planner@proj-a": "chosen"})
    rig.daemon.stop()
    assert ask("prompt", env) is None
    capsys.readouterr()
    assert json.loads(state.read_text()) == {"planner@proj-a": "chosen"}
