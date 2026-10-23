"""Claims: exclusivity, the reserved agent-N namespace, scope resolution, standing bind."""

import pytest

from moxi_comms.client import DaemonClient, DaemonError
from moxi_comms.paths import daemon_socket


def client():
    return DaemonClient(daemon_socket()).connect()


def test_nameless_claim_gets_a_monotonic_auto_name(rig):
    a = rig.shim(slot=1)
    b = rig.shim(slot=2)
    assert (a.identity, a.tier) == ("agent-1", "ephemeral")
    assert (b.identity, b.tier) == ("agent-2", "ephemeral")


def test_auto_names_are_never_reused(rig):
    a = rig.shim(slot=1)
    assert a.identity == "agent-1"
    a.stop()
    from conftest import wait_until
    wait_until(lambda: "agent-1" not in rig.daemon.bindings)
    c = rig.shim(slot=1)
    assert c.identity == "agent-2", "a retired agent-N is nobody; the number never returns"


def test_agent_n_is_refused_as_a_chosen_name(rig):
    with pytest.raises(DaemonError, match="reserved"):
        rig.shim(slot=1, name="agent-9", scope="proj-a")


def test_one_live_binding_per_slot_and_the_refusal_names_the_holder(rig):
    """The daemon's law, asked of the daemon. A claude-shim reads
    this refusal as a slot refusal and binds `next-free` instead (tests/test_slot_fix.py),
    so the register goes straight through a client here: the refusal itself is
    unchanged."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    c = client()
    try:
        with pytest.raises(DaemonError) as e:
            c.register(slot=1, name="other", scope="proj-a")
    finally:
        c.close()
    assert "planner@proj-a" in str(e.value)
    assert e.value.response["holder"]["qualified"] == "planner@proj-a"


def test_one_live_binding_per_name_scope(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    with pytest.raises(DaemonError, match="already bound"):
        rig.shim(slot=2, name="planner", scope="proj-a")
    # ...but the same bare name in another scope is a different identity
    other = rig.shim(slot=3, name="planner", scope="proj-b")
    assert other.identity == "planner@proj-b"


def test_standing_bind_is_open_first_bind_wins_and_the_second_is_refused(rig):
    s = rig.shim(slot=1, name="main", scope="proj-a")
    assert s.tier == "standing", "a provisioned row makes the binding standing"
    with pytest.raises(DaemonError) as e:
        rig.shim(slot=2, name="main", scope="proj-a")
    assert "main@proj-a" in str(e.value)
    assert e.value.response["holder"]["slot"] == 1


def test_standing_rows_are_read_per_claim_not_from_a_boot_cache(rig):
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.provision import add_standing

    conn = directory.connect(directory_db())
    try:
        add_standing(rig.gateway, conn, "curator", "proj-a")
    finally:
        conn.close()
    s = rig.shim(slot=1, name="curator", scope="proj-a")
    assert s.tier == "standing", "a row added while the daemon runs protects immediately"


def test_global_claim_is_unscoped(rig):
    s = rig.shim(slot=1, name="herald", global_=True)
    assert s.identity == "herald" and s.tier == "standing"


def test_a_tag_names_one_identity_whatever_the_scope_asking(rig):
    """The tag alone decides the identity. A bare name is the global identity from every
    scope, `x@S` is x@S from every scope, and the scope in the frame plays no part."""
    rig.shim(slot=1, name="main", scope="proj-a")
    rig.shim(slot=2, name="main", scope="proj-b")
    rig.shim(slot=3, name="herald", global_=True)
    c = client()
    try:
        for asking in ("proj-a", "proj-b", "global", None):
            frame = {} if asking is None else {"scope": asking}
            # a scoped identity is named in full, and is the same from everywhere
            assert c.query("resolve", name="main@proj-a", **frame)["qualified"] == "main@proj-a"
            assert c.query("resolve", name="main@proj-b", **frame)["qualified"] == "main@proj-b"
            # a bare name is the global identity, even while main@<asking> is bound
            r = c.query("resolve", name="main", **frame)
            assert r["qualified"] == "main" and r["bound"] is False and r["deferred"] is True
            r = c.query("resolve", name="herald", **frame)
            assert r["qualified"] == "herald" and r["bound"] is True
            # nothing bound: defer as the global identity, never as <asking>'s
            r = c.query("resolve", name="ghost", **frame)
            assert (r["qualified"], r["tier"], r["deferred"]) == ("ghost", "chosen", True)
    finally:
        c.close()


def test_unbound_standing_name_resolves_to_deferral_not_an_error(rig):
    c = client()
    try:
        r = c.query("resolve", name="main@proj-a")
        assert r["tier"] == "standing" and r["deferred"] is True and r["bound"] is False
        r = c.query("resolve", name="herald", scope="proj-a")
        assert r["tier"] == "standing" and r["deferred"] is True and r["bound"] is False
    finally:
        c.close()


def test_unresolvable_ephemeral_is_an_honest_error(rig):
    c = client()
    try:
        with pytest.raises(DaemonError, match="no such ephemeral identity"):
            c.query("resolve", name="agent-77", scope="proj-a")
    finally:
        c.close()


def test_resolution_is_not_a_liveness_check(rig):
    """A bound name and an unbound chosen name both RESOLVE; only `bound` differs."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    c = client()
    try:
        live = c.query("resolve", name="planner@proj-a")
        deferred = c.query("resolve", name="typo@proj-a")
        assert live["route"] == deferred["route"] == "#names"
        assert live["bound"] is True and deferred["bound"] is False
    finally:
        c.close()


def test_close_is_deregistration(rig):
    from conftest import wait_until
    s = rig.shim(slot=1, name="planner", scope="proj-a")
    assert "planner@proj-a" in rig.daemon.bindings
    s.stop()
    assert wait_until(lambda: "planner@proj-a" not in rig.daemon.bindings)
    assert 1 not in rig.daemon.by_slot


def test_unknown_adapter_kind_is_refused(rig):
    c = client()
    try:
        with pytest.raises(DaemonError, match="unknown adapter kind"):
            c.register(slot=1, name="x", scope="proj-a", adapter="telepathy")
    finally:
        c.close()


def test_register_still_refuses_an_unknown_adapter_kind(rig):
    """The refusal is at the frame -- never discovered later at delivery time -- and it
    names every kind that IS accepted, so a closed set does not quietly turn open."""
    c = client()
    try:
        with pytest.raises(DaemonError, match="unknown adapter kind") as e:
            c.register(slot=1, name="x", scope="proj-a", adapter="megaphone")
    finally:
        c.close()
    said = str(e.value)
    for kind in ("claude-shim", "codex", "opencode", "send-only"):
        assert kind in said
    assert "x@proj-a" not in rig.daemon.bindings   # refused at the frame: nothing bound


def test_next_free_slot(rig):
    a = rig.shim()
    b = rig.shim()
    assert (a.slot_id, b.slot_id) == (1, 2)
