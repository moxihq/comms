"""The Codex roster settings handed to turns.

turns owns cwd, developer instructions and approval policy. comms owns the roster and
hands every other Codex thread setting to the host as one unread ``options`` mapping.
The verifier remains comms' read-back of the sandbox reported by each load.
"""

from __future__ import annotations

import os

import pytest

from moxi_comms.adapters.codex_roster import CodexAgent, record_write_root


def agent(tmp_path, **changes):
    values = dict(qualified="pilot@proj-a", name="pilot", scope="proj-a", slot=3,
                  cwd=str(tmp_path), thread_file=tmp_path / "pilot.json",
                  writable_roots=[tmp_path / "row"], model="gpt-sample")
    values.update(changes)
    return CodexAgent(**values)


def test_workspace_options_carry_every_roster_owned_setting(tmp_path):
    added = tmp_path / "record"
    options = agent(tmp_path, network_access=False).launch_options(added)
    assert options == {
        "sandbox": "workspace-write",
        "model": "gpt-sample",
        "config": {
            "agents": {"enabled": False},
            "sandbox_workspace_write": {
                "writable_roots": [str(tmp_path / "row"), str(added)],
                "network_access": False,
            },
        },
    }
    assert not {"cwd", "developerInstructions", "approvalPolicy"} & options.keys()


def test_the_added_root_is_not_listed_twice(tmp_path):
    root = tmp_path / "row"
    options = agent(tmp_path).launch_options(root)
    assert options["config"]["sandbox_workspace_write"]["writable_roots"] == [str(root)]


def test_a_permission_profile_replaces_the_sandbox_and_gets_no_added_root(tmp_path):
    options = agent(tmp_path, sandbox=None, permissions="locked",
                    profile={"network": {"enabled": True}}).launch_options(
                        tmp_path / "record")
    assert options == {
        "permissions": "locked",
        "model": "gpt-sample",
        "config": {
            "agents": {"enabled": False},
            "permissions": {"locked": {"network": {"enabled": True}}},
        },
    }


@pytest.mark.parametrize(("reported", "reason"), [
    ({"type": "readOnly", "networkAccess": True, "writableRoots": []},
     "sandbox is 'readOnly', not 'workspaceWrite'"),
    ({"type": "workspaceWrite", "networkAccess": False, "writableRoots": []},
     "network access is off, not on"),
    ({"type": "workspaceWrite", "networkAccess": True, "writableRoots": []},
     "writable roots lack"),
])
def test_the_load_verifier_names_each_mismatch(tmp_path, reported, reason):
    row = agent(tmp_path)
    options = row.launch_options()
    assert reason in row.verify_load(options, {"cwd": str(tmp_path),
                                                "sandbox": reported})


def test_the_load_verifier_counts_cwd_as_writable(tmp_path):
    row = agent(tmp_path, writable_roots=[tmp_path])
    options = row.launch_options()
    answer = {"cwd": str(tmp_path), "sandbox": {
        "type": "workspaceWrite", "networkAccess": True, "writableRoots": [],
    }}
    assert row.verify_load(options, answer) is None


def test_a_permission_profile_has_no_sandbox_read_back(tmp_path):
    row = agent(tmp_path, sandbox=None, permissions="locked")
    assert row.verify_load(row.launch_options(), {"sandbox": {"type": "readOnly"}}) is None


def test_the_mock_backend_adds_the_store_directory(monkeypatch, tmp_path):
    store = tmp_path / "mock" / "store.json"
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "mock")
    monkeypatch.setenv("MOXI_COMMS_MOCK_GATEWAY", str(store))
    root, reason = record_write_root(env=os.environ, cwd=tmp_path)
    assert root == store.parent
    assert str(store) in reason


def test_a_bad_config_adds_no_root_and_says_why(monkeypatch, tmp_path):
    from moxi_comms import config as config_mod
    from moxi_comms.config import ConfigError

    monkeypatch.setattr(config_mod, "load", lambda **kwargs: (_ for _ in ()).throw(
        ConfigError("bad config")))
    root, reason = record_write_root(env=os.environ, cwd=tmp_path)
    assert root is None
    assert "could not be read" in reason
