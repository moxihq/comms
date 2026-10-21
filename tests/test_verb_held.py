"""The send verb's half of comms/refine §3 ([B8] verb slice): `--held` and `[held]`, the
send's `[cold-at:…]` ([C12], [R70]), the refusal of a read comms cannot make ([F7]),
`--held` to the operator ([F21], [R12]), the held frame mark ([R10]) and the one version
check on `ping` ([R14]).

The daemon's use of the cold-at (§3.6) is the queue slice's; here the token is pinned
as the verb writes it, from `session` answers the daemon is made to give.
"""

from __future__ import annotations

import time

import pytest
from conftest import queued, wait_for_queue, wait_until

from moxi_comms import cli, machine_line
from moxi_comms.adapters.claude_shim import frame_line, held_mark, sent_at
from moxi_comms.client import DaemonClient
from moxi_comms.paths import daemon_socket

PLANNER = dict(slot=1, name="planner")
T_ASK = 1_760_000_000.5


def last(rig):
    return rig.gateway.history(rig.names_channel)[-1]


def posted(rig):
    return rig.gateway.history(rig.names_channel)


def answers(rig, monkeypatch, table: dict):
    """The daemon's `session` answer for each name in `table`, as given; every other
    name answers as the daemon does."""
    real = rig.daemon._session_query

    def session(frame):
        identity = str(frame.get("identity") or "")
        if identity in table:
            return {"identity": identity, "bound": True, "session_id": "s",
                    "bound_at": 0.0, "bound_for": 60.0, "cache_reason": None,
                    "cache_key": None, "cache_source": None, **table[identity]}
        return real(frame)

    monkeypatch.setattr(rig.daemon, "_session_query", session)


# ------------------------------------------------------------------ [R70] the cold-at

def test_the_cold_at_is_written_per_tag_from_each_session_answer(rig, monkeypatch):
    """§3.13 test 19: a warm Claude x (age 600, ttl 3600), a cold Codex y (age 600 on a
    row of 300 s), an unbound z, a fresh f and a no-clock n, in tag order."""
    for slot, name in ((2, "x"), (3, "y"), (4, "f"), (5, "n")):
        rig.shim(slot=slot, name=name)
    answers(rig, monkeypatch, {
        "x": dict(adapter="claude-shim", cache_kind="warm", age_seconds=600,
                         cache_ttl=3600),
        "y": dict(adapter="codex", cache_kind="cold", age_seconds=600,
                         cache_ttl=300),
        "f": dict(adapter="claude-shim", cache_kind="fresh", age_seconds=None,
                         cache_ttl=None),
        "n": dict(adapter="pager", cache_kind="no-cache", age_seconds=None,
                         cache_ttl=None, cache_reason="comms has no cache clock for pager"
                         " agents"),
    })
    code, out = rig.cli(["send", "x,y,z,f,n", "hi", "--posture", "none", "--new-name"], **PLANNER,
                        clock=lambda: T_ASK)
    assert code == 0, out
    ml = machine_line.parse(last(rig).content)
    t = int(T_ASK)
    assert ml.recipients == ("x", "y", "z", "f", "n")
    assert ml.cold_at == (t + 3000, t - 300, t, "-", "-")
    assert f"[cold-at:{t + 3000},{t - 300},{t},-,-]" in ml.line
    # never later than the read's last turn plus its lifetime
    assert ml.cold_at[0] <= T_ASK - 600 + 3600 and ml.cold_at[1] <= T_ASK - 600 + 300
    assert not ml.held


def test_a_cold_read_with_no_lifetime_writes_the_ask_time(rig, monkeypatch):
    """A Codex or OpenCode `cold` with no lifetime (a model switch) is a "wait" reading:
    `floor(t_ask)`, a time already reached. So is a warm read with no lifetime."""
    rig.shim(slot=2, name="y")
    rig.shim(slot=3, name="w")
    answers(rig, monkeypatch, {
        "y": dict(adapter="codex", cache_kind="cold", age_seconds=600,
                         cache_ttl=None, cache_reason="the model changed"),
        "w": dict(adapter="codex", cache_kind="warm", age_seconds=10,
                         cache_ttl=None),
    })
    code, out = rig.cli(["send", "y,w", "hi", "--posture", "none"], **PLANNER,
                        clock=lambda: T_ASK)
    assert code == 0, out
    assert machine_line.parse(last(rig).content).cold_at == (int(T_ASK), int(T_ASK))


def test_a_send_that_reads_only_live_writes_every_value_as_dash(rig, monkeypatch):
    """Every recipient `fresh` or `no-cache` (no clock, a no-row model, the operator):
    the token is still written, every value `-`, so a line from this verb always carries
    the send's reading."""
    rig.shim(slot=2, name="f")
    rig.shim(slot=3, name="n")
    answers(rig, monkeypatch, {
        "f": dict(adapter="claude-shim", cache_kind="fresh", age_seconds=None,
                         cache_ttl=None),
        "n": dict(adapter="opencode", cache_kind="no-cache", age_seconds=None,
                         cache_ttl=None),
    })
    code, out = rig.cli(["send", "f,n", "hi", "--posture", "none"], **PLANNER)
    assert code == 0, out
    assert machine_line.parse(last(rig).content).cold_at == ("-", "-")


def test_a_held_send_writes_no_cold_at(rig):
    rig.shim(slot=2, name="x")
    code, out = rig.cli(["send", "x", "later", "--posture", "none", "--held"], **PLANNER)
    assert code == 0, out
    ml = machine_line.parse(last(rig).content)
    assert ml.held and ml.cold_at is None
    assert "[held]" in ml.line and "[next-turn]" not in ml.line and "cold-at" not in ml.line


def test_an_unbound_recipient_alone_gets_the_ask_time_and_the_not_bound_note(rig):
    for argv in (["send", "z", "hi", "--posture", "none", "--new-name"],
                 ["send", "z", "hi", "--posture", "none", "--held", "--new-name"]):
        code, out = rig.cli(argv, **PLANNER, clock=lambda: T_ASK)
        assert code == 0, out
        assert cli.NOT_BOUND_NOTE.format(name="z") in out.splitlines()
    plain, flagged = posted(rig)[-2:]
    assert machine_line.parse(plain.content).cold_at == (int(T_ASK),)
    assert machine_line.parse(flagged.content).cold_at is None


# ------------------------------------------------------------ [F7] reads comms cannot make

def test_an_unknown_read_refuses_the_send_held_or_not(rig, monkeypatch):
    rig.shim(slot=2, name="x")
    rig.shim(slot=3, name="w")
    answers(rig, monkeypatch, {
        "x": dict(adapter="claude-shim", cache_kind="unknown", age_seconds=None,
                         cache_ttl=None, cache_reason="no session id reported yet"),
    })
    before = posted(rig)
    for flags in ([], ["--held"]):
        code, out = rig.cli(["send", "x,w", "hi", "--posture", "none", *flags], **PLANNER)
        assert code == cli.EXIT_COLD, out
        assert out.splitlines() == [
            "Nothing was sent: comms could not read x's cache (no session id "
            "reported yet).",
            "To reach the other recipients now, send it again without x.",
            "Send it again in a moment; a read that succeeds holds or delivers it.",
            "Nothing was sent."]
    assert posted(rig) == before


def test_the_refusal_reasons(rig):
    from moxi_comms import cache

    def state(**kw):
        return cache.CacheState(target="reviewer", kind="unknown", **kw)

    assert cli._refusal_reason(state(adapter="codex", reason="transcript missing")) == \
        "comms could not read reviewer's cache (transcript missing)"
    assert cli._refusal_reason(state(reason=cache.NOT_BOUND)) == \
        "reviewer unbound while comms was checking it"
    assert cli._refusal_reason(state(reason="the session query failed (boom)")) == \
        "comms could not ask the daemon about reviewer (the session query failed (boom))"
    assert cli._hold_text([state(adapter="codex", reason="x")]).splitlines()[1:] == [
        "Send it again in a moment; a read that succeeds holds or delivers it.",
        "Nothing was sent."]


def test_a_cold_read_of_any_adapter_posts_and_holds(rig, monkeypatch):
    rig.shim(slot=2, name="y")
    answers(rig, monkeypatch, {
        "y": dict(adapter="codex", cache_kind="cold", age_seconds=4000,
                         cache_ttl=1800),
    })
    code, out = rig.cli(["send", "y", "hi", "--posture", "none"], **PLANNER)
    assert code == 0, out
    lines = out.splitlines()
    assert lines[1].startswith("held: y's cache went cold")
    assert lines[-1] == ("Sent and held: y reads this after its own next turn, or "
                         "when its session is cleared, compacted or restarted. Nothing "
                         "wakes it for this message alone.")


def test_held_to_a_bound_recipient_says_when_it_reads_it(rig):
    rig.shim(slot=2, name="x")
    code, out = rig.cli(["send", "x", "later", "--posture", "none", "--held"], **PLANNER)
    assert code == 0, out
    assert out.splitlines()[-1] == (
        "NOTE: x reads this after its own next turn, or when its session is "
        "cleared, compacted or restarted. Nothing wakes it for this message alone.")
    assert "held:" not in out


# ------------------------------------------------------------- [F21] --held to the operator

def operator_env(rig):
    return {"MOXI_COMMS_OPERATOR": rig.prov.operator_user_id}


def test_held_to_the_operator_alone_writes_no_held(rig):
    rig.shim(slot=4, name="operator", global_=True)
    code, out = rig.cli(["send", "operator", "fyi", "--posture", "none", "--held"],
                        **PLANNER)
    assert code == 0, out
    assert cli.HELD_IGNORED_OPERATOR in out.splitlines()
    ml = machine_line.parse(last(rig).content)
    assert not ml.held and ml.cold_at is not None, "an ordinary message, with its reading"


def test_held_to_the_operator_and_another_flags_the_line_and_says_so(rig):
    """§3.13 test 15: one post with `[held]`, the NOTE printed, the operator gets it live
    and x waits."""
    operator = rig.shim(slot=4, name="operator", global_=True)
    x = rig.shim(slot=2, name="x")
    code, out = rig.cli(["send", "operator,x", "both", "--posture", "none", "--held"],
                        **PLANNER)
    assert code == 0, out
    lines = out.splitlines()
    assert cli.HELD_IGNORED_OPERATOR in lines
    assert cli.HELD_NOTE.format(name="x") in lines
    assert not any(line.startswith("NOTE: operator reads") for line in lines)
    msg = last(rig)
    ml = machine_line.parse(msg.content)
    assert ml.held and ml.cold_at is None
    assert any(p["message_id"] == msg.id for p in wait_for_queue(operator, 1))
    assert wait_until(lambda: msg.id in queued(rig.daemon, "x"))
    assert msg.id not in [p["message_id"] for p in x.queue]


# ------------------------------------------------------------------ [R14] one version check

@pytest.mark.parametrize("answer", [{}, {"cli_protocol": 0}, {"cli_protocol": 1},
                                    {"cli_protocol": 3}])
def test_a_daemon_on_another_verb_protocol_refuses_every_send(rig, monkeypatch, answer):
    """§3.13 test 16: exit 4 with §3.10's text, nothing posted."""
    rig.shim(slot=2, name="x")
    real = DaemonClient.request

    def request(self, op, **kw):
        got = real(self, op, **kw)
        if op == "ping":
            got = {k: v for k, v in got.items() if k != "cli_protocol"} | answer
        return got

    monkeypatch.setattr(DaemonClient, "request", request)
    before = posted(rig)
    code, out = rig.cli(["send", "x", "hi", "--posture", "none"], **PLANNER)
    spoken = answer.get("cli_protocol", 0)
    assert (code, out.strip()) == (cli.EXIT_OFFLINE, (
        f"the comms daemon speaks verb protocol {spoken} and this comms speaks 2; restart "
        "the daemon (moxi comms service status names its unit), then run this again. Nothing "
        "was sent."))
    assert posted(rig) == before
    code, out = rig.cli(["agents"], **PLANNER)
    assert code == cli.EXIT_OFFLINE and "speaks verb protocol" in out


@pytest.mark.parametrize("side", ["sending", "daemon"])
def test_a_verb_and_a_daemon_from_either_side_of_cache_code_refuse_each_other(
        rig, monkeypatch, side):
    """build verification b15-1 [L11]: the session answer gained `cache_code` at verb
    protocol 2. An old verb (1) on a new daemon (2), and a new verb (2) on an old daemon
    (1), each exit 4 with nothing sent, so neither reads the other's session answer."""
    rig.shim(slot=2, name="x")
    monkeypatch.setattr(f"moxi_comms.{side}.CLI_PROTOCOL", 1)
    daemon, verb = (2, 1) if side == "sending" else (1, 2)
    before = posted(rig)
    code, out = rig.cli(["send", "x", "hi", "--posture", "none"], **PLANNER)
    assert (code, out.strip()) == (cli.EXIT_OFFLINE, (
        f"the comms daemon speaks verb protocol {daemon} and this comms speaks {verb}; "
        "restart the daemon (moxi comms service status names its unit), then run this "
        "again. Nothing was sent."))
    assert posted(rig) == before
    code, out = rig.cli(["agents"], **PLANNER)
    assert code == cli.EXIT_OFFLINE and "speaks verb protocol" in out


def test_ping_answers_the_verb_protocol_and_register_the_claims_one(rig):
    from moxi_comms.protocol import CLI_PROTOCOL, PROTOCOL_VERSION

    c = DaemonClient(daemon_socket()).connect()
    try:
        assert c.request("ping") == {"id": 1, "ok": True, "cli_protocol": CLI_PROTOCOL}
    finally:
        c.close()
    assert (CLI_PROTOCOL, PROTOCOL_VERSION) == (2, 1)


# --------------------------------------------------------------- [R10] the held frame mark

def test_the_held_mark_says_when_it_was_sent_and_how_long_it_waited():
    """§3.13 test 14."""
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 3600 - 30))
    assert held_mark(stamp, now=now) == f"[held] sent {sent_at(stamp, now=now)}, waited 3h"
    assert held_mark("not a time", now=now) == "[held]"
    payload = {"message_id": "4611686018427391719", "room": "#names",
               "author": {"name": "core-lead", "tier": "chosen", "attribution": "verified"},
               "posture": "once", "timestamp": stamp, "held": True}
    line = frame_line(payload, now=now)
    assert line.endswith(f"· [held] sent {sent_at(stamp, now=now)}, waited 3h · "
                         "[reply-once]")
    assert "[held]" not in frame_line({**payload, "held": False}, now=now)
    assert "[next-turn]" not in frame_line({**payload, "next_turn": True}, now=now)
