"""One rule for names, and retired identities.

A tag names exactly one identity, and the tag alone decides which: `x@S` (S not global) is
x@S, `x` and `x@global` are the global x, `agent-N` is the ephemeral agent-N. No reader
consults the sender's, author's, reader's, claim's or working directory's scope. A bind
that names no scope is global. A directory row with status `retired` at (name, scope) is
an identity comms no longer binds, routes to or queues for; its history stays."""

from __future__ import annotations

import pytest

from conftest import queued, record_rows, wait_for_queue, wait_until
from moxi_comms import bindings, cards, directors, directory, scoping
from moxi_comms.adapters.session import Session
from moxi_comms.client import DaemonClient, DaemonError
from moxi_comms.paths import daemon_socket, directory_db

RETIRED_SEND = ("{q} is retired: comms keeps a project suffix only to tell apart two "
                "agents of one name in different projects, and {q} had no such twin. "
                "Send to {name} instead. Nothing was sent.")
RETIRED_BIND = ("{q} is retired: comms keeps a project suffix only to tell apart two "
                "agents of one name in different projects, and {q} had no such twin. "
                "Bind {name} instead, with no --scope.")


def retire(name: str, scope: str) -> None:
    """The retire op's own insert."""
    conn = directory.connect(directory_db())
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO agents(name, scope, status, created_at, "
                         "kind) VALUES (?, ?, 'retired', ?, 'agent')",
                         (name, scope, directory.now()))
    finally:
        conn.close()


def history(rig):
    return rig.gateway.history(rig.names_channel)


def query(kind, **kw):
    c = DaemonClient(daemon_socket()).connect()
    try:
        return c.query(kind, **kw)
    finally:
        c.close()


# --------------------------------------------------------------------------- the rule

@pytest.mark.parametrize("tag, identity", [
    ("x", "x"), ("x@global", "x"), ("x@proj-a", "x@proj-a"),
    ("agent-7", "agent-7"), ("agent-7@proj-a", "agent-7"),
])
def test_identity_of_reads_the_tag_alone(tag, identity):
    assert scoping.identity_of(tag) == identity


def test_tag_matches_is_identity_equality_with_no_scope():
    assert scoping.tag_matches("x", "x")
    assert scoping.tag_matches("x@global", "x")
    assert not scoping.tag_matches("x", "x@proj-a"), "a bare tag never matches x@S"
    assert not scoping.tag_matches("x@proj-a", "x"), "x@S never matches the global x"
    assert scoping.tag_matches("x@proj-a", "x@proj-a")
    assert not scoping.tag_matches("x@proj-b", "x@proj-a")


def test_x_and_x_at_global_resolve_to_one_identity_from_every_scope(rig):
    for tag in ("lead", "lead@global"):
        r = rig.daemon.resolve(tag)
        assert (r.name, r.scope, r.qualified) == ("lead", "global", "lead")
    r = rig.daemon.resolve("lead@proj-a")
    assert r.qualified == "lead@proj-a"
    # the verb's query: a scope or project on the frame is not read
    for extra in ({}, {"scope": "proj-a"}, {"project": "proj-b"}):
        assert query("resolve", name="lead", **extra)["qualified"] == "lead"


def test_a_bare_name_typed_in_s_goes_to_the_global_x_while_x_at_s_is_bound(rig):
    first = rig.shim(slot=3, name="reviewer")         # the global one has bound before
    first.stop()
    assert wait_until(lambda: "reviewer" not in rig.daemon.bindings)
    scoped = rig.shim(slot=2, name="reviewer", scope="proj-a")
    assert scoped.identity == "reviewer@proj-a"
    code, out = rig.cli(["send", "--posture", "none", "reviewer", "to global"],
                        slot=1, name="planner", scope="proj-a")
    assert code == 0, out
    assert history(rig)[-1].content.splitlines()[0].startswith("::to reviewer")
    assert "reviewer is not bound right now" in out
    assert scoped.queue == [], "never reaches x@S"
    glob = rig.shim(slot=3, name="reviewer")
    assert glob.identity == "reviewer"
    wait_for_queue(glob, 1)
    assert glob.queue[0]["text"] == "to global"
    assert scoped.queue == []


def test_a_bare_line_zero_tag_from_an_author_in_s_waits_for_the_global_x(rig):
    """Live route: an author in proj-a writes bare `reviewer` while reviewer@proj-a is
    bound. It waits for the global reviewer and never reaches reviewer@proj-a."""
    scoped = rig.shim(slot=2, name="reviewer", scope="proj-a")
    rig.shim(slot=1, name="planner", scope="proj-a")
    m = rig.post_from_slot(1, ["reviewer"], "bare from proj-a", display="planner@proj-a")
    assert wait_until(lambda: m.id in queued(rig.daemon, "reviewer"))
    assert not queued(rig.daemon, "reviewer@proj-a")
    assert scoped.queue == []


def test_at_a_rescan_and_a_replay_a_bare_tag_is_the_global_x(rig):
    """The journal knows reviewer@proj-a, the global reviewer was never bound: a rescan
    still files the bare tag for the global one, and its first bind's replay delivers it,
    while a rebind of reviewer@proj-a is never handed it."""
    scoped = rig.shim(slot=2, name="reviewer", scope="proj-a")
    scoped.stop()
    assert wait_until(lambda: "reviewer@proj-a" not in rig.daemon.bindings)
    m = rig.post_from_slot(1, ["reviewer"], "for the global one", display="planner@proj-a")
    assert wait_until(lambda: m.id in queued(rig.daemon, "reviewer"))
    rig.daemon.rescan()
    assert m.id in queued(rig.daemon, "reviewer")
    assert not queued(rig.daemon, "reviewer@proj-a")
    again = rig.shim(slot=2, name="reviewer", scope="proj-a")
    glob = rig.shim(slot=3, name="reviewer")
    wait_for_queue(glob, 1)
    assert glob.queue[0]["message_id"] == m.id
    assert again.queue == []


def test_a_reply_to_a_global_author_goes_to_the_global_author(rig):
    lead = rig.shim(slot=3, name="lead")
    rig.shim(slot=2, name="lead", scope="proj-a")
    code, out = rig.cli(["send", "--posture", "none", "lead", "from the global"], slot=1,
                        name="planner")
    assert code == 0, out
    asked = history(rig)[-1]
    code, out = rig.cli(["reply", "--posture", "none", str(asked.id), "an answer"],
                        slot=4, name="planner", scope="proj-a")
    assert code == 0, out
    assert history(rig)[-1].content.splitlines()[0].startswith("::to planner ")
    assert lead.identity == "lead"


def test_attributed_message_resolves_by_the_rule(rig):
    rig.shim(slot=2, name="reviewer", scope="proj-a")
    m = rig.post_from_slot(1, ["reviewer", "reviewer@proj-a"], "two",
                           display="planner@proj-a")
    got = query("attributed_message", message_id=m.id)
    assert got["resolved"] == ["reviewer", "reviewer@proj-a"]


def test_history_spells_the_typed_identity_once_by_the_rule(rig):
    rig.shim(slot=3, name="lead")
    code, out = rig.cli(["send", "--posture", "none", "lead", "hello"], slot=1, name="planner")
    assert code == 0, out
    code, a = rig.cli(["history", "lead@global"])
    code, b = rig.cli(["history", "lead"])
    assert a == b and "hello" in a
    code, c = rig.cli(["history", "lead@proj-a"])
    assert "hello" not in c


# --------------------------------------------------------------------------- the bind default

def test_a_shim_with_no_scope_binds_global(rig):
    unstarted = rig.shim(slot=2, name="reviewer", project="proj-a", start=False)
    assert unstarted.own_qualified() == "reviewer", "the holder a refusal is about"
    s = rig.shim(slot=2, name="reviewer", project="proj-a")
    assert s.identity == "reviewer"


def test_a_register_with_only_project_binds_global(rig):
    c = DaemonClient(daemon_socket()).connect()
    try:
        got = c.register(name="loner", project="proj-a", adapter="claude-shim")
        assert got["identity"]["qualified"] == "loner"
    finally:
        c.close()


def test_comms_claude_with_neither_scope_nor_global_is_global(tmp_path):
    s = Session("abcd1234", name="reviewer", project="proj-a")
    assert s.global_ and s.scope == "global"
    env = s.env(base={})
    assert env["MOXI_COMMS_GLOBAL"] == "1" and env["MOXI_COMMS_SCOPE"] == "global"
    assert env["MOXI_COMMS_PROJECT"] == "proj-a", "the project still feeds the environment"
    scoped = Session("abcd1235", name="reviewer", scope="proj-a", project="proj-a")
    assert not scoped.global_ and scoped.scope == "proj-a"
    assert "MOXI_COMMS_GLOBAL" not in scoped.env(base={})


def test_a_stamp_with_no_scope_is_the_global_identity():
    from moxi_comms.sender import sender_from_env
    assert sender_from_env({"MOXI_COMMS_NAME": "planner"}, "proj-a").qualified == "planner"
    assert sender_from_env({"MOXI_COMMS_NAME": "planner", "MOXI_COMMS_SCOPE": "proj-a"},
                           "proj-b").qualified == "planner@proj-a"


# --------------------------------------------------------------------------- directors, whois

@pytest.mark.parametrize("scope", ["proj-a", "proj-b", "global"])
def test_a_bare_director_is_the_global_identity_from_every_session_scope(scope):
    s = Session("abcd1236", name="worker", scope=None if scope == "global" else scope,
                directors=["lead", "boss@proj-b", "chief@global"], project="proj-a")
    assert s.directors == ["lead", "boss@proj-b", "chief"]
    assert directors.declare(["lead", "lead@global"]) == ["lead"]


def test_a_bare_whois_reads_the_global_card_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cards, "cards_dir", lambda: tmp_path)
    (tmp_path / "lead@proj-a.md").write_text("the proj-a lead\n")
    env = {"MOXI_COMMS_NAME": "me", "MOXI_COMMS_SCOPE": "proj-a"}
    miss = cards.answer("lead", env)
    assert "the proj-a lead" not in miss.text
    (tmp_path / "lead.md").write_text("the global lead\n")
    assert "the global lead" in cards.answer("lead", env).text
    assert "the proj-a lead" in cards.answer("lead@proj-a", env).text
    assert cards._own_identity({"MOXI_COMMS_NAME": "me"}) == "me", "no stamp scope: global"


# --------------------------------------------------------------------------- ever bound

def test_journal_ever_bound_reads_the_exact_identity(tmp_path):
    j = bindings.Journal(tmp_path / "b.db")
    assert j.ever_bound("x", "global") is None, "a journal that is not open cannot say"
    assert j.open()
    j.bind(qualified="x@proj-a", name="x", scope="proj-a", tier="chosen", slot_id=1,
           adapter="claude-shim")
    assert j.ever_bound("x", "proj-a") is True
    assert j.ever_bound("x", "global") is False
    assert j.ever_bound("x", "proj-b") is False
    j.close()


def test_resolve_answers_ever_bound_and_the_identities_seen(rig):
    s = rig.shim(slot=2, name="reviewer", scope="proj-a")
    got = query("resolve", name="reviewer@proj-a")
    assert got["ever_bound"] is True and "seen_as" not in got
    s.stop()
    assert wait_until(lambda: "reviewer@proj-a" not in rig.daemon.bindings)
    assert query("resolve", name="reviewer@proj-a")["ever_bound"] is True
    bare = query("resolve", name="reviewer")
    assert bare["ever_bound"] is False and bare["seen_as"] == ["reviewer@proj-a"]
    assert query("resolve", name="nobody")["seen_as"] == []
    retire("reviewer", "proj-a")
    assert query("resolve", name="reviewer")["seen_as"] == [], "never a retired one"


def test_resolve_answers_ever_bound_none_when_the_journal_is_closed(rig):
    rig.daemon.journal.close()
    got = query("resolve", name="nobody")
    assert got["ever_bound"] is None


# --------------------------------------------------------------------------- retired

def test_directory_retired_and_standing_row_ignores_a_retired_row(rig):
    retire("captain", "proj-old")
    conn = directory.connect(directory_db(), read_only=True)
    try:
        assert directory.retired(conn, "captain", "proj-old")
        assert not directory.retired(conn, "captain", "global")
        assert directory.standing_row(conn, "captain", "proj-old") is None
        assert all(r["name"] != "captain" for r in directory.standing_rows(conn))
    finally:
        conn.close()


def test_a_retired_identity_cannot_bind(rig):
    retire("captain", "proj-old")
    c = DaemonClient(daemon_socket()).connect()
    try:
        with pytest.raises(DaemonError) as e:
            c.register(name="captain", scope="proj-old", adapter="claude-shim")
        assert str(e.value) == RETIRED_BIND.format(q="captain@proj-old",
                                                   name="captain")
    finally:
        c.close()
    glob = rig.shim(slot=2, name="captain")
    assert glob.identity == "captain", "the global name binds"


@pytest.mark.parametrize("argv", [
    ["send", "--posture", "none", "mate@proj-old", "x"],
    ["send", "--check", "--posture", "none", "mate@proj-old", "x"],
])
def test_a_send_or_check_to_a_retired_identity_is_refused(rig, argv):
    retire("mate", "proj-old")
    before = len(history(rig))
    code, out = rig.cli(argv, slot=1, name="planner")
    assert code == 2
    assert out.strip() == RETIRED_SEND.format(q="mate@proj-old",
                                              name="mate")
    assert len(history(rig)) == before, "nothing posted"


def test_a_reply_to_a_retired_author_is_refused(rig):
    rig.shim(slot=2, name="mate", scope="proj-old")
    m = rig.post_from_slot(2, ["planner"], "from the old address",
                           display="mate@proj-old")
    rig.shims[-1].stop()
    assert wait_until(lambda: "mate@proj-old" not in rig.daemon.bindings)
    retire("mate", "proj-old")
    before = len(history(rig))
    code, out = rig.cli(["reply", "--posture", "none", str(m.id), "answer"], slot=1,
                        name="planner")
    assert code == 2
    assert RETIRED_SEND.format(q="mate@proj-old",
                               name="mate") in out
    assert len(history(rig)) == before


def test_explain_and_cache_refuse_a_retired_identity(rig):
    retire("captain", "proj-old")
    text = RETIRED_SEND.format(q="captain@proj-old", name="captain")
    code, out = rig.cli(["explain", "captain@proj-old"])
    assert code == 2 and out.strip() == text
    code, out = rig.cli(["cache", "captain@proj-old"],
                        env={"MOXI_COMMS_OPERATOR": "900001"})
    assert code == 2 and text in out


def test_a_recorded_tag_naming_a_retired_identity_is_held_for_nobody(rig):
    """A line zero typed by hand (or written before the retirement): the live route and
    a rescan write nothing for it, and the global name is untouched."""
    retire("captain", "proj-old")
    rig.shim(slot=1, name="planner")
    m = rig.post_from_slot(1, ["captain@proj-old", "captain"], "two",
                           display="planner")
    assert wait_until(lambda: m.id in queued(rig.daemon, "captain"))
    rig.daemon.rescan()
    assert not queued(rig.daemon, "captain@proj-old")
    assert not record_rows(rig.gateway, "captain@proj-old")
    assert m.id in queued(rig.daemon, "captain")


def test_the_provisioner_does_not_reactivate_a_retired_row(rig):
    from moxi_comms import provision
    retire("console2", "acme")
    with pytest.raises(SystemExit) as e:
        provision.send_only_main(["console2@acme", "--db", str(directory_db())],
                                 gateway=rig.gateway, store=rig.store)
    assert "retired" in str(e.value)
    conn = directory.connect(directory_db(), read_only=True)
    try:
        assert directory.retired(conn, "console2", "acme")
        assert directory.standing_row(conn, "console2", "acme") is None
    finally:
        conn.close()
