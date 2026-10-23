"""comms' interface 1 (`moxi_comms.api`): the announcement, the pins, and the library's
send against a live rig.

The pins hold every public name and the send's signature, so a change to the interface
fails here first. The rig tests run on the local backend: the library opens the box's
record by the config, as a caller outside the rig does, and the rig's record is that
file.

`moxi comms send` renders what the library returns, so the CLI's suites read
`lines()` already; the tests here read the data beside it and hold `lines()` to the
verb's output for the same send.
"""

from __future__ import annotations

import inspect
import re

import pytest
from conftest import wait_for_queue, write_config

from moxi import modules
from moxi_comms import adapter, api, cli, module, sending
from moxi_comms.api import tokens as api_tokens
from moxi_comms.claimhold import ClaimServer

R_SCOPED = "reviewer@proj-a"


@pytest.fixture
def backend():
    """The library opens the box's record by the config: the local backend's file."""
    return "local"


# --------------------------------------------------------------------------- the pins

def test_the_announcement_offers_interface_1():
    assert module.INTERFACES == frozenset({1})
    assert module.API == "moxi_comms.api"
    assert api.INTERFACE == 1


def test_load_finds_comms_and_its_api():
    modules.reset()
    found = modules.load("comms", interface=1)
    assert found.ok, found
    assert found.api is api
    modules.reset()
    assert not modules.load("comms", interface=2).ok


def test_interface_1_holds_its_names():
    assert set(api.__all__) == {
        "INTERFACE", "send", "check", "Sender", "SendResult", "Recipient", "Refusal",
        "OUTCOMES", "DELIVERIES", "EXIT_CODES", "POSTURES", "ClaimServer", "attributed",
        "session", "roster", "owner", "Roster", "Agent", "Standing", "Owner",
        "record_dir", "daemon_socket", "GLOBAL", "qualify", "split_tag", "NAME_RE",
        "replay", "DaemonError", "DaemonOffline"}
    for name in api.__all__:
        assert hasattr(api, name), name
    assert api.ClaimServer is ClaimServer
    assert api.GLOBAL == "global" and api.qualify("lead", "proj-a") == "lead@proj-a"
    assert api.split_tag("lead@proj-a") == ("lead", "proj-a")
    assert api.NAME_RE.match("lead") and not api.NAME_RE.match("a b")


def test_the_send_and_check_signatures():
    for fn in (api.send, api.check):
        params = inspect.signature(fn).parameters
        assert list(params) == ["sender", "to", "text", "posture", "reply_to", "new",
                                "audience", "held", "cap", "no_cap", "deadline",
                                "standing", "new_name"], fn
        assert all(params[n].kind is inspect.Parameter.KEYWORD_ONLY
                   for n in list(params)[3:])
        assert params["posture"].default is inspect.Parameter.empty
        assert params["deadline"].default == 30.0
        assert [params[n].default for n in ("reply_to", "new", "audience", "held", "cap",
                                            "no_cap", "standing", "new_name")] == [
            None, False, None, False, None, False, False, False]


def test_the_outcomes_deliveries_and_exit_codes():
    assert api.OUTCOMES == ("sent", "would-send", "refused", "cache-unread", "blocked",
                            "offline", "failed")
    assert api.EXIT_CODES == {"sent": 0, "would-send": 0, "refused": 2, "blocked": 3,
                              "offline": 4, "failed": 5, "cache-unread": 6}
    assert api.EXIT_CODES["cache-unread"] == cli.EXIT_COLD
    assert api.DELIVERIES == ("live", "held-cold", "held-flag", "waiting", "unbound",
                              "operator")
    assert [f.name for f in api.SendResult.__dataclass_fields__.values()] == [
        "outcome", "message_id", "recipients", "refusal", "notes", "said"]
    assert list(api.Recipient.__dataclass_fields__) == ["qualified", "delivery",
                                                        "cold_at", "reason"]
    assert list(api.Refusal.__dataclass_fields__) == ["code", "text"]


def test_waiting_has_one_meaning_bound_with_no_endpoint():
    """One meaning of `waiting`: a bound session with no endpoint. A name no
    session holds is `unbound`, its own value."""
    doc = inspect.getsource(sending).split("DELIVERY_LIVE, DELIVERY_HELD_COLD")[0]
    assert "waiting    the recipient is bound and its session has no endpoint" in doc
    assert "unbound    no session holds the name" in doc


def test_the_reads_and_names_are_pinned():
    for name, params in {"attributed": ["message_id", "deadline"],
                         "session": ["identity", "deadline"],
                         "roster": ["deadline"], "owner": ["session_id", "deadline"],
                         "record_dir": [], "daemon_socket": [], "replay": []}.items():
        assert list(inspect.signature(getattr(api, name)).parameters) == params, name
    assert list(api.Agent.__dataclass_fields__) == ["qualified", "name", "scope", "tier",
                                                    "adapter", "slot", "directors"]
    assert list(api.Standing.__dataclass_fields__) == ["qualified", "name", "scope",
                                                       "kind"]
    assert list(api.Roster.__dataclass_fields__) == ["bound", "standing"]
    assert list(api.Owner.__dataclass_fields__) == ["session_id", "identity", "adapter",
                                                    "directors", "awaiting"]


def test_api_tokens_holds_the_store_calls():
    from moxi_comms import tokens

    assert set(api_tokens.__all__) == {"TokenStore", "TokenOwner", "owner_for_role",
                                       "TokenStoreError"}
    assert api_tokens.TokenStore is tokens.TokenStore
    assert api_tokens.TokenOwner is tokens.TokenOwner
    assert api_tokens.owner_for_role is tokens.owner_for_role
    assert api_tokens.TokenStoreError is tokens.TokenStoreError


def test_a_box_role_keys_under_its_own_name(env):
    write_config(None, '[secrets]\nroles = ["calendar"]\n')
    owner = api_tokens.owner_for_role("calendar")
    assert owner.key == "calendar"
    assert api_tokens.owner_for_role("not-listed").key == ""


def test_the_adapter_surface():
    from moxi_comms import attribution, client, cursors, machine_line
    from moxi_comms.adapters import claude_shim

    assert set(adapter.__all__) == {
        "DaemonClient", "DaemonError", "DaemonOffline", "backoff_delays", "cursors",
        "render_digest", "frame_line", "TIER_EPHEMERAL", "TIER_CHOSEN", "TIER_STANDING",
        "TIER_OPERATOR", "TIER_UNKNOWN", "VERIFIED", "LABELED", "UNVERIFIED",
        "parse_machine_line", "MachineLine"}
    assert adapter.DaemonClient is client.DaemonClient
    assert adapter.backoff_delays is client.backoff_delays
    assert adapter.cursors is cursors
    assert adapter.render_digest is claude_shim.render_digest
    assert adapter.frame_line is claude_shim.frame_line
    assert adapter.parse_machine_line is machine_line.parse
    assert adapter.TIER_STANDING == attribution.TIER_STANDING
    assert adapter.UNVERIFIED == attribution.UNVERIFIED


def test_a_bad_argument_raises_as_the_parser_would():
    with pytest.raises(TypeError):
        api.send("lead", "reviewer", "x", posture="none")
    with pytest.raises(ValueError):
        api.send(api.Sender.session(), "reviewer", "x", posture="maybe")
    with pytest.raises(ValueError):
        api.send(api.Sender.session(), None, "x", posture="none")


def test_the_sender_forms_name_the_process_the_verb_would_see(tmp_path):
    base = {"HOME": "/h", "MOXI_COMMS_CLAIM_SOCKET": "/old"}
    assert api.Sender.session().environ(base) == base
    assert api.Sender.socket(tmp_path / "s.sock").environ(base)[
        "MOXI_COMMS_CLAIM_SOCKET"] == str(tmp_path / "s.sock")
    op = api.Sender.operator("900001").environ(base)
    assert op["MOXI_COMMS_OPERATOR"] == "900001" and "MOXI_COMMS_CLAIM_SOCKET" not in op

    class Server:
        socket_path = tmp_path / "srv.sock"

    assert api.Sender.claim(Server()) == api.Sender("claim", path=str(Server.socket_path))


def test_the_deadline_reaches_the_daemon_call_and_the_records_busy_timeout(env):
    ctx = sending.Context(deadline=2.5)
    seen = {}

    class Client:
        def __init__(self, socket_path=None, timeout=10.0):
            seen["timeout"] = timeout

        def connect(self):
            raise api.DaemonOffline("down")

    real = sending.DaemonClient
    sending.DaemonClient = Client
    try:
        with pytest.raises(api.DaemonOffline):
            ctx.daemon()
    finally:
        sending.DaemonClient = real
    assert seen["timeout"] == 2.5
    assert ctx.gateway.busy_timeout_ms == 2500
    assert sending.Context().gateway.busy_timeout_ms == 5000


# --------------------------------------------------------------------------- the rig

def _sender(rig, name, slot, scope=None):
    environ = {"MOXI_COMMS_PROJECT": "proj-a", "MOXI_COMMS_NAME": name,
               "MOXI_COMMS_SLOT": str(slot)}
    if scope:
        environ["MOXI_COMMS_SCOPE"] = scope
    from conftest import _stamp_of
    return _stamp_of(environ)


def _ids_out(lines):
    return [re.sub(r"\b\d{6,}\b", "<id>", line) for line in lines]


def test_a_send_returns_its_data_and_the_verbs_lines(rig):
    rig.shim(slot=1, name="planner")
    reviewer = rig.shim(slot=2, name="reviewer")
    code, out = rig.cli(["send", "reviewer", "first", "--posture", "once"], slot=1,
                        name="planner")
    assert code == 0, out
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        got = api.send(api.Sender.socket(path), "reviewer", "second", posture="once")
    assert got.outcome == "sent" and got.exit_code == 0 and got.posted, got
    assert got.refusal is None and got.notes == ()
    assert got.message_id and got.message_id in got.lines()[0]
    assert [(r.qualified, r.delivery) for r in got.recipients] == [("reviewer", "live")]
    assert got.recipients[0].reason is None
    assert _ids_out(got.lines()) == _ids_out(out.splitlines())
    wait_for_queue(reviewer, 2)
    assert [p["text"] for p in reviewer.queue] == ["first", "second"]


def test_a_refusal_is_data_with_a_code(rig):
    rig.shim(slot=1, name="planner")
    rig.shim(slot=2, name="reviewer")
    before = len(rig.gateway.history(rig.names_channel))
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        got = api.send(api.Sender.socket(path), "reviewer", "x", posture=None)
    assert got.outcome == "refused" and got.exit_code == 2 and not got.posted
    assert got.refusal.code == "posture-required"
    assert got.refusal.text == cli.POSTURE_REQUIRED
    assert got.lines() == [cli.POSTURE_REQUIRED]
    assert got.recipients == ()
    assert len(rig.gateway.history(rig.names_channel)) == before


def test_check_posts_nothing_and_says_what_the_send_would(rig):
    rig.shim(slot=1, name="planner")
    before = len(rig.gateway.history(rig.names_channel))
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        got = api.check(api.Sender.socket(path), "later-agent", "x", posture="none",
                        new_name=True)
    assert got.outcome == "would-send" and got.exit_code == 0 and not got.posted
    assert [(r.qualified, r.delivery) for r in got.recipients] == [
        ("later-agent", "unbound")]
    assert got.recipients[0].reason == cli.NOT_BOUND_NOTE.format(name="later-agent")
    assert got.notes == (cli.NOT_BOUND_NOTE.format(name="later-agent"),)
    assert got.lines() == ["would send 1 message(s) to later-agent",
                           cli.NOT_BOUND_NOTE.format(name="later-agent")]
    assert len(rig.gateway.history(rig.names_channel)) == before


def test_a_held_send_says_held_flag_per_recipient(rig):
    rig.shim(slot=1, name="planner")
    rig.shim(slot=2, name="reviewer")
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        got = api.send(api.Sender.socket(path), ["reviewer"], "later", posture="none",
                       held=True)
    assert got.outcome == "sent", got
    (r,) = got.recipients
    assert (r.delivery, r.cold_at) == ("held-flag", None)
    assert r.reason == cli.HELD_NOTE.format(name="reviewer")


def test_the_never_bound_refusal_and_new_name(rig):
    rig.shim(slot=1, name="planner")
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        refused = api.send(api.Sender.socket(path), "nobody-yet", "x", posture="none")
        sent = api.send(api.Sender.socket(path), "nobody-yet", "x", posture="none",
                        new_name=True)
    assert (refused.outcome, refused.refusal.code) == ("refused", "never-bound")
    assert sent.outcome == "sent" and sent.recipients[0].delivery == "unbound"


def test_a_reply_by_message_id_goes_to_the_author(rig):
    planner = rig.shim(slot=1, name="planner")
    rig.shim(slot=2, name="reviewer")
    with rig.session_holding(_sender(rig, "reviewer", 2)) as path:
        ask = api.send(api.Sender.socket(path), "planner", "q?", posture="once")
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        got = api.send(api.Sender.socket(path), None, "a", posture=None,
                       reply_to=ask.message_id)
    assert got.outcome == "sent", got.lines()
    assert got.lines()[0].startswith("replying to reviewer")
    assert [r.qualified for r in got.recipients] == ["reviewer"]
    wait_for_queue(planner, 1)
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        missing = api.send(api.Sender.socket(path), None, "a", posture=None,
                           reply_to="4611686018427399999")
    assert (missing.outcome, missing.refusal.code) == ("refused", "no-such-message")


def test_a_claim_server_sends_as_its_binding(rig):
    reviewer = rig.shim(slot=2, name="reviewer")
    server = ClaimServer("planner", "global", kind="claude-shim", slot=1, global_=True)
    server.holder.ever_held = True
    server.set_offline()
    server.start()
    try:
        got = api.send(api.Sender.claim(server), "reviewer", "hi", posture="none")
    finally:
        server.stop()
    assert got.outcome == "sent", got.lines()
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["author"]["name"] == "planner"


def test_no_identity_and_the_offline_daemon_are_refusals(rig):
    got = api.send(api.Sender.session(), "reviewer", "x", posture="none")
    assert (got.outcome, got.refusal.code) == ("refused", "no-identity")
    rig.shim(slot=1, name="planner")
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        rig.daemon.stop()
        got = api.send(api.Sender.socket(path), "reviewer", "x", posture="none")
    assert (got.outcome, got.exit_code, got.refusal.code) == ("offline", 4, "offline")


def test_the_operator_lane(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    got = api.send(api.Sender.operator(rig.prov.operator_user_id), R_SCOPED, "ship it",
                   posture=None)
    assert got.outcome == "sent", got.lines()
    assert [(r.qualified, r.delivery) for r in got.recipients] == [(R_SCOPED, "live")]
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["author"]["name"] == "operator"


def test_a_config_that_does_not_load_is_a_refusal(rig):
    rig.shim(slot=1, name="planner")
    write_config(None, "[threads]\nreply_cap = 0\n")
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        got = api.send(api.Sender.socket(path), "reviewer", "x", posture="open")
    assert (got.outcome, got.refusal.code) == ("refused", "config"), got.lines()


# --------------------------------------------------------------------------- the reads

def test_attributed_and_session(rig):
    rig.shim(slot=1, name="planner")
    reviewer = rig.shim(slot=2, name="reviewer")
    with rig.session_holding(_sender(rig, "planner", 1)) as path:
        sent = api.send(api.Sender.socket(path), "reviewer", "hello", posture="none")
    wait_for_queue(reviewer, 1)
    got = api.attributed(sent.message_id)
    assert got["text"] == "hello" and got["author"]["name"] == "planner"
    assert api.attributed("4611686018427399999") is None
    s = api.session("reviewer")
    assert s["bound"] is True and s["adapter"] == "claude-shim"
    assert s["session_id"] == reviewer.session_id
    assert api.session("nobody")["bound"] is False


def test_roster_and_owner_carry_declared_directors(rig):
    reviewer = rig.shim(slot=2, name="reviewer", directors=("main@proj-a",))
    with rig.session_holding(_sender(rig, "reviewer", 2)):
        # A live session's claim socket answers, so its claim file is live.
        got = api.roster()
        owner = api.owner(reviewer.session_id)
    (agent,) = [a for a in got.bound if a.qualified == "reviewer"]
    assert (agent.slot, agent.adapter, agent.directors) == (2, "claude-shim",
                                                            ("main@proj-a",))
    assert {s.qualified for s in got.standing} >= {"main@proj-a", "main@proj-b",
                                                   "herald"}
    assert (owner.identity, owner.adapter, owner.directors) == (
        "reviewer", "claude-shim", ("main@proj-a",))
    assert api.owner("no-such-session").identity is None
    with pytest.raises(api.DaemonError):
        api.owner("")


def test_the_paths_and_the_replay_bound(env, tmp_path):
    from moxi_comms import paths

    assert api.daemon_socket() == paths.daemon_socket()
    assert api.record_dir() == paths.record_db().parent
    assert api.replay() == (50, 24 * 3600)
    write_config(None, f'[local]\nrecord = "{tmp_path / "r" / "record.db"}"\n'
                       "[replay]\nmessages = 7\nhours = 2\n")
    assert api.record_dir() == tmp_path / "r"
    assert api.replay() == (7, 7200)



@pytest.mark.parametrize("to", [[], "", " , "])
def test_a_send_that_names_no_recipient_is_refused_before_anything_posts(to):
    with pytest.raises(ValueError, match="at least one recipient"):
        api.send(api.Sender.operator("u1"), to, "hello", posture=None)
