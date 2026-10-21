"""The `opencode` adapter kind and the daemon policy keyed to its name.

A kind adds its name to the daemon's list of kinds and to the cache reader's answer for
the kind. The send takes the daemon's verdict for it as for every adapter: `--held` is
accepted, a cold read posts and holds, and a read comms cannot make refuses the send.
Each is pinned here.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from conftest import wait_until

from moxi_comms import cache, cli, machine_line
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import ADAPTER_KINDS
from moxi_comms.paths import daemon_socket

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
CODER = dict(slot=5, name="coder", scope="proj-a", adapter="opencode")
AS_OPERATOR = dict(env={"MOXI_COMMS_OPERATOR": "900001"})


@pytest.fixture
def bind():
    made = []

    def _bind(**kwargs):
        c = DaemonClient(daemon_socket()).connect()
        c.register(**kwargs)
        made.append(c)
        return c

    yield _bind
    for c in made:
        c.close()


def test_the_daemon_knows_the_kind_and_binds_a_name_with_it(rig, bind):
    assert "opencode" in ADAPTER_KINDS
    bind(**CODER)
    assert wait_until(lambda: "coder@proj-a" in rig.daemon.bindings)
    assert rig.daemon.bindings["coder@proj-a"].adapter == "opencode"


def coder_reads(rig, monkeypatch, **answer):
    """The daemon's `session` answer for coder@proj-a, as given; every other name
    answers as the daemon does."""
    real = rig.daemon._session_query

    def session(frame):
        if frame.get("identity") == "coder@proj-a":
            return {"identity": "coder@proj-a", "bound": True, "adapter": "opencode",
                    "session_id": "s", "bound_at": 0.0, "bound_for": 60.0,
                    "cache_reason": None, "cache_key": None, "cache_source": None,
                    **answer}
        return real(frame)

    monkeypatch.setattr(rig.daemon, "_session_query", session)


def test_held_is_taken_by_an_opencode_name(rig, bind, monkeypatch):
    """`--held` is accepted for every recipient; an opencode one is no exception."""
    rig.shim(**REVIEWER)
    bind(**CODER)
    coder_reads(rig, monkeypatch, cache_kind="cold", age_seconds=4000, cache_ttl=1800)
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a,coder@proj-a", "later",
                         "--held"],
                        **PLANNER)
    assert code == 0, out
    assert cli.HELD_NOTE.format(name="coder@proj-a") in out.splitlines()
    (msg,) = rig.gateway.history(rig.names_channel)
    assert machine_line.parse(msg.content).held


def test_a_cold_opencode_name_is_posted_and_held(rig, bind, monkeypatch):
    bind(**CODER)
    coder_reads(rig, monkeypatch, cache_kind="cold", age_seconds=4000, cache_ttl=1800)
    code, out = rig.cli(["send", "--posture", "none", "coder@proj-a", "hello"], **PLANNER)
    assert code == 0, out
    assert out.splitlines()[1].startswith("held: coder@proj-a's cache went cold")
    assert len(rig.gateway.history(rig.names_channel)) == 1


def test_an_opencode_name_takes_the_daemons_verdict(rig, bind):
    """cache-clock: comms reads opencode bindings, so the verb takes the daemon's
    verdict, with its age and lifetime. A reply with no verdict reads `unknown`; a bare
    bind that reported no evidence reads `unknown` with the reason."""
    target = cache.target_for_name("coder@proj-a", {
        "bound": True, "adapter": "opencode", "cache_kind": "cold", "age_seconds": 4000,
        "cache_ttl": 1800})
    assert (target.settled, target.age_seconds, target.ttl_seconds) == \
        (cache.COLD, 4000.0, 1800.0)
    target = cache.target_for_name("coder@proj-a", {"bound": True, "adapter": "opencode"})
    assert target.settled == cache.UNKNOWN
    rig.shim(**REVIEWER)
    bind(**CODER)
    code, out = rig.cli(["cache", "coder@proj-a"], **AS_OPERATOR)
    assert code == 0
    assert "coder@proj-a: cache state not known (no session id reported yet)" in out


def test_a_send_to_an_opencode_name_with_no_session_id_is_refused(rig, bind):
    """A read comms cannot make refuses the send, exit 6, nothing posted: a bare bind
    has reported no session id, so its cache cannot be read."""
    bind(**CODER)
    for flags in ([], ["--held"]):
        code, out = rig.cli(["send", "--posture", "none", "coder@proj-a", "hello", *flags],
                            **PLANNER)
        assert code == cli.EXIT_COLD, out
        assert out.splitlines() == [
            "Nothing was sent: comms could not read coder@proj-a's cache (no session id "
            "reported yet).",
            "Send it again in a moment; a read that succeeds holds or delivers it.",
            "Nothing was sent."]
    assert rig.gateway.history(rig.names_channel) == []


def doctor(env):
    out = io.StringIO()
    ctx = cli.Context(gateway=object(), env=env, socket_path=None, out=out,
                      db_path=Path("/nonexistent/db"))
    cli.main(["doctor"], ctx=ctx)
    return out.getvalue()


def test_doctor_lists_the_opencode_binary_with_its_version(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "opencode"
    fake.write_text("#!/bin/sh\necho '1.18.34'\n")
    fake.chmod(0o755)
    rows = {line.split("  ", 1)[0].strip(): line.split("  ", 1)[1].strip()
            for line in doctor({"PATH": str(bin_dir)}).splitlines()
            if line[:1] != " " and "  " in line}
    assert rows["opencode"] == f"{fake}, 1.18.34"
    rows = {line.split("  ", 1)[0].strip(): line.split("  ", 1)[1].strip()
            for line in doctor({"PATH": str(tmp_path / "empty")}).splitlines()
            if line[:1] != " " and "  " in line}
    assert rows["opencode"] == "not on PATH"
