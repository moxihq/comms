"""The `moxi.comms.kinds` group: comms' own kinds declared as any module's are, the check
each declaration gets, and the daemon accepting what the group declares at register."""

from __future__ import annotations

import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from moxi_comms import kinds
from moxi_comms.client import DaemonClient, DaemonError
from moxi_comms.daemon import ADAPTER_KINDS

PYPROJECT = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())


class Entry:
    """A stand-in entry point: a name, and what loading it gives."""

    def __init__(self, name, value=None, *, fails=None):
        self.name, self.value, self.fails = name, value, fails

    def load(self):
        if self.fails:
            raise self.fails
        return self.value


def test_comms_declares_its_own_kinds_in_the_group():
    declared = PYPROJECT["project"]["entry-points"][kinds.GROUP]
    assert declared == {"claude-shim": "moxi_comms.kinds:CLAUDE_SHIM",
                        "codex": "moxi_comms.kinds:CODEX",
                        "opencode": "moxi_comms.kinds:OPENCODE",
                        "send-only": "moxi_comms.kinds:SEND_ONLY"}
    assert tuple(declared) == tuple(kinds.BUILT_IN) == ADAPTER_KINDS


def test_the_installed_group_reads_as_the_built_ins():
    """This environment's metadata carries the group, and reading it changes nothing."""
    names = {ep.name for ep in entry_points(group=kinds.GROUP)}
    assert set(kinds.BUILT_IN) <= names
    got = kinds.load()
    assert got.names()[:4] == ADAPTER_KINDS and got.broken == ()


def test_the_built_ins_are_known_with_no_metadata():
    got = kinds.load([])
    assert got.names() == ADAPTER_KINDS
    assert [(k.cache, k.harness) for k in got.kinds] == [
        ("harness", "claude"), ("harness", "codex"), ("harness", "opencode"),
        ("send-only", None)]
    assert all(k.built_in and k.evidence is None for k in got.kinds)


def test_a_person_kind_and_a_harness_kind_are_accepted():
    got = kinds.load([
        Entry("speech", {"cache": "person"}),
        Entry("driver", {"cache": "harness", "harness": "claude",
                         "evidence": "some_addon.kinds:driver_evidence"}),
    ])
    assert got.broken == ()
    assert got.get("speech") == kinds.Kind("speech", "person")
    assert got.get("driver") == kinds.Kind("driver", "harness", "claude",
                                           "some_addon.kinds:driver_evidence")
    assert "driver" in got and "nothing" not in got


@pytest.mark.parametrize("entry, reason", [
    (Entry("Bad_Name", {"cache": "person"}), "is not a kind name"),
    (Entry("listy", ["person"]), "not a table"),
    (Entry("extra", {"cache": "person", "colour": "red"}), "keys comms does not read"),
    (Entry("talker", {"cache": "person", "harness": "claude"}), "names no harness"),
    (Entry("pager", {"cache": "pager"}), "cache treatment is 'pager'"),
    (Entry("silent", {"cache": "send-only"}), "cache treatment is 'send-only'"),
    (Entry("gemini", {"cache": "harness", "harness": "gemini", "evidence": "a.b:c"}),
     "harness is 'gemini'"),
    (Entry("blind", {"cache": "harness", "harness": "claude"}), "evidence function"),
    (Entry("broken", fails=ImportError("no module")), "does not load"),
    (Entry("codex", {"cache": "person"}), "built-in kind of comms"),
])
def test_a_declaration_that_does_not_hold_is_left_out_with_its_reason(entry, reason):
    got = kinds.load([entry])
    assert got.names() == ADAPTER_KINDS
    (broken,) = got.broken
    assert broken.name == entry.name and reason in broken.reason, broken


def test_two_declarations_of_one_kind_are_both_left_out():
    got = kinds.load([Entry("speech", {"cache": "person"}),
                      Entry("speech", {"cache": "person"})])
    assert "speech" not in got and got.broken[0].name == "speech"


def test_comms_own_entry_for_a_built_in_is_its_declaration():
    got = kinds.load([Entry("codex", dict(kinds.CODEX))])
    assert got.broken == () and got.names() == ADAPTER_KINDS


def test_the_daemon_accepts_a_declared_kind_and_refuses_an_unknown_one(rig):
    rig.daemon.kinds = kinds.load([Entry("speech", {"cache": "person"})])
    client = DaemonClient(rig.daemon.socket_path).connect()
    try:
        with pytest.raises(DaemonError) as e:
            client.register(slot="next-free", name="talker", scope="proj-a",
                            adapter="no-such-kind")
        assert "unknown adapter kind 'no-such-kind'" in str(e.value)
        assert "claude-shim, codex, opencode, send-only, speech" in str(e.value)
        answer = client.register(slot="next-free", name="talker", scope="proj-a",
                                 adapter="speech")
        assert answer["ok"]
    finally:
        client.close()
    assert rig.daemon.bindings["talker@proj-a"].adapter == "speech"


def test_a_default_daemon_reads_the_group(rig):
    assert rig.daemon.kinds.names()[:4] == ADAPTER_KINDS
