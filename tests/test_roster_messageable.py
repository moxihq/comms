"""`moxi comms agents` and the session-start roster show the names an agent can message.

[F30], design §7.3 with [R43]-[R46], against the `agents` answer of §3.11:
`{"bindings": [...], "waiting": {name: {"count", "oldest"}}, "pending": {},
"unanswered": [...], "keeps": {...}}`. The daemon's half of that answer is the queue
slice's; these tests hand the renderers a fake answer of that shape, except the last
section, which asks the daemon itself.

The default view (the verb and the snapshot) is one row per live binding whose adapter is
not `send-only`, and nothing else. `--seats` is the operator's view: every binding with
its slot, one waiting block, the unanswered rows and the slot keeps. The operator is a
row like any other. The deltas never name a send-only binding, nor announce one leaving
from a memory written before.
"""

from __future__ import annotations

import json

from conftest import wait_until

from moxi_comms import cli, roster_view
from moxi_comms.adapters import codex, opencode, shim_roster
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import SEND_ONLY_ADAPTER
from moxi_comms.paths import daemon_socket

NOW = 1_800_000_000.0
DAY, HOUR = 86400, 3600

CONSOLE = {"qualified": "console@acme", "name": "console", "scope": "acme",
           "tier": "standing", "adapter": "send-only", "slot": None}
OPERATOR = {"qualified": "operator", "name": "operator", "scope": "global",
            "tier": "standing", "adapter": "claude-shim", "slot": 4}
EPHEMERAL = {"qualified": "agent-3", "name": "agent-3", "scope": "global",
             "tier": "ephemeral", "adapter": "claude-shim", "slot": 3}
REVIEWER = {"qualified": "reviewer@my-project", "name": "reviewer", "scope": "my-project",
            "tier": "chosen", "adapter": "codex", "slot": 1}


def answer(bindings=(CONSOLE, OPERATOR, EPHEMERAL, REVIEWER), **over):
    """An `agents` answer in §3.11's one shape."""
    info = {
        "bindings": [dict(b) for b in bindings],
        "waiting": {"reviewer@my-project": {"count": 3, "oldest": NOW - (2 * DAY + 3 * HOUR)},
                    "curator": {"count": 1, "oldest": NOW - 42 * 60}},
        "pending": {},
        "unanswered": [{"owing": "agent-3", "author": "operator", "message_id": "9001",
                        "for": 40 * 60}],
        "keeps": {"2": {"qualified": "planner", "expires_in": 90.0}},
    }
    info.update(over)
    return info


DEFAULT = ("operator                 standing   adapter claude-shim\n"
           "agent-3                  ephemeral  adapter claude-shim\n"
           "reviewer@my-project      chosen     adapter codex")


# ------------------------------------------------------------------- the default view

def test_the_default_view_is_the_messageable_names_and_nothing_else():
    """[R43]: one row per live binding that is not send-only, `agent-N` and the
    operator included; no send-only row, no waiting mail, no unanswered rows, no keeps,
    no voice line, no slot."""
    out = roster_view.render_agents(answer(), now=NOW)
    assert out == DEFAULT
    for absent in ("console", "send-only", "waiting", "curator", "unanswered", "9001",
                   "slot", "keeps", "voice", "pending", "held"):
        assert absent not in out, absent


def test_the_send_only_kind_is_the_daemons():
    assert roster_view.SEND_ONLY_ADAPTER == SEND_ONLY_ADAPTER


def test_a_roster_of_only_send_only_rows_is_the_empty_roster():
    out = roster_view.render_agents(answer(bindings=(CONSOLE,)), now=NOW)
    assert out == "no live bindings — nothing is addressable right now."
    assert roster_view.render_agents(answer(bindings=()), now=NOW) == out


def test_the_default_view_reads_bindings_alone():
    """The snapshot and the delta render the default view, which needs nothing but
    `bindings`: an answer with no other key renders."""
    assert roster_view.render_agents({"bindings": [OPERATOR]}) == (
        "operator                 standing   adapter claude-shim")


def test_the_operator_is_absent_when_unbound():
    out = roster_view.render_agents(answer(bindings=(EPHEMERAL,)), now=NOW)
    assert "operator" not in out


# ------------------------------------------------------------------- --seats

SEATS = (
    "console@acme             standing   no slot  adapter send-only\n"
    "operator                 standing   slot 4   adapter claude-shim\n"
    "agent-3                  ephemeral  slot 3   adapter claude-shim\n"
    "reviewer@my-project      chosen     slot 1   adapter codex\n"
    "\n"
    "waiting (queued for each name until its next session start, /clear, compaction, or "
    "a turn of its own on a warm cache):\n"
    "  curator                  1 message(s), oldest waiting 42m\n"
    "  reviewer@my-project      3 message(s), oldest waiting 2d 3h\n"
    "unanswered: agent-3 owes operator msg 9001, 40m\n"
    "\n"
    "slot keeps (held for a name a restart has not brought back yet):\n"
    "  slot 2   planner                  90s left")


def test_seats_is_every_binding_then_one_waiting_block_then_unanswered_then_keeps():
    """[R45]: send-only rows included as `no slot`; the waiting block's rows come from
    `waiting` (count, and the oldest's wait from `now - oldest`)."""
    assert roster_view.render_agents(answer(), seats=True, now=NOW) == SEATS


def test_the_waiting_header_says_what_a_row_means():
    assert roster_view.WAITING_HEADER == (
        "waiting (queued for each name until its next session start, /clear, compaction, "
        "or a turn of its own on a warm cache):")


def test_the_old_headers_and_rows_are_gone():
    out = roster_view.render_agents(answer(), seats=True, now=NOW)
    assert "pending (waiting in #names for a matching bind):" not in out
    assert "held for next turn" not in out


def test_no_waiting_block_when_nothing_waits():
    out = roster_view.render_agents(answer(waiting={}, unanswered=[], keeps={}),
                                    seats=True, now=NOW)
    assert out == ("console@acme             standing   no slot  adapter send-only\n"
                   "operator                 standing   slot 4   adapter claude-shim\n"
                   "agent-3                  ephemeral  slot 3   adapter claude-shim\n"
                   "reviewer@my-project      chosen     slot 1   adapter codex")


def test_now_defaults_to_the_wall_clock(monkeypatch):
    monkeypatch.setattr(roster_view.time, "time", lambda: NOW + HOUR)
    out = roster_view.render_agents(answer(), seats=True)
    assert "  curator                  1 message(s), oldest waiting 1h 42m" in out


def test_seats_reads_waiting_and_not_pending():
    """`pending` is always empty in the answer and nothing renders it."""
    out = roster_view.render_agents(answer(pending={"ghost": 7}), seats=True, now=NOW)
    assert "ghost" not in out


# ------------------------------------------------------------------- the snapshot

def test_the_snapshot_body_is_the_default_view():
    envelope, online = shim_roster.render_snapshot(answer(), env={})
    assert online
    assert envelope.split("\n\n", 2)[2] == f"{shim_roster.LABEL}\n{DEFAULT}"


def test_codex_and_opencode_layer_one_carry_the_same_view():
    for module in (codex, opencode):
        text = module.layer_one_text(answer())
        assert text.endswith(f"{shim_roster.LABEL}\n{DEFAULT}"), module.__name__
        assert "console@acme" not in text


def test_the_race_line_tests_the_filtered_list():
    """[R46]: a roster of only send-only rows is empty to the session, so a stamped
    session still gets the race line."""
    env = {"MOXI_COMMS_NAME": "rev"}
    envelope, _ = shim_roster.render_snapshot(answer(bindings=(CONSOLE,)), env=env)
    assert envelope.endswith(shim_roster.OWN_BINDING_RACE.format(name="rev"))
    envelope, _ = shim_roster.render_snapshot(answer(bindings=(CONSOLE, OPERATOR)), env=env)
    assert "may not have completed registration" not in envelope


def test_the_verb_and_the_snapshot_print_the_same_from_one_answer(rig, monkeypatch):
    """Snapshot equals verb, through the real CLI path and the real hook, both handed
    the same §3.11 answer."""
    real = DaemonClient.query

    def query(self, kind, **kw):
        return answer() if kind == "agents" else real(self, kind, **kw)

    monkeypatch.setattr(DaemonClient, "query", query)
    code, out = rig.cli(["agents"], slot=1)
    assert code == 0
    envelope, online = shim_roster.snapshot(daemon_socket(), env={})
    assert online and envelope.split("\n\n", 2)[2] == f"{shim_roster.LABEL}\n{out.rstrip()}"
    assert out.rstrip("\n") == DEFAULT


# ------------------------------------------------------------------- usable

#: `shim_roster.usable` as it was before this round, verbatim: what a Codex or OpenCode
#: adapter started before the deploy still runs (design §7.3, "Pre-round adapters").
def pre_round_usable(info) -> bool:
    return (isinstance(info, dict) and isinstance(info.get("bindings"), list)
            and isinstance(info.get("pending"), dict))


def test_usable_takes_an_answer_with_bindings_and_no_pending():
    assert shim_roster.usable({"bindings": []})
    assert shim_roster.usable({"bindings": [OPERATOR], "waiting": {}})
    assert shim_roster.usable(answer())


def test_usable_refuses_what_the_default_view_cannot_render():
    for info in (None, [], {}, {"pending": {}}, {"bindings": {}}, {"bindings": None},
                 {"error": "unknown query"}):
        assert not shim_roster.usable(info), info


def test_a_pre_round_usable_accepts_the_answer_because_it_keeps_an_empty_pending():
    """Verification fold-1 re-check MINOR-1, [R63], §3.11: the answer keeps `pending: {}`
    so an adapter started before the deploy renders its roster at /clear, /compact and
    resume instead of the offline line. Without the key, the old check fails."""
    info = answer()
    assert info["pending"] == {}
    assert pre_round_usable(info)
    del info["pending"]
    assert not pre_round_usable(info), "why the key is kept"


# ------------------------------------------------------------------- the deltas

def test_roster_of_drops_send_only_bindings():
    assert shim_roster.roster_of(answer()) == {
        "operator": {"tier": "standing", "adapter": "claude-shim"},
        "agent-3": {"tier": "ephemeral", "adapter": "claude-shim"},
        "reviewer@my-project": {"tier": "chosen", "adapter": "codex"}}


def fake_fetch(monkeypatch, info):
    monkeypatch.setattr(shim_roster, "fetch", lambda *a, **k: info)


def test_a_delta_never_names_a_send_only_join(monkeypatch, tmp_path):
    state = tmp_path / "roster.json"
    shim_roster.write_state(state, {})
    fake_fetch(monkeypatch, answer(bindings=(CONSOLE, REVIEWER)))
    envelope, online = shim_roster.delta(None, state, env={})
    assert online and envelope == (
        "(comms roster update: reviewer@my-project [chosen] is now reachable — "
        "`moxi comms agents` is always current.)")
    assert "console" not in state.read_text(), "the memory holds messageable names only"


def test_a_memory_written_before_this_build_announces_no_send_only_leave(monkeypatch,
                                                                         tmp_path):
    """The remembered side is filtered too: an old memory holding a send-only row whose
    binding is still live, or gone, announces nothing about it."""
    state = tmp_path / "roster.json"
    old = {"console@acme": {"tier": "standing", "adapter": "send-only"},
           "reviewer@my-project": {"tier": "chosen", "adapter": "codex"}}
    for live in ((CONSOLE, REVIEWER), (REVIEWER,)):
        shim_roster.write_state(state, old)
        fake_fetch(monkeypatch, answer(bindings=live))
        envelope, online = shim_roster.delta(None, state, env={})
        assert online and envelope is None, live


def test_an_operator_join_and_leave_ride_the_delta_like_any_name(monkeypatch, tmp_path):
    """[R44]."""
    state = tmp_path / "roster.json"
    shim_roster.write_state(state, {})
    fake_fetch(monkeypatch, answer(bindings=(OPERATOR,)))
    envelope, _ = shim_roster.delta(None, state, env={})
    assert envelope == ("(comms roster update: operator [standing] is now reachable — "
                        "`moxi comms agents` is always current.)")
    fake_fetch(monkeypatch, answer(bindings=()))
    envelope, _ = shim_roster.delta(None, state, env={})
    assert envelope == "(comms roster update: operator left — `moxi comms agents` is always current.)"
    assert json.loads(state.read_text()) == {}


def test_the_unanswered_line_still_rides_the_authors_delta(monkeypatch, tmp_path):
    """`UNANSWERED_LINE` is not roster content: it stays, reading `unanswered`."""
    state = tmp_path / "roster.json"
    shim_roster.write_state(state, shim_roster.roster_of(answer()))
    fake_fetch(monkeypatch, answer())
    envelope, _ = shim_roster.delta(None, state, env={"MOXI_COMMS_NAME": "operator",
                                                      "MOXI_COMMS_GLOBAL": "1"})
    assert envelope == "(comms: agent-3 ended a turn without answering your msg 9001.)"


# ------------------------------------------------------------------- help

def _agents_help():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    verb = next(c.help for c in sub._choices_actions if c.dest == "agents")
    seats = next(a.help for a in sub.choices["agents"]._actions if "--seats" in a.option_strings)
    return verb, seats


def test_the_agents_help_says_who_you_can_message():
    assert _agents_help() == (
        "who you can message",
        "operator view: also show send-only identities, each binding's slot, and the mail "
        "waiting for each name")


# ------------------------------------------------------------------- the daemon's answer

def test_the_daemons_answer_keeps_an_empty_pending_beside_waiting(rig):
    """MINOR-1 against the daemon itself: with mail waiting for an unbound name, the
    answer counts it under `waiting`, keeps `pending` empty, and a pre-round `usable`
    accepts it. (The answer is the queue slice's, §3.11.)"""
    rig.shim(slot=1, name="planner", scope="proj-a")
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a",
                         "waiting"], slot=1, name="planner", scope="proj-a")
    assert code == 0, out

    def info():
        c = DaemonClient(daemon_socket()).connect()
        try:
            return c.query("agents")
        finally:
            c.close()

    assert wait_until(lambda: "curator@proj-a" in (info().get("waiting") or {}))
    got = info()
    assert got["pending"] == {}
    assert got["waiting"]["curator@proj-a"]["count"] == 1
    assert pre_round_usable(got) and shim_roster.usable(got)
