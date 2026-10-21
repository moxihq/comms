"""Send-only identities: `console@acme`, a standing row of kind `send-only` that a
module's own process holds as a live binding, sends through its claim socket and is
never a recipient, bound or not. On both backends."""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import queued, wait_until, warm_session, write_config, write_transcript
from moxi_comms import attribution, cli, directory, machine_line, provision
from moxi_comms.claimhold import CLAIM_SOCKET_ENV, NOT_INSIDE, ClaimServer, call
from moxi_comms.client import DaemonClient, DaemonError
from moxi_comms.gateway import Message
from moxi_comms.mock_gateway import NO_MENTIONS
from moxi_comms.paths import daemon_socket, directory_db
from moxi_comms.sender import SenderIdentity, credential_for

HOUR = 3600
CONSOLE = "console@acme"
PLANNER = "planner@proj-a"
#: The words the refusal names the console with: config.toml's `[policy] labels`.
LABEL = "the build console's send-only identity"
REFUSAL = (f"console@acme is {LABEL} and receives no "
           "mail; nothing was sent. A brief that wants a report names whom to report to.")
#: A sender for the verbs: a name no adapter binds, sent as from a session holding it
#: offline on its own slot (the rig's `session_holding`).
LEAD = dict(name="lead", scope="proj-a", slot=5)


# --------------------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def console_label(env):
    """The box names its console in `[policy] labels`, as the operator's config does."""
    write_config(None, f'[policy.labels]\n"{CONSOLE}" = "{LABEL}"\n')


def provision_console(rig, out=None):
    """The provisioner's act, as the lead runs it at land: the subcommand, against the
    rig's gateway and store."""
    lines = out if out is not None else []
    code = provision.send_only_main(["console@acme"], gateway=rig.gateway,
                                    store=rig.store, out=lines.append)
    assert code == 0, lines
    return lines


@pytest.fixture
def console(rig):
    """`console@acme` provisioned send-only, nobody holding it."""
    provision_console(rig)
    return rig


@pytest.fixture
def server(console):
    """A send-only console's binding: a `ClaimServer` with its own daemon arm."""
    srv = ClaimServer("console", "acme", kind="send-only", arm=True).start()
    assert wait_until(lambda: CONSOLE in console.daemon.bindings), srv.refusal
    yield srv
    srv.stop()
    wait_until(lambda: CONSOLE not in console.daemon.bindings)


class Pushes:
    """A raw send-only binding of console that records every frame the daemon pushes."""

    def __init__(self):
        self.frames = []
        self.client = DaemonClient(daemon_socket(), on_push=self.frames.append).connect()
        self.reply = self.client.register(slot="next-free", name="console",
                                          scope="acme", project="acme",
                                          adapter="send-only")

    def delivers(self):
        return [f for f in self.frames if f.get("op") == "deliver"]

    def close(self):
        self.client.close()


@pytest.fixture
def pushes(console):
    p = Pushes()
    yield p
    p.close()
    wait_until(lambda: CONSOLE not in console.daemon.bindings)


def history(rig):
    return rig.gateway.history(rig.names_channel)


def raw_post(rig, slot, display, body):
    """A body posted exactly as given through a slot's #names webhook: the hand-made
    machine line, or an older client's."""
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(slot, display, "global"))
    finally:
        db.close()
    return rig.gateway.execute_webhook(cred, body, username=display,
                                       allowed_mentions=NO_MENTIONS)


def register(**kw):
    c = DaemonClient(daemon_socket()).connect()
    try:
        return c.register(**kw)
    finally:
        c.close()


def got(shim, message_id):
    """Was this message handed to the shim?"""
    return any(p["message_id"] == message_id for p in shim.queue)


def nothing_kept_for(rig, qualified):
    d = rig.daemon
    return not queued(d, qualified)


def console_cli(rig, srv, argv):
    """`comms` run by the console's own process, the binding's `self` root."""
    return rig.cli(argv, env={CLAIM_SOCKET_ENV: str(srv.socket_path)},
                   project="acme")


# --------------------------------------------------------------------------- the register

def test_a_send_only_register_is_refused_for_a_row_that_is_not_send_only(console):
    for kw in (dict(name="main", scope="proj-a"),          # a standing agent row
               dict(name="planner", scope="proj-a"),       # a chosen name, no row
               dict(name="steward", global_=True),         # a global standing row
               dict()):                                    # the auto-namer
        with pytest.raises(DaemonError) as e:
            register(slot="next-free", adapter="send-only", **kw)
        assert "is not a send-only identity" in str(e.value), kw
    assert not any(b.adapter == "send-only" for b in console.daemon.bindings.values())


@pytest.mark.parametrize("kind", ["claude-shim", "codex", "opencode"])
def test_an_agent_kind_register_of_console_is_refused(console, kind):
    with pytest.raises(DaemonError) as e:
        register(slot="next-free", name="console", scope="acme",
                 project="acme", adapter=kind)
    assert (f"console@acme is a send-only identity and receives no mail: it binds "
            f"only with the send-only adapter kind, never as {kind}") in str(e.value)
    assert CONSOLE not in console.daemon.bindings


def test_the_send_only_kind_binds_console_and_only_agents_seats_shows_it(server, console):
    """[R43]: a send-only binding is not a name anyone can message, so the default view
    leaves it out; the operator's `--seats` view lists it."""
    b = console.daemon.bindings[CONSOLE]
    assert b.adapter == "send-only" and b.tier == "standing"
    code, out = console.cli(["agents"], **LEAD)
    assert code == 0 and "console@acme" not in out and "send-only" not in out
    code, out = console.cli(["agents", "--seats"], **LEAD)
    assert code == 0 and "console@acme" in out and "adapter send-only" in out


# --------------------------------------------------------------------------- the verbs

@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("tag,scope", [("console@acme", "proj-a"),
                                       ("console@acme", "acme")])
def test_a_send_to_console_is_refused_before_posting(console, request, bound, tag, scope):
    """console@acme typed in full meets the send-only refusal from every scope
    ([C16] keeps its name). A bare `console` is the global console, not this one
    ([C10]; below)."""
    if bound:
        request.getfixturevalue("server")
    before = len(history(console))
    code, out = console.cli(["send", "--posture", "none", tag, "report back"], name="lead", scope=scope, slot=5)
    assert code == cli.EXIT_REFUSED
    assert out.strip() == REFUSAL
    assert len(history(console)) == before


@pytest.mark.parametrize("bound", [False, True])
def test_a_send_naming_console_among_others_is_refused_whole(console, request, bound):
    if bound:
        request.getfixturevalue("server")
    console.shim(name="planner", scope="proj-a", slot=1)
    before = len(history(console))
    code, out = console.cli(["send", "--posture", "none", f"{PLANNER},{CONSOLE}", "x"], **LEAD)
    assert code == cli.EXIT_REFUSED and REFUSAL in out
    assert len(history(console)) == before


@pytest.mark.parametrize("bound", [False, True])
def test_a_reply_to_consoles_message_is_refused_before_posting(console, request, bound):
    """The console's own brief, then a reply to it: resolved to console, refused."""
    srv = request.getfixturevalue("server")
    console.shim(name="planner", scope="proj-a", slot=1)
    code, out = console_cli(console, srv, ["send", PLANNER, "your brief", "--posture", "none"])
    assert code == 0, out
    brief = history(console)[-1]
    if not bound:
        srv.stop()
        assert wait_until(lambda: CONSOLE not in console.daemon.bindings)
    before = len(history(console))
    code, out = console.cli(["reply", brief.id, "done"], **LEAD)
    assert code == cli.EXIT_REFUSED
    assert out.splitlines()[-1] == REFUSAL
    code, out = console.cli(["send", "--reply-to", brief.id, CONSOLE, "done"], **LEAD)
    assert code == cli.EXIT_REFUSED and out.strip() == REFUSAL
    assert len(history(console)) == before


# --------------------------------------------------------------------------- the router

@pytest.mark.parametrize("next_turn", [False, True])
def test_a_record_message_naming_console_is_delivered_to_nobody_and_pended_for_nobody(
        console, next_turn):
    planner = console.shim(name="planner", scope="proj-a", slot=1)
    body = machine_line.compose([CONSOLE, PLANNER], "by hand", next_turn=next_turn)
    msg = raw_post(console, 4, "older-client", body)
    if next_turn:
        assert wait_until(lambda: msg.id in queued(console.daemon, PLANNER))
    else:
        assert wait_until(lambda: got(planner, msg.id))
    assert nothing_kept_for(console, CONSOLE)
    assert console.daemon.send_only_seen == {msg.id: None}
    # A rescan re-reads the same line: still nobody.
    console.daemon.rescan()
    assert nothing_kept_for(console, CONSOLE)
    agents = DaemonClient(daemon_socket()).connect()
    try:
        view = agents.query("agents")
    finally:
        agents.close()
    assert CONSOLE not in view["waiting"] and view["pending"] == {}


def test_no_deliver_frame_reaches_the_send_only_binding(console):
    planner = console.shim(name="planner", scope="proj-a", slot=1)
    # Before the bind: a register's replay hands nothing either.
    early = raw_post(console, 4, "older-client",
                     machine_line.compose([CONSOLE], "before the bind"))
    assert wait_until(lambda: early.id in console.daemon.send_only_seen)
    p = Pushes()
    try:
        late = raw_post(console, 4, "older-client",
                        machine_line.compose([CONSOLE, PLANNER], "after the bind"))
        assert wait_until(lambda: got(planner, late.id))
        console.daemon.rescan()
        assert p.delivers() == []
        assert nothing_kept_for(console, CONSOLE)
        # And the one place every delivery passes: it sends a send-only binding nothing.
        b = console.daemon.bindings[CONSOLE]
        author = console.daemon.attribute(late, live=False)
        console.daemon._deliver(b, late, machine_line.parse(late.content), author,
                                backlog=False)
        assert not wait_until(p.delivers, timeout=0.5)
    finally:
        p.close()


def test_mail_pended_for_console_before_its_row_was_send_only_goes_at_its_bind(rig):
    """A box that pended mail for console@acme while it was an ordinary name, then
    provisioned the row: the send-only bind keeps none of it."""
    code, out = rig.cli(["send", "--posture", "none", "--new-name", CONSOLE, "before"], **LEAD)
    assert code == 0, out
    msg = history(rig)[-1]
    assert wait_until(lambda: msg.id in queued(rig.daemon, CONSOLE))
    provision_console(rig)
    p = Pushes()
    try:
        assert nothing_kept_for(rig, CONSOLE)
        assert not wait_until(p.delivers, timeout=0.3)
    finally:
        p.close()


def test_a_reply_once_line_naming_console_leaves_no_ask_open_for_it(console, pushes):
    console.shim(name="planner", scope="proj-a", slot=2)
    console.shim(name="reviewer", scope="proj-a", slot=3)
    seat = console.daemon.bindings[PLANNER].slot_id      # the asker's own seat: verified
    both = raw_post(console, seat, PLANNER, machine_line.compose(
        [CONSOLE, "reviewer@proj-a"], "which branch?", posture="once"))
    alone = raw_post(console, seat, PLANNER, machine_line.compose(
        [CONSOLE], "and you?", posture="once"))
    assert wait_until(lambda: both.id in console.daemon.asks
                      and alone.id in console.daemon.send_only_seen)
    assert console.daemon.asks[both.id]["open"] == {"reviewer@proj-a"}
    assert not (console.daemon.asks.get(alone.id) or {}).get("open")
    assert not any(CONSOLE in a["open"] for a in console.daemon.asks.values())
    nominated = console.daemon._asks_query({"sender": CONSOLE,
                                            "recipients": [PLANNER]})
    assert nominated["asks"] == []


@pytest.mark.parametrize("scope", ["proj-a", "acme"])
def test_a_bare_console_is_the_global_console_an_ordinary_unbound_name(console, pushes,
                                                                     scope):
    """console@acme is bound; a bare `console` is the global console from every
    scope, acme included ([C10], §7.10.1): a chosen name nobody has bound, pended
    for it (with `--new-name`, §7.6), never the send-only console's."""
    code, out = console.cli(["send", "--posture", "none", "--new-name", "console", "hello"],
                            name="lead", scope=scope, slot=5)
    assert code == 0, out
    assert "NOTE: console is not bound right now" in out
    msg = history(console)[-1]
    assert machine_line.parse(msg.content).recipients == ("console",)
    assert wait_until(lambda: msg.id in queued(console.daemon, "console"))
    assert pushes.delivers() == []
    assert nothing_kept_for(console, CONSOLE)
    r = console.daemon.resolve("console")
    assert (r.qualified, r.tier, r.send_only, r.binding) == ("console", "chosen",
                                                             False, None)


# --------------------------------------------------------------------------- sending as it

def test_a_send_from_inside_the_claimservers_session_is_verified_as_console(console,
                                                                           server):
    planner = console.shim(name="planner", scope="proj-a", slot=1)
    code, out = console_cli(console, server, ["send", PLANNER, "your brief", "--posture", "none"])
    assert code == 0, out
    msg = history(console)[-1]
    assert msg.author_display == CONSOLE
    assert msg.author_id == console.agent_webhook_id("console", "acme")
    live = console.daemon.attribute(msg, live=True)
    assert (live.qualified, live.tier, live.attribution) == (CONSOLE, "standing",
                                                             attribution.VERIFIED)
    db = directory.connect(directory_db(), read_only=True)
    try:
        assert attribution.from_record(db, msg).attribution == attribution.VERIFIED
    finally:
        db.close()
    assert wait_until(lambda: got(planner, msg.id))


def test_a_comms_send_child_the_service_spawns_sends_as_console(console, server):
    """The shape the service uses for a first message: a `comms send` child spawned as a
    `tree` root of the binding. A child is a second process, so it needs a record it can
    reach: the local backend's, which every comms process writes directly."""
    if console.backend != "local":
        pytest.skip("a child process cannot reach the mock's in-memory record")
    planner = console.shim(name="planner", scope="proj-a", slot=1)
    script = ("import sys\nfrom moxi_comms import cli\n"
              f"sys.exit(cli.main(['send', {PLANNER!r}, 'your brief', '--posture', 'none']))\n")
    env = dict(os.environ, MOXI_COMMS_GATEWAY="local", MOXI_COMMS_PROJECT="acme")
    for var in ("MOXI_COMMS_NAME", "MOXI_COMMS_SCOPE", "MOXI_COMMS_SLOT", "MOXI_COMMS_GLOBAL"):
        env.pop(var, None)
    child = server.spawn([sys.executable, "-c", script], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = child.communicate(timeout=60)[0]
    assert child.returncode == 0, out
    msg = history(console)[-1]
    assert msg.author_display == CONSOLE
    assert console.daemon.attribute(msg, live=True).attribution == attribution.VERIFIED
    assert wait_until(lambda: got(planner, msg.id))


def test_a_send_from_a_process_outside_it_is_refused(console, server):
    console.shim(name="planner", scope="proj-a", slot=1)
    before = len(history(console))
    # A process the service did not spawn, holding the socket's path: not inside.
    script = ("import sys\nfrom moxi_comms import cli\n"
              f"sys.exit(cli.main(['send', {PLANNER!r}, 'x']))\n")
    env = dict(os.environ, **{CLAIM_SOCKET_ENV: str(server.socket_path)})
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == cli.EXIT_REFUSED, done
    assert NOT_INSIDE in done.stdout and "nothing was sent" in done.stdout
    # A stamp of the name is no live session holding it.
    code, out = console.cli(["send", "--posture", "none", PLANNER, "x"], name="console", scope="acme",
                            stamp_only=True)
    assert code == cli.EXIT_REFUSED
    assert "not inside the session that holds console@acme" in out
    assert len(history(console)) == before
    answer = call(server.socket_path, "claim")
    assert answer["claim"]["qualified"] == CONSOLE        # the holder itself is inside


def test_the_console_sends_to_a_fresh_session_and_is_held_by_a_cold_one(console, server):
    """The cold-cache rule applies to the console as to any sender, on the recipient:
    a just-bound session with no turn reads fresh and gets it; a cold one holds it --
    posted, exit 0, and kept for its own next turn (cold-hold)."""
    fresh = console.shim(name="planner", scope="proj-a", slot=1,
                         session_id="22222222-2222-4222-8222-222222222222")
    code, out = console_cli(console, server, ["send", PLANNER, "first turn", "--posture", "none"])
    assert code == 0, out
    assert wait_until(lambda: got(fresh, history(console)[-1].id))
    cold_sid = "33333333-3333-4333-8333-333333333333"
    write_transcript(cold_sid, age=2 * HOUR + 600)
    console.shim(name="reviewer", scope="proj-a", slot=2, session_id=cold_sid)
    before = len(history(console))
    code, out = console_cli(console, server, ["send", "reviewer@proj-a", "x", "--posture", "none"])
    assert code == 0, out
    assert out.splitlines()[1].startswith("held: reviewer@proj-a's cache went cold")
    assert out.splitlines()[-1].startswith("Sent and held: reviewer@proj-a reads this")
    assert len(history(console)) == before + 1


# --------------------------------------------------------------------------- provisioning

def test_the_provisioning_adds_a_send_only_row_with_its_own_webhooks(rig):
    lines = provision_console(rig)
    db = directory.connect(directory_db(), read_only=True)
    try:
        row = directory.standing_row(db, "console", "acme")
        assert directory.kind_of(row) == "send-only"
        assert directory.is_send_only(db, "console", "acme")
        assert not directory.is_send_only(db, "main", "proj-a")
        hooks = {r["channel_id"]: r["webhook_id"] for r in db.execute(
            "SELECT * FROM webhooks WHERE agent_name='console' AND agent_scope='acme'")}
    finally:
        db.close()
    assert set(hooks) == {rig.names_channel}, "one #names webhook"
    assert "send-only console@acme: standing row, kind send-only" in lines
    if rig.store is not None:
        assert "acme/console" in rig.store.owners_present()
    # A re-run converges: the same webhooks, nothing minted.
    provision_console(rig)
    db = directory.connect(directory_db(), read_only=True)
    try:
        again = {r["channel_id"]: r["webhook_id"] for r in db.execute(
            "SELECT * FROM webhooks WHERE agent_name='console' AND agent_scope='acme'")}
    finally:
        db.close()
    assert again == hooks


def test_the_provisioning_refuses_to_convert_an_agent_row(rig):
    with pytest.raises(SystemExit) as e:
        provision.send_only_main(["main@proj-a"], gateway=rig.gateway, store=rig.store,
                                 out=lambda _l: None)
    assert "already provisioned as a standing row of kind 'agent'" in str(e.value)
    db = directory.connect(directory_db(), read_only=True)
    try:
        assert directory.kind_of(directory.standing_row(db, "main", "proj-a")) == "agent"
    finally:
        db.close()


def test_the_provisioning_refuses_a_name_live_as_a_chosen_binding(rig):
    rig.shim(name="console", scope="acme", slot=1, project="acme")
    with pytest.raises(SystemExit) as e:
        provision.send_only_main(["console@acme"], gateway=rig.gateway,
                                 store=rig.store, out=lambda _l: None)
    assert "currently live as a chosen binding" in str(e.value)


def test_a_directory_from_before_the_kind_column_reads_agent_and_migrates(tmp_path):
    path = tmp_path / "directory.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE agents (name TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
            created_at TEXT NOT NULL, PRIMARY KEY (name, scope));
        INSERT INTO agents VALUES ('main', 'proj-a', 'active', '2026-01-01');
    """)
    old.commit()
    old.close()
    ro = directory.connect(path, read_only=True)
    try:
        # The daemon's read-only view of an unmigrated file: every row is an agent.
        assert directory.kind_of(directory.standing_row(ro, "main", "proj-a")) == "agent"
        assert not directory.is_send_only(ro, "main", "proj-a")
    finally:
        ro.close()
    conn = directory.init(path)
    try:
        assert directory.kind_of(directory.standing_row(conn, "main", "proj-a")) == "agent"
        assert directory.migrate(conn) is False                # once is enough
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE agents SET kind='inbox' WHERE name='main'")
    finally:
        conn.close()


def test_the_cli_dispatches_the_send_only_subcommand(rig, monkeypatch):
    seen = {}
    monkeypatch.setattr(provision, "send_only_main",
                        lambda argv: seen.setdefault("argv", argv) and 0)
    provision.main(["send-only", "console@acme"])
    assert seen["argv"] == ["console@acme"]


# --------------------------------------------------------------------------- build verification 1

def _armed_but_unsupervised(name="console", scope="acme", kind="send-only", **kw):
    """A ClaimServer whose daemon arm the test drives one register at a time."""
    srv = ClaimServer(name, scope, kind=kind, arm=True, **kw)
    srv.holder.save()
    srv.holder.listen()
    return srv


#: The arm's slot behaviour needs a binding that holds a slot, and since [B14] the
#: console holds none: these three drive a ClaimServer of a chosen name, the shape a
#: module's own holder takes, registered under a built-in kind.
ARM = "voicearm@proj-a"


def _slot_arm(**kw):
    return _armed_but_unsupervised(name="voicearm", scope="proj-a", kind="codex", **kw)


def test_a_rebind_after_the_daemon_drops_it_asks_next_free_not_its_last_slot(console):
    """The arm's re-register asks what the holder asked (next-free), never the slot the
    last bind answered: a name that took that slot meanwhile does not lock it out."""
    srv = _slot_arm()
    try:
        assert srv._register_once()
        first = console.daemon.bindings[ARM].slot_id
        assert isinstance(first, int)
        srv._client.close()
        assert wait_until(lambda: ARM not in console.daemon.bindings)
        squatter = DaemonClient(daemon_socket()).connect()
        try:
            squatter.register(slot=first, name="squatter", scope="proj-a",
                              project="proj-a", adapter="codex")
            assert srv._register_once(), srv.refusal
            assert console.daemon.bindings[ARM].slot_id not in (first, None)
            assert call(srv.socket_path, "state")["state"] == "bound"
        finally:
            squatter.close()
    finally:
        srv.stop()


def test_a_pinned_slot_held_by_another_name_falls_back_to_next_free(console, caplog):
    squatter = DaemonClient(daemon_socket()).connect()
    try:
        squatter.register(slot=2, name="squatter", scope="proj-a", project="proj-a",
                          adapter="codex")
        srv = _slot_arm(slot=2)
        try:
            with caplog.at_level("WARNING", logger="comms"):
                assert srv._register_once(), srv.refusal
            slot = console.daemon.bindings[ARM].slot_id
            assert slot not in (2, None) and srv.asked == "next-free"
            assert call(srv.socket_path, "claim")["claim"]["slot"] == slot
            assert f"slot 2 was refused for {ARM}" in caplog.text
        finally:
            srv.stop()
    finally:
        squatter.close()


def test_a_refused_name_is_not_taken_for_a_slot_refusal(console):
    """Pinned, and refused for the name (another holder of it): no fallback register
    binds it anywhere."""
    first = ClaimServer("voicearm", "proj-a", kind="codex", arm=True).start()
    try:
        assert wait_until(lambda: ARM in console.daemon.bindings)
        srv = _slot_arm(slot=5)
        try:
            assert not srv._register_once()
            assert srv.asked == 5 and "already bound" in srv.refusal
        finally:
            srv.stop()
    finally:
        first.stop()


def test_a_bare_console_in_acme_is_the_global_console_while_one_is_bound(
        console):
    """console@acme unbound, a global `console` bound by hand: a bare `console`
    from acme is that global binding, as for any name ([C10]); console@acme
    typed in full is still the send-only row, refused."""
    frames = []
    global_console = DaemonClient(daemon_socket(), on_push=frames.append).connect()
    try:
        global_console.register(slot="next-free", name="console", global_=True,
                                project="acme", adapter="claude-shim",
                                session_id=warm_session())
        assert "console" in console.daemon.bindings
        r = console.daemon.resolve("console")
        assert (r.qualified, r.send_only) == ("console", False) and r.binding is not None
        r = console.daemon.resolve(CONSOLE)
        assert (r.qualified, r.send_only, r.binding) == (CONSOLE, True, None)
        code, out = console.cli(["send", "--posture", "none", "console", "x"], name="lead",
                                scope="acme", slot=5)
        assert code == 0, out
        mid = history(console)[-1].id
        assert machine_line.parse(history(console)[-1].content).recipients == ("console",)
        assert wait_until(lambda: mid in console.daemon.bindings["console"].delivered)
        before = len(history(console))
        code, out = console.cli(["send", "--posture", "none", CONSOLE, "x"], name="lead",
                                scope="acme", slot=5)
        assert code == cli.EXIT_REFUSED and out.strip() == REFUSAL
        assert len(history(console)) == before
    finally:
        global_console.close()


def test_the_bind_logs_what_it_dropped(rig, caplog):
    code, out = rig.cli(["send", "--posture", "none", "--new-name", CONSOLE, "before"], **LEAD)
    assert code == 0, out
    msg = history(rig)[-1]
    assert wait_until(lambda: msg.id in queued(rig.daemon, CONSOLE))
    provision_console(rig)
    with caplog.at_level("INFO", logger="comms"):
        p = Pushes()
        p.close()
    assert (f"bind console@acme: dropped 1 message(s) queued for it from before its "
            f"row was send-only, delivered to nobody: {msg.id}") in caplog.text


def test_the_provisioning_refuses_a_retired_send_only_row(console):
    db = directory.connect(directory_db())
    try:
        db.execute("UPDATE agents SET status='retired' WHERE name='console' "
                   "AND scope='acme'")
        db.commit()
    finally:
        db.close()
    lines = []
    with pytest.raises(SystemExit) as e:
        provision.send_only_main(["console@acme"], gateway=console.gateway,
                                 store=console.store, out=lines.append)
    assert "console@acme is provisioned but retired; nothing was changed" in str(e.value)
    assert lines == []
    db = directory.connect(directory_db(), read_only=True)
    try:
        row = db.execute("SELECT status FROM agents WHERE name='console'").fetchone()
        assert row["status"] == "retired"
    finally:
        db.close()


# --------------------------------------------------------------------------- no slot ([B14])
#
# The rule ([R27], design §3.4 *No slot*, operator ruling [C27]): a send-only binding is
# never on a slot -- not at its register, not while it is bound, not at its unbind, not
# across a daemon restart. It takes none of the provisioned slots, spends no slot keep,
# has no entry in the keep mark, and no free-slot count sees it.

def provisioned_slots():
    db = directory.connect(directory_db(), read_only=True)
    try:
        return directory.slot_ids(db)
    finally:
        db.close()


def keep_mark():
    import json

    from moxi_comms.paths import slot_keeps
    return json.loads(slot_keeps().read_text())


def holders(n, start=0):
    """`n` raw chosen bindings on `next-free`, each holding one slot."""
    out = []
    for i in range(start, start + n):
        c = DaemonClient(daemon_socket()).connect()
        c.register(slot="next-free", name=f"filler{i}", scope="proj-a", project="proj-a",
                   adapter="codex")
        out.append(c)
    return out


def test_a_send_only_register_binds_with_no_slot(console, pushes):
    assert pushes.reply["slot"] is None
    assert pushes.reply["identity"]["qualified"] == CONSOLE
    assert console.daemon.bindings[CONSOLE].slot_id is None
    assert CONSOLE not in console.daemon.by_slot.values()
    assert None not in console.daemon.by_slot
    view = DaemonClient(daemon_socket()).connect()
    try:
        row = [b for b in view.query("agents")["bindings"] if b["qualified"] == CONSOLE]
    finally:
        view.close()
    assert row == [{"qualified": CONSOLE, "name": "console", "scope": "acme",
                    "tier": "standing", "adapter": "send-only", "slot": None,
                    "endpoint": None, "endpoint_since": None}]


@pytest.mark.parametrize("asked", [1, 6, 99])
def test_a_send_only_register_asking_a_slot_by_number_is_refused(console, asked):
    before = dict(console.daemon.by_slot)
    with pytest.raises(DaemonError) as e:
        register(slot=asked, name="console", scope="acme", project="acme",
                 adapter="send-only")
    assert ("console@acme is a send-only identity and holds no slot: it registers "
            f"next-free, never on slot {asked}") in str(e.value)
    assert CONSOLE not in console.daemon.bindings
    assert console.daemon.by_slot == before
    assert all(k.qualified != CONSOLE for k in console.daemon.keeps.values())


def test_the_console_takes_none_of_the_slots(console, pushes):
    """Every provisioned slot but one bound, and the console bound: a `next-free`
    register gets that one; with it taken too, the next is refused "no free slot"."""
    slots = provisioned_slots()
    held = holders(len(slots) - 1)
    try:
        last = DaemonClient(daemon_socket()).connect()
        try:
            reply = last.register(slot="next-free", name="last", scope="proj-a",
                                  project="proj-a", adapter="codex")
            free = set(slots) - {console.daemon.bindings[f"filler{i}@proj-a"].slot_id
                                 for i in range(len(slots) - 1)}
            assert {reply["slot"]} == free
            with pytest.raises(DaemonError) as e:
                register(slot="next-free", name="one-more", scope="proj-a",
                         project="proj-a", adapter="codex")
            assert "no free slot" in str(e.value)
        finally:
            last.close()
    finally:
        for c in held:
            c.close()


def test_the_console_binds_with_every_slot_taken(console):
    held = holders(len(provisioned_slots()))
    try:
        p = Pushes()
        try:
            assert p.reply["slot"] is None
            assert console.daemon.bindings[CONSOLE].slot_id is None
        finally:
            p.close()
    finally:
        for c in held:
            c.close()


def test_the_console_cannot_take_a_seats_pinned_slot(console):
    """A pinned seat down past its keep: the console binding meanwhile leaves its slot
    free, and the seat comes back to it."""
    seat = console.shim(slot=1, name="planner", scope="proj-a")
    seat.stop()
    assert wait_until(lambda: PLANNER not in console.daemon.bindings)
    console.daemon.keeps.clear()                     # the seat's keep has lapsed
    p = Pushes()
    try:
        assert 1 not in console.daemon.by_slot
        assert console.shim(slot=1, name="planner", scope="proj-a").slot_id == 1
    finally:
        p.close()


def test_the_keep_mark_lists_nothing_for_it_and_its_stop_leaves_no_keep(console, server):
    assert console.daemon._keeps_armed
    assert all(e["qualified"] != CONSOLE for e in keep_mark())
    server.stop()
    assert wait_until(lambda: CONSOLE not in console.daemon.bindings)
    assert all(k.qualified != CONSOLE for k in console.daemon.keeps.values())
    assert all(e["qualified"] != CONSOLE for e in keep_mark())


def test_a_second_send_only_register_is_refused_as_already_bound_not_on_a_slot(console,
                                                                            pushes):
    with pytest.raises(DaemonError) as e:
        register(slot="next-free", name="console", scope="acme", project="acme",
                 adapter="send-only")
    assert str(e.value) == ("console@acme is already bound (no slot, adapter "
                            "send-only)")
    assert e.value.response["holder"] == {"qualified": CONSOLE, "slot": None,
                                          "adapter": "send-only"}
    assert console.daemon.bindings[CONSOLE].slot_id is None


def test_the_journal_row_has_no_slot_and_replay_reads_its_post_verified(console, server):
    planner = console.shim(name="planner", scope="proj-a", slot=1)
    code, out = console_cli(console, server, ["send", PLANNER, "your brief", "--posture", "none"])
    assert code == 0, out
    msg = history(console)[-1]
    assert wait_until(lambda: got(planner, msg.id))
    rows = [r for r in console.daemon.journal.intervals() if r["qualified"] == CONSOLE]
    assert rows and all(r["slot_id"] is None for r in rows)
    replayed = console.daemon.attribute(msg, live=False)
    assert (replayed.qualified, replayed.tier, replayed.attribution) == (
        CONSOLE, "standing", attribution.VERIFIED)


def test_agents_seats_lists_it_with_no_slot(console, server):
    console.shim(name="planner", scope="proj-a", slot=1)
    code, out = console.cli(["agents", "--seats"], **LEAD)
    assert code == 0, out
    line = next(ln for ln in out.splitlines() if ln.startswith(CONSOLE))
    assert line.split() == [CONSOLE, "standing", "no", "slot", "adapter", "send-only"]
    seat = next(ln for ln in out.splitlines() if ln.startswith(PLANNER))
    assert line.index("adapter") == seat.index("adapter")      # the columns still line up
    code, out = console.cli(["agents"], **LEAD)
    assert code == 0 and "no slot" not in out                 # seats are --seats only


def test_across_a_daemon_restart_it_is_never_on_a_slot(console, monkeypatch):
    """Stopped cleanly (the freeze writes the mark), crashed (the mark stays as last
    written), and stopped inside the arm's register, after the daemon answered and
    before the arm took the binding (s113.1, forced): the mark never names the console,
    the new daemon keeps it no slot, and the arm re-registers it with none."""
    import itertools

    from claim_kit import RegisterDrop
    from moxi_comms import claimhold
    drop = RegisterDrop(monkeypatch, claimhold, console)
    srv = ClaimServer("console", "acme", kind="send-only", arm=True,
                      backoff=itertools.repeat(0.05)).start()
    try:
        # Only the daemon's table, not the holder's own `bound`: a stop that lands
        # between the register's answer and `set_bound` is the arm's to survive.
        assert wait_until(lambda: CONSOLE in console.daemon.bindings)
        for leg in ("clean", "crashed", "inside the register"):
            if leg == "crashed":
                console.daemon._keeps_armed = False     # a dead process writes nothing
            console.daemon.stop()
            assert all(e["qualified"] != CONSOLE for e in keep_mark())
            if leg == "inside the register":
                drop.arm("after_answer_restart")
                drop.fresh_daemon()
                assert drop.fired.wait(5.0)
                assert not (srv.holder.state == "bound" and not srv._client.connected)
                assert all(e["qualified"] != CONSOLE for e in keep_mark())
            drop.fresh_daemon()
            assert all(k.qualified != CONSOLE for k in console.daemon.keeps.values())
            assert wait_until(lambda: CONSOLE in console.daemon.bindings
                              and srv.holder.state == "bound", timeout=5), \
                (leg, srv.holder.state, srv.refusal)
            assert srv._client.connected
            assert console.daemon.bindings[CONSOLE].slot_id is None
            assert CONSOLE not in console.daemon.by_slot.values()
            assert all(e["qualified"] != CONSOLE for e in keep_mark())
    finally:
        srv.stop()
        drop.stop_all()


def test_the_claim_keeps_next_free_as_its_slot_and_a_child_gets_no_comms_slot(console,
                                                                            server):
    """v0.25 re-check MN2: `set_bound` keeps the asked value when the answer's slot is
    null, so the console's claim carries the string `next-free`, never a number; every
    reader takes only an int, so its children get no `MOXI_COMMS_SLOT`."""
    from moxi_comms import claimfile
    from moxi_comms.claimhold import claim_overlay
    assert server.holder.claim["slot"] == "next-free"
    assert call(server.socket_path, "claim")["claim"]["slot"] == "next-free"
    assert call(server.socket_path, "state")["state"] == "bound"
    assert claimfile.read(server.holder.key)["claim"]["slot"] == "next-free"
    env = claim_overlay({CLAIM_SOCKET_ENV: str(server.socket_path), "MOXI_COMMS_SLOT": "3",
                         "MOXI_COMMS_NAME": "stale"})
    assert "MOXI_COMMS_SLOT" not in env
    assert (env["MOXI_COMMS_NAME"], env["MOXI_COMMS_SCOPE"]) == ("console", "acme")


def test_a_send_only_claimserver_pinned_by_mistake_still_binds_with_no_slot(console):
    """The console pins none. If a holder did, the daemon refuses the number and the
    arm's slot fall-back binds it `next-free`: still no slot."""
    srv = _armed_but_unsupervised(slot=2)
    try:
        assert srv._register_once(), srv.refusal
        assert srv.asked == "next-free"
        assert console.daemon.bindings[CONSOLE].slot_id is None
        assert 2 not in console.daemon.by_slot
    finally:
        srv.stop()
