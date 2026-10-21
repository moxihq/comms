"""comms' one seam to the cache module (`moxi_comms.cache`, design §6.4, §7.3, §7.4).

cache says what a recipient's cache is; comms decides what happens to the mail. comms
reaches cache only through `moxi.modules.load("cache", interface=1).api`, in the seam's
order: a `person` kind and an adapter with no clock answer without the module; then
`load`; then a kind's evidence function; then cache's `read` on the kind's harness. With
cache absent every agent reads `no-cache`, code `module-absent`, so comms holds nothing
for a cold cache and refuses nothing for an unreadable one. `[cache] required` makes the
daemon refuse to start without it.

The evidence functions below stand in for a module's kind (the fleet add-on's `driver`):
they are named by `test_cache_seam:<function>`, which the seam imports as a declared kind's
`evidence` target is imported.
"""

from __future__ import annotations

import ast
import json
import time
import uuid
from pathlib import Path

import pytest
from conftest import (assistant_line, cache_absent, cache_api, wait_until, write_config,
                      write_transcript)
from test_cold_every_sender import (HELPER_Q, HOUR, SESS_D, adapters,  # noqa: F401
                                    bare_shim, held_of, only_tmp_paths, post, send)

from moxi_comms import cache, cli, kinds
from moxi_comms.daemon import Daemon
from moxi_comms.paths import daemon_socket

SRC = Path(__file__).parents[1] / "src" / "moxi_comms"
NOW = time.time()

# ------------------------------------------------------------------ evidence functions
# What each was asked, so a test can see the seam hands over the binding's name and id.
ASKED: list = []
ANCHORS: dict = {}


def anchor_evidence(qualified, session_id):
    """A driver-like kind: its anchor is `ANCHORS[qualified]`; no entry is no anchor."""
    ASKED.append((qualified, session_id))
    if qualified not in ANCHORS:
        return ("", True)              # no anchor: the next delivery starts a new session
    anchor = ANCHORS[qualified]
    if anchor is None:
        return kinds.UNREADABLE        # an anchor file that cannot be read
    if anchor == "":
        return ("", True)              # a blank anchor: a new session next
    return (anchor, False)             # a recorded id: a session someone has run


def raising_evidence(qualified, session_id):
    raise OSError("the anchor directory is gone")


def misshapen_evidence(qualified, session_id):
    return {"session_id": "x"}


def empty_id_no_new_session(qualified, session_id):
    return ("", False)


def registry(evidence="anchor_evidence"):
    return kinds.load([
        Entry("driver", {"cache": "harness", "harness": "claude",
                         "evidence": f"test_cache_seam:{evidence}"}),
        Entry("speech", {"cache": "person"}),
    ])


class Entry:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def load(self):
        return self.value


@pytest.fixture(autouse=True)
def clean_evidence():
    ASKED.clear()
    ANCHORS.clear()
    yield
    ASKED.clear()
    ANCHORS.clear()


def module_not_asked(monkeypatch):
    def refuse():
        raise AssertionError("the seam asked for the cache module")
    monkeypatch.setattr(cache, "module", refuse)


# ============================================================== the seam's own rules

def test_no_import_in_comms_names_cache_s_package():
    """§6.4, §8.1: only `load` reaches cache; `moxi check` refuses an import of an
    optional module, and this holds it for every file of the package too."""
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(n.split(".")[0] == "moxi_cache" for n in names), path


def test_the_old_cache_code_is_gone():
    """§7.1: the readers, the table and the drift record left comms for cache."""
    for name in ("cache_codex.py", "cache_opencode.py", "cache_table.py", "cache_drift.py",
                 "cache_lifetimes.toml"):
        assert not (SRC / name).exists(), name


def test_a_person_kind_and_an_adapter_with_no_clock_answer_without_the_module(
        monkeypatch):
    module_not_asked(monkeypatch)
    person = cache.clock_state("speech", None, kinds=registry())
    assert (person.kind, person.code) == (cache.NO_CACHE, cache.PERSON)
    for adapter in ("send-only", "no-such-kind", None):
        clock = cache.clock_state(adapter, "x", kinds=registry())
        assert (clock.kind, clock.code) == (cache.NO_CACHE, cache.NO_CLOCK)
        assert clock.reason == cache.no_clock(adapter)


@pytest.mark.parametrize("state", ["absent", "broken"])
def test_with_cache_absent_every_harness_kind_reads_no_cache(monkeypatch, state):
    cache_absent(monkeypatch, state)
    sid = str(uuid.uuid4())
    write_transcript(sid, age=3 * HOUR)             # cold, if anything read it
    for adapter in ("claude-shim", "codex", "opencode", "driver"):
        clock = cache.clock_state(adapter, sid, kinds=registry())
        assert (clock.kind, clock.code) == (cache.NO_CACHE, cache.MODULE_ABSENT)
    assert ASKED == [], "no evidence function runs without the module"
    assert cache.projects_root({"CLAUDE_CONFIG_DIR": "/x"}) is None
    assert cache.open_store(learn=True) is None


def test_a_read_maps_the_kind_to_its_harness_and_carries_cache_s_fields():
    cache_api()
    sid = str(uuid.uuid4())
    write_transcript(sid, age=60)
    clock = cache.clock_state("claude-shim", sid)
    assert clock.kind == cache.WARM and isinstance(clock, cache.Clock)
    assert clock.ttl_seconds == 3600 and clock.ttl_source == "config"
    missing = cache.clock_state("claude-shim", "")
    assert (missing.kind, missing.code) == (cache.UNKNOWN, "no-session-yet")


def test_the_5_minute_tier_is_read_from_the_transcript():
    """§6.3 [R39]: a session on the five-minute tier counts five minutes, so ten minutes
    after its last request it reads cold (comms counted an hour before cache)."""
    cache_api()
    sid = str(uuid.uuid4())
    line = assistant_line(600)
    line["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 900,
                                                  "ephemeral_1h_input_tokens": 0}
    write_transcript(sid, lines=[line])
    clock = cache.clock_state("claude-shim", sid)
    assert (clock.kind, clock.ttl_seconds, clock.ttl_source) == (cache.COLD, 300, "tier")


def test_a_read_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("discovery broke")
    monkeypatch.setattr(cache, "module", broken)
    clock = cache.clock_state("claude-shim", "x")
    assert (clock.kind, clock.code) == (cache.UNKNOWN, cache.READ_FAILED)
    assert "discovery broke" in clock.reason


def test_a_kind_cache_does_not_know_reads_unknown(monkeypatch):
    api = cache_api()
    monkeypatch.setattr(api, "read", lambda *a, **k: type("C", (), {"kind": "tepid"})())
    clock = cache.clock_state("claude-shim", "x")
    assert (clock.kind, clock.code) == (cache.UNKNOWN, cache.READ_FAILED)


# ======================================================== a kind's evidence (§7.3)

def test_no_anchor_and_a_blank_anchor_read_fresh():
    cache_api()
    clock = cache.clock_state("driver", None, name="thinker@proj", kinds=registry())
    assert clock.kind == cache.FRESH
    ANCHORS["thinker@proj"] = ""
    assert cache.clock_state("driver", "ignored", name="thinker@proj",
                             kinds=registry()).kind == cache.FRESH
    assert ASKED == [("thinker@proj", None), ("thinker@proj", "ignored")]


def test_an_anchor_that_cannot_be_read_is_unknown():
    cache_api()
    ANCHORS["thinker@proj"] = None
    clock = cache.clock_state("driver", None, name="thinker@proj", kinds=registry())
    assert (clock.kind, clock.code) == (cache.UNKNOWN, cache.EVIDENCE_UNREADABLE)
    assert "thinker@proj" in clock.reason


def test_a_recorded_id_reads_the_transcript_s_clock_with_no_root_of_its_own():
    """The id names a session someone has run: read with `projects_root=None` and
    `turned=True`, so a missing transcript is `unknown`, whatever the binding reported."""
    cache_api()
    warm, cold, gone = (str(uuid.uuid4()) for _ in range(3))
    write_transcript(warm, age=60)
    write_transcript(cold, age=2 * HOUR)
    for sid, kind in ((warm, cache.WARM), (cold, cache.COLD)):
        ANCHORS["thinker@proj"] = sid
        assert cache.clock_state("driver", None, name="thinker@proj",
                                 kinds=registry()).kind == kind
    ANCHORS["thinker@proj"] = gone
    from conftest import projects_root
    clock = cache.clock_state("driver", None, name="thinker@proj", kinds=registry(),
                              projects_root=str(projects_root()), turned=False)
    assert (clock.kind, clock.code) == (cache.UNKNOWN, "no-record")


@pytest.mark.parametrize("fn", ["raising_evidence", "misshapen_evidence"])
def test_an_evidence_function_that_fails_is_unknown(fn):
    cache_api()
    clock = cache.clock_state("driver", None, name="thinker@proj", kinds=registry(fn))
    assert (clock.kind, clock.code) == (cache.UNKNOWN, cache.EVIDENCE_FAILED)


def test_an_empty_id_without_new_session_is_unknown():
    cache_api()
    clock = cache.clock_state("driver", None, name="t@p",
                              kinds=registry("empty_id_no_new_session"))
    assert (clock.kind, clock.code) == (cache.UNKNOWN, "no-session-yet")


def test_the_daemon_reads_a_declared_kind_through_its_evidence(rig, adapters):
    """A binding of a module's harness kind: its mail is read by the kind's evidence
    (B16 accepted it at register; this is where its evidence is called). A cold recorded
    session holds the mail; a blank anchor then lets the next mail through."""
    cache_api()
    rig.daemon.kinds = registry()
    sid = str(uuid.uuid4())
    write_transcript(sid, age=3 * HOUR)
    ANCHORS[HELPER_Q] = sid
    helper = adapters(slot=3, name="helper", scope="proj-a", adapter="driver")
    msg = post(rig, [HELPER_Q])
    assert wait_until(lambda: msg.id in held_of(rig, HELPER_Q))
    assert (HELPER_Q, None) in ASKED
    ANCHORS[HELPER_Q] = ""
    newer = post(rig, [HELPER_Q], "newer")
    assert wait_until(lambda: helper.ids() == [newer.id])


# ============================================== the daemon and the send, cache absent

def test_with_cache_absent_a_cold_agent_gets_its_mail_live(rig, adapters, monkeypatch):
    cache_absent(monkeypatch)
    helper = bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    msg = post(rig, [HELPER_Q])
    assert wait_until(lambda: helper.ids() == [msg.id])
    assert held_of(rig, HELPER_Q) == set()


def test_with_cache_absent_an_unreadable_agent_refuses_nothing(rig, adapters, monkeypatch):
    cache_absent(monkeypatch)
    adapters(slot=3, name="helper", scope="proj-a", adapter="codex", session_id="t-1")
    code, out = send(rig, HELPER_Q)
    assert code == 0, out
    assert "Nothing was sent" not in out


def test_comms_cache_says_no_cache_is_read_without_the_module(rig, adapters, monkeypatch):
    cache_absent(monkeypatch)
    bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    code, out = rig.cli(["cache", HELPER_Q], env={"MOXI_COMMS_OPERATOR": "900001"})
    assert code == 0, out
    assert out.startswith(f"{HELPER_Q}: cache is not installed, so comms does not count "
                          "its cache, and agents' messages reach it."), out


def test_the_session_answer_carries_the_code(rig, adapters, monkeypatch):
    cache_absent(monkeypatch)
    bare_shim(adapters, sid=SESS_D, age=3 * HOUR)
    answer = rig.daemon._session_query({"identity": HELPER_Q})
    assert (answer["cache_kind"], answer["cache_code"]) == (cache.NO_CACHE,
                                                            cache.MODULE_ABSENT)
    assert rig.daemon._session_query({"identity": "nobody@x"})["cache_code"] is None


# ======================================================== [cache] required (§7.4)

def test_required_and_absent_refuses_the_start(rig, monkeypatch):
    rig.daemon.stop()
    write_config(None, "[cache]\nrequired = true\n")
    cache_absent(monkeypatch)
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False)
    with pytest.raises(RuntimeError, match=r"\[cache\] required = true") as e:
        daemon.start()
    assert cache.INSTALL in str(e.value)
    assert not daemon.bound and not daemon_socket().exists()


def test_required_and_broken_refuses_the_start(rig, monkeypatch):
    rig.daemon.stop()
    write_config(None, "[cache]\nrequired = true\n")
    cache_absent(monkeypatch, "broken")
    with pytest.raises(RuntimeError, match="does not load"):
        Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()


def test_required_and_present_starts(rig):
    cache_api()
    rig.daemon.stop()
    write_config(None, "[cache]\nrequired = true\n")
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    try:
        assert daemon.bound
    finally:
        daemon.stop()


def test_not_required_and_absent_starts_without_the_hold(rig, monkeypatch):
    rig.daemon.stop()
    cache_absent(monkeypatch)
    daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    try:
        assert daemon.bound and daemon.cache_store is None
    finally:
        daemon.stop()


def test_doctor_names_the_module_and_the_install_command(rig, monkeypatch, capsys):
    cache_absent(monkeypatch)
    write_config(None, "[cache]\nrequired = true\n")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "not installed" in out and cache.INSTALL in out
    assert "the daemon refuses to start" in out


def test_doctor_says_present(rig, capsys):
    cache_api()
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "present (moxi-cache" in out


# ========================================================= the drift store (RULE C3)

def test_the_daemon_learns_through_cache_s_store_and_lets_it_go_at_stop(rig):
    api = cache_api()
    assert rig.daemon.cache_store is not None and rig.daemon.cache_store.learning
    second = api.open_store(learn=True)
    try:
        assert not second.learning, "one process learns; the daemon holds the lock"
    finally:
        second.close()
    rig.daemon.stop()
    after = api.open_store(learn=True)
    try:
        assert after.learning
    finally:
        after.close()


def test_a_store_handed_in_is_kept_open_at_stop(rig):
    api = cache_api()
    rig.daemon.stop()
    store = api.open_store(learn=True)
    try:
        daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False,
                        cache_store=store).start()
        assert daemon.cache_store is store
        daemon.stop()
        assert store.learning
    finally:
        store.close()


def test_cache_writes_under_its_own_roots_in_the_rig(rig, tmp_path):
    """The rig points cache's roots into tmp: the learning lock is there, not in the home
    of whoever runs the suite."""
    cache_api()
    lock = Path(rig.daemon.cache_store.lock_path)
    assert str(lock).startswith(str(tmp_path)), lock
    assert json.dumps(str(lock))
