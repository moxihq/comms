"""The ``[codex]`` roster after the app-server moved to turns."""

from __future__ import annotations

import textwrap

import pytest
from conftest import write_config

from moxi_comms import codex_threads
from moxi_comms.adapters import codex_roster
from moxi_comms.config import ConfigError


def roster_file(body: str):
    write_config(None, textwrap.dedent(body).replace("[agents.", "[codex.agents."))


GOOD = """
    [agents."planner@proj-a"]
    slot = 3
    cwd = "/work/proj-a"
    writable_roots = ["/write/one"]
"""


def test_a_missing_roster_is_empty():
    assert codex_roster.load().agents == {}


def test_an_entry_carries_identity_slot_and_thread_file():
    roster_file(GOOD)
    row = codex_roster.load().require("planner@proj-a")
    assert (row.slot, row.name, row.scope, row.cwd) == (3, "planner", "proj-a",
                                                       "/work/proj-a")
    assert row.thread_file == codex_threads.path("planner@proj-a")
    assert not hasattr(row, "socket")


def test_socket_is_a_dead_key_and_is_refused():
    roster_file("""
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        socket = "/run/old.sock"
    """)
    with pytest.raises(ConfigError, match="unknown key.*socket"):
        codex_roster.load()


def test_an_identity_not_in_the_roster_is_refused():
    roster_file(GOOD)
    with pytest.raises(ConfigError) as caught:
        codex_roster.load().require("stranger@proj-a")
    assert "never register-triggered" in str(caught.value)
    assert "planner@proj-a" in str(caught.value)


def test_a_token_shaped_key_is_refused():
    roster_file("""
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        openai_api_token = "not-here"
    """)
    with pytest.raises(ConfigError, match="looks like a secret"):
        codex_roster.load()


@pytest.mark.parametrize(("body", "expected"), [
    ('cwd = "/work"', "no slot"),
    ('slot = "next-free"\ncwd = "/work"', "must be a number"),
    ("slot = 3", "no cwd"),
    ('slot = 3\ncwd = "/work"\nsandbox = "workspace-write"\npermissions = "p"',
     "cannot be combined"),
    ('slot = 3\ncwd = "/work"\nsandbox = "wide-open"', "is not one of"),
    ('slot = 3\ncwd = "/work"\nprofile = {network = {enabled = true}}',
     "needs the `permissions`"),
])
def test_roster_refusals(body, expected):
    roster_file(f'[agents."planner@proj-a"]\n{body}\n')
    with pytest.raises(ConfigError) as caught:
        codex_roster.load()
    assert expected in str(caught.value)


def test_agent_names_reserved_for_the_daemon_are_refused():
    roster_file("""
        [agents."agent-3"]
        slot = 3
        cwd = "/work"
    """)
    with pytest.raises(ConfigError, match="reserved"):
        codex_roster.load()


def test_a_row_takes_the_default_sandbox_it_does_not_name():
    roster_file("""
        [codex.defaults]
        sandbox = "read-only"
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        [agents."coder@proj-a"]
        slot = 4
        cwd = "/work"
        sandbox = "workspace-write"
    """)
    roster = codex_roster.load()
    assert roster.require("planner@proj-a").sandbox == "read-only"
    assert roster.require("coder@proj-a").sandbox == "workspace-write"


def test_permissions_replace_the_default_sandbox():
    roster_file("""
        [codex.defaults]
        sandbox = "read-only"
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        permissions = "comms"
    """)
    row = codex_roster.load().require("planner@proj-a")
    assert (row.permissions, row.sandbox) == ("comms", None)


@pytest.mark.parametrize("body", [
    "slot = 3", 'thread_file = "/tmp/t.json"', 'directors = ["main"]',
    'model = "m"', 'cwd = "/work"', "network_access = false",
])
def test_defaults_hold_only_the_sandbox(body):
    roster_file(f"[codex.defaults]\n{body}\n")
    with pytest.raises(ConfigError, match="one identity's own"):
        codex_roster.load()


def test_launch_options_hold_model_sandbox_network_roots_and_agents_switch():
    roster_file("""
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        sandbox = "workspace-write"
        writable_roots = ["/write/one"]
        network_access = false
        model = "gpt-sample"
    """)
    row = codex_roster.load().require("planner@proj-a")
    assert row.launch_options("/write/record") == {
        "sandbox": "workspace-write",
        "model": "gpt-sample",
        "config": {
            "agents": {"enabled": False},
            "sandbox_workspace_write": {
                "writable_roots": ["/write/one", "/write/record"],
                "network_access": False,
            },
        },
    }


def test_network_off_is_honoured_but_said_out_loud(caplog):
    roster_file("""
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        network_access = false
    """)
    row = codex_roster.load().require("planner@proj-a")
    assert row.network_access is False
    assert "cannot run `moxi comms reply`" in caplog.text


def test_declared_directors_reach_the_session_stamp():
    roster_file("""
        [agents."planner@proj-a"]
        slot = 3
        cwd = "/work"
        directors = ["main", "reviewer@proj-a"]
    """)
    env = codex_roster.load().require("planner@proj-a").env({})
    assert env["MOXI_COMMS_DIRECTORS"] == "main,reviewer@proj-a"
