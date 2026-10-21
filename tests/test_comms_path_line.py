"""Every session comms launches is told in its layer one which comms to run, by absolute
path, in the same words on every harness.

Codex and OpenCode write layer one themselves and always carried the line. A Claude
session gets layer one from comms' listener: the launcher stamps its own comms command
into the session's environment (`MOXI_COMMS_COMMAND`) and the listener renders the line
from it. A session an older launcher started carries no stamp and gets the listener's
own install; with neither, the line is left out.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from conftest import wait_until

import moxi_turns
from moxi_comms import context, listener
from moxi_comms.adapters import codex, session, shim_roster
from moxi_comms.adapters import opencode as oc
from moxi_comms.context import CONTEXT_BLOCK

COMMAND = "/x/bin/moxi comms"
LINE = ("Run every comms verb with /x/bin/moxi comms (this agent's own comms install). "
        "A bare `moxi comms` on your shell's PATH may be a different install.")


def event(name, *, env=None):
    return moxi_turns.SessionEvent(event=name, harness="claude", session_id="s-1",
                                   env=dict(env or {}))


def session_env(name="reviewer", sid="pl1", **extra) -> dict:
    """A launched session's environment: the stamps and its claim socket."""
    s = session.Session(sid, name=name, scope="proj-a", project="proj-a")
    s.write()
    return dict(s.env(), **extra)


# ------------------------------------------------------------------- the listener

def test_the_listener_names_the_stamped_comms_in_a_claude_sessions_layer_one(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    text = listener.answer(event("ready", env=session_env(MOXI_COMMS_COMMAND=COMMAND)))
    assert "Run every comms verb with /x/bin/moxi comms" in text
    block, line, roster = text.split("\n\n", 2)
    assert (block, line) == (CONTEXT_BLOCK, LINE), "the block, then the path line"
    assert roster.startswith(shim_roster.LABEL) and "planner@proj-a" in roster


def test_the_offline_branch_carries_the_line_too(rig, capsys):
    rig.daemon.stop()
    text = listener.answer(event("ready", env=session_env(MOXI_COMMS_COMMAND=COMMAND)))
    assert text == f"{CONTEXT_BLOCK}\n\n{LINE}\n\n{shim_roster.OFFLINE}"
    capsys.readouterr()


def test_the_line_follows_the_director_sentence(rig, capsys):
    rig.daemon.stop()
    env = session_env(MOXI_COMMS_COMMAND=COMMAND, MOXI_COMMS_DIRECTORS="lead")
    text = listener.answer(event("ready", env=env))
    assert text == (f"{context.layer_one_block(['lead'])}\n\n{LINE}\n\n"
                    f"{shim_roster.OFFLINE}")
    capsys.readouterr()


def test_with_no_stamp_the_line_names_the_listeners_own_install(rig, capsys, monkeypatch):
    """A session an older launcher started: the fallback is this process's comms."""
    monkeypatch.setattr(shim_roster, "own_comms", lambda: "/endpoint/bin/moxi comms")
    rig.daemon.stop()
    env = session_env()
    env.pop("MOXI_COMMS_COMMAND", None)
    text = listener.answer(event("ready", env=env))
    assert "Run every comms verb with /endpoint/bin/moxi comms (" in text
    capsys.readouterr()


def test_with_neither_the_line_names_the_bare_verb(rig, capsys, monkeypatch):
    """No stamp and no moxi script: the line names `moxi comms`, as Codex's and OpenCode's do."""
    monkeypatch.setattr(shim_roster, "own_comms", lambda: None)
    rig.daemon.stop()
    env = session_env()
    env.pop("MOXI_COMMS_COMMAND", None)
    text = listener.answer(event("ready", env=env))
    bare = context.MOXI_COMMS_PATH_LINE.format(comms=context.BARE_COMMS)
    assert text == f"{CONTEXT_BLOCK}\n\n{bare}\n\n{shim_roster.OFFLINE}"
    capsys.readouterr()


def test_prompt_deltas_carry_no_path_line(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    env = session_env(MOXI_COMMS_COMMAND=COMMAND)
    listener.answer(event("ready", env=env))
    rig.shim(slot=3, name="curator", scope="proj-a")
    delta = listener.answer(event("prompt", env=env))
    assert "curator@proj-a" in delta and "Run every comms verb" not in delta


# ------------------------------------------------------------------- one copy

def test_the_line_and_own_comms_are_held_once_in_context():
    assert codex.MOXI_COMMS_PATH_LINE is context.MOXI_COMMS_PATH_LINE
    assert oc.MOXI_COMMS_PATH_LINE is context.MOXI_COMMS_PATH_LINE
    assert codex.BARE_COMMS == oc.BARE_COMMS == context.BARE_COMMS == "moxi comms"
    assert codex.own_comms is oc.own_comms is context.own_comms
    assert context.MOXI_COMMS_PATH_LINE.format(comms=COMMAND) == LINE


def test_codex_and_opencode_layer_one_and_its_hash_are_unchanged():
    """Pinned against the strings before the line moved: a Codex thread's recorded hash
    still matches, and neither harness's text changes."""
    for module in (codex, oc):
        assert module.layer_one_text(None, COMMAND) == \
            f"{CONTEXT_BLOCK}\n\n{LINE}\n\n{shim_roster.OFFLINE}"
    assert codex.layer_one_hash(COMMAND) == "47971003ac4493c6"
    assert codex.layer_one_hash(COMMAND, directors=("lead",)) == "42df5102e69bdda2"
    assert codex.layer_one_hash() == "2a8e3a7162ca0ea8"


# ------------------------------------------------------------------- the launcher's stamp

def test_stamped_env_scrubs_an_inherited_comms_command():
    env = session.stamped_env({"MOXI_COMMS_COMMAND": "/other/bin/moxi comms",
                               "PATH": "/bin"}, name="x")
    assert "MOXI_COMMS_COMMAND" not in env
    assert "MOXI_COMMS_COMMAND" in session.INHERITED_COMMS_ENV


def test_a_session_carries_its_launchers_command_and_never_an_inherited_one():
    s = session.Session("pl2", name="x", project="proj-a", comms=COMMAND)
    env = s.env({"MOXI_COMMS_COMMAND": "/other/bin/moxi comms"})
    assert env["MOXI_COMMS_COMMAND"] == COMMAND
    bare = session.Session("pl3", name="x", project="proj-a")
    assert "MOXI_COMMS_COMMAND" not in bare.env({"MOXI_COMMS_COMMAND": "/other"})


@pytest.fixture
def fake_claude(tmp_path):
    out = tmp_path / "claude.out"
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "command $MOXI_COMMS_COMMAND" >> {out}
        """))
    script.chmod(0o755)
    return script, out


def test_the_claude_launcher_stamps_its_own_comms_command(rig, fake_claude):
    script, out = fake_claude
    code = ("from moxi_comms.adapters import session; raise SystemExit(session.main("
            f"['--name', 'stamped', '--claude', {str(script)!r}]))")
    env = dict(os.environ, MOXI_COMMS_COMMAND="/inherited/bin/moxi comms")
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert wait_until(lambda: out.exists(), timeout=10)
    expected = context.own_comms()
    assert expected and expected.endswith("/moxi comms")
    assert out.read_text().splitlines() == [f"command {expected}"]
