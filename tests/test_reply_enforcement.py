"""Reply enforcement, from the mark on line zero to the reminder at a Stop.

A claude-shim session that ends a turn owing its director or the operator a reply is
held open once by the Stop hook the shim already runs; each debt draws one counted reminder, never while work that will wake
the session runs, and every missing or failed fact lets the turn end. A debt is paid by
the session's own message to the sender, and a sender's answer threaded to that payment
owes nothing. An ignored reminder is recorded where the owed party sees it. The
operator's binding is a roster row like any other. The quiet rule is in the
context block.

`[operator-words]` and `[speaker]` are marks an older comms wrote; old records that carry
them still parse, and the marks are read and ignored. An operator debt is paid only by a
message to `operator`. The rest of the old marks' tests are in `test_old_marks.py`.

Every rig test here runs on the mock and on the local backend (`BOTH_BACKENDS` in
conftest), and no test targets the Discord implementation. Tests that take no
rig are pure and run once.
"""

from __future__ import annotations

import time

import pytest
from conftest import projects_root, wait_for_queue, wait_until, write_config

from moxi_comms import cli, constants, directory, machine_line, scoping
from moxi_comms.adapters.claude_shim import frame_line
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import Daemon
from moxi_comms.mock_gateway import NO_MENTIONS
from moxi_comms.paths import daemon_socket, directory_db
from moxi_comms.sender import SenderIdentity, credential_for

import shim_drive

PLANNER = dict(slot=1, name="planner")
REVIEWER = dict(slot=2, name="reviewer")
CRITIC = dict(slot=3, name="critic")
ARCHIVIST = dict(slot=5, name="archivist", env={"MOXI_COMMS_GLOBAL": "1"})
LEAD = dict(slot=6, name="lead", env={"MOXI_COMMS_GLOBAL": "1"})

P, R, C = "planner", "reviewer", "critic"

#: The session id of the operator's own binding.
OPERATOR_SID = "eeeeeeee-5555-4555-8555-555555555555"


# --------------------------------------------------------------------------- helpers

def posted(rig):
    return rig.gateway.history(rig.names_channel)


def last(rig):
    return posted(rig)[-1]


def line_zero(msg):
    return machine_line.parse(msg.content)


def query_owed(identity, authors=None, referents=()):
    client = DaemonClient(daemon_socket()).connect()
    try:
        return client.query("owed", identity=identity, authors=authors or {},
                            referents=list(referents))
    finally:
        client.close()


def send(rig, to, text="x", *flags, who=PLANNER):
    """One send that goes. These tests were written when a plain send was open, so a new
    send with no `--posture` states open. Many send before the recipient's first bind,
    so it passes `--new-name`; on a name bound now or before that changes
    nothing."""
    flags = list(flags)
    if "--posture" not in flags and "--reply-to" not in flags:
        flags += ["--posture", "open"]
    code, out = rig.cli(["send", "--new-name", to, text, *flags], **who)
    assert code == 0, out
    return last(rig)


def reply(rig, message_id, text="x", *flags, who=REVIEWER):
    code, out = rig.cli(["reply", message_id, text, *flags], **who)
    assert code == 0, out
    return last(rig)


def indexed(rig, message_id, daemon=None):
    daemon = daemon or rig.daemon
    assert wait_until(lambda: message_id in daemon.sent_by_id), \
        f"{message_id} never reached the sent index"
    return daemon.sent_by_id[message_id]


def raw_post(rig, slot, display, body):
    """Post a body exactly as given through a slot's webhook, wearing `display`."""
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(slot, display, "global"))
    finally:
        db.close()
    return rig.gateway.execute_webhook(cred, body, username=display,
                                       allowed_mentions=NO_MENTIONS)


@pytest.fixture
def bind():
    """Bindings held by bare DaemonClients (the operator's own binding, a worker)."""
    made = []

    def _bind(**kwargs):
        c = DaemonClient(daemon_socket()).connect()
        c.register(**kwargs)
        made.append(c)
        return c

    yield _bind
    for c in made:
        c.close()


def bind_operator(bind):
    """The operator bound by a bare binding of its own: a session that has made no
    request yet, so its cache reads `fresh` and mail to it goes live."""
    return bind(slot=4, name="operator", global_=True, adapter="claude-shim",
                session_id=OPERATOR_SID, projects_root=str(projects_root()), turned=False)


@pytest.fixture
def reboot():
    started = []

    def _reboot(rig):
        rig.daemon.stop()
        rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
        started.append(rig.daemon)
        return rig.daemon

    yield _reboot
    for d in started:
        d.stop()


def payload(**extra):
    """The delivered shape (test_frame pins it against `daemon._deliver`)."""
    base = {"message_id": "101", "channel_id": "c1", "room": "#names",
            "author": {"name": "lead", "tier": "chosen", "attribution": "verified"},
            "recipients": ["reviewer@proj-a"], "posture": "once",
            "backlog": False, "text": "body", "reply_to": None, "audience": None,
            "audience_source": None, "timestamp": "2026-09-30T12:00:00Z"}
    base.update(extra)
    return base


# ------------------------------------------------ an old [operator-words] line

def test_an_old_marked_line_parses_with_every_other_field():
    """An old mark keeps its slot: matched and dropped."""
    ml = machine_line.parse(
        "::to lead re:77 [audience:operator] [operator-words] [next-turn] [no reply]\nhe asks")
    assert (ml.recipients, ml.reply_to, ml.audience, ml.held, ml.posture, ml.text) == \
        (("lead",), "77", "operator", True, "none", "he asks")
    assert not hasattr(ml, "operator_words")
    assert machine_line.parse("::to lead [operator-words]\nhi").posture == "open"


def test_the_old_mark_is_matched_case_insensitively():
    assert machine_line.parse("::to lead [Operator-Words]").recipients == ("lead",)


@pytest.mark.parametrize("line", [
    "::to lead [operator-words] [operator-words]",        # doubled
    "::to lead [operator-words] [audience:operator]",     # before the audience
    "::to lead [next-turn] [operator-words]",             # after next-turn
    "::to lead [speaker] [operator-words]",               # after the posture slot
    "::to [operator-words] lead",                         # in the tags
    "::to lead [operator-words] please",                  # anything after it
])
def test_the_old_mark_out_of_place_or_doubled_makes_line_zero_prose(line):
    assert machine_line.parse(line) is None


def test_the_frame_line_shows_no_removed_mark_even_from_a_payload_that_carries_one():
    """An older daemon's payload may still carry the three keys; the frame drops them."""
    line = frame_line(payload(operator_words=True, speaker=True, via_phone=True,
                              audience="operator", posture="none"))
    assert line.endswith("audience: operator · [no reply]")
    for mark in ("[operator-words]", "[speaker]", "[via-phone]"):
        assert mark not in line


def test_bodies_keeps_its_bare_namespace_form():
    """A caller outside comms calls `cli._bodies` with a bare namespace and gets exactly
    these bytes."""
    from types import SimpleNamespace

    class Ctx:
        class gateway:
            max_content_length = 2000

    args = SimpleNamespace(text="hi")
    assert cli._bodies(args, Ctx(), ["lead"], None) == ["::to lead\nhi"]


# ------------------------------------------------ the sent index and `owed`

def test_tag_matching_moved_to_scoping_and_the_cli_name_still_answers():
    assert cli._tag_matches is scoping.tag_matches
    assert scoping.tag_matches("lead", "lead")
    assert scoping.tag_matches("planner@global", "planner"), "one identity, two spellings"
    assert scoping.tag_matches("planner@proj-a", "planner@proj-a")
    assert not scoping.tag_matches("planner", "planner@proj-a"), "a bare tag is the global x"
    assert not scoping.tag_matches("planner@proj-a", "planner")
    assert not scoping.tag_matches("planner@proj-b", "planner@proj-a")


def test_the_index_is_fed_live_with_literal_tags_posture_reply_and_attribution(rig):
    rig.shim(**PLANNER)
    ask = send(rig, "reviewer", "which?", "--posture", "once")
    e = indexed(rig, ask.id)
    assert (e.author, e.tags, e.posture, e.reply_to, e.verified) == \
        (P, (R,), "once", None, True), "the verb writes qualified tags"
    typed = raw_post(rig, 1, P, "::to reviewer re:5\nby hand")
    e = indexed(rig, typed.id)
    assert (e.tags, e.reply_to) == (("reviewer",), "5"), "tags exactly as written"


def test_an_unbound_sender_is_indexed_unverified(rig):
    m = send(rig, "reviewer", "hi")
    assert indexed(rig, m.id).verified is False


def test_a_reparse_adds_nothing(rig):
    m = send(rig, "reviewer", "hi")
    indexed(rig, m.id)
    rig.daemon.rescan()
    assert [e.id for e in rig.daemon.sent[P]].count(m.id) == 1


def test_block_indexes_nothing(rig):
    write_config(None, "block = true\n")
    m = raw_post(rig, 1, P, "::to reviewer\nhi")
    rig.daemon._route(m, backlog=False)
    assert m.id not in rig.daemon.sent_by_id


def test_the_boot_read_rebuilds_the_same_entries_for_bare_and_qualified_tags(rig, reboot):
    rig.shim(**PLANNER)
    bare = raw_post(rig, 1, P, "::to reviewer\nbare")
    qualified = raw_post(rig, 1, P, f"::to {R}@global\nqualified")
    live = {i: indexed(rig, i) for i in (bare.id, qualified.id)}
    before = query_owed(P, {R: "0"})["sent"][R]
    assert [row["id"] for row in before] == [bare.id, qualified.id]
    daemon = reboot(rig)
    for i, e in live.items():
        again = daemon.sent_by_id[i]
        assert (again.author, again.tags, again.posture, again.reply_to) == \
            (e.author, e.tags, e.posture, e.reply_to)
    after = query_owed(P, {R: "0"})["sent"][R]
    assert [row["id"] for row in after] == [bare.id, qualified.id]


def test_the_floor_is_the_boot_windows_start_and_older_mail_is_not_read(rig, reboot):
    m = send(rig, "reviewer", "old")
    rig.age_message(m.id, "2020-01-01T00:00:00Z")
    daemon = reboot(rig)
    assert daemon.sent_floor == pytest.approx(rig.clock() - constants.OWED_TTL, abs=2)
    assert m.id not in daemon.sent_by_id
    assert query_owed(R)["floor"] == daemon.sent_floor


def test_a_gateway_without_since_floors_at_boot(rig, monkeypatch, reboot):
    monkeypatch.delattr(type(rig.gateway), "since")
    daemon = reboot(rig)
    assert daemon.sent_floor == pytest.approx(rig.clock(), abs=1)


def test_a_failed_boot_read_floors_at_boot_and_the_daemon_still_starts(rig, monkeypatch,
                                                                        reboot):
    def broken(*a, **k):
        raise RuntimeError("record unreadable")
    monkeypatch.setattr(type(rig.gateway), "since", broken)
    daemon = reboot(rig)
    assert daemon.sent_floor == pytest.approx(rig.clock(), abs=1)


def test_the_index_trims_by_age(rig):
    m = send(rig, "reviewer", "hi")
    indexed(rig, m.id)
    rig.clock.advance(constants.OWED_TTL + 60)
    with rig.daemon._lock:
        rig.daemon._trim_sent()
    assert m.id not in rig.daemon.sent_by_id


def test_owed_sent_lists_the_identitys_entries_to_each_author_after_its_id(rig):
    rig.shim(**REVIEWER)
    ask = send(rig, R, "build it")
    early = send(rig, "planner", "before", who=REVIEWER)
    later = send(rig, "planner", "after", "--posture", "none", who=REVIEWER)
    other = send(rig, "critic", "elsewhere", who=REVIEWER)
    indexed(rig, other.id)
    answer = query_owed(R, {P: early.id})
    assert [row["id"] for row in answer["sent"][P]] == [later.id]
    assert answer["sent"][P][0]["posture"] == "none"
    assert [row["id"] for row in query_owed(R, {P: ask.id})["sent"][P]] == \
        [early.id, later.id]


def test_owed_names_only_the_authors_asked_for(rig, bind):
    """The answer names only the authors asked for, while the operator is bound."""
    rig.shim(**REVIEWER)
    bind_operator(bind)
    wait_until(lambda: "operator" in rig.daemon.bindings)
    to_archivist = send(rig, "archivist", "done", who=REVIEWER)
    to_him = send(rig, "operator", "done", who=REVIEWER)
    indexed(rig, to_archivist.id), indexed(rig, to_him.id)
    answer = query_owed(R, {"operator": "0"})
    assert answer["sent"] == {"operator": [{"id": to_him.id, "posture": "open"}]}


def test_owed_referents_say_whose_message_and_its_posture(rig):
    rig.shim(**REVIEWER)
    mine = send(rig, "planner", "report", who=REVIEWER)
    theirs = send(rig, "reviewer", "task")
    indexed(rig, theirs.id)
    refs = query_owed(R, referents=[mine.id, theirs.id, "12345"])["referents"]
    assert refs == {mine.id: {"own": True, "posture": "open"},
                    theirs.id: {"own": False, "posture": "open"}, "12345": None}


def test_an_unknown_query_kind_is_what_an_old_daemon_answers(rig):
    client = DaemonClient(daemon_socket()).connect()
    try:
        from moxi_comms.client import DaemonError
        with pytest.raises(DaemonError, match="unknown query kind 'nope'"):
            client.query("nope")
    finally:
        client.close()


# awaiting

def test_a_plain_send_starts_no_awaiting(rig):
    rig.shim(**REVIEWER), rig.shim(**PLANNER)
    m = send(rig, "planner", "fyi", who=REVIEWER)
    indexed(rig, m.id)
    assert query_owed(R)["awaiting"] == []


def test_a_reply_once_send_to_a_bound_worker_awaits_it(rig):
    rig.shim(**REVIEWER), rig.shim(**PLANNER)
    ask = send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    indexed(rig, ask.id)
    assert query_owed(R)["awaiting"] == [P]


def test_an_ask_to_a_name_that_is_not_bound_awaits_nothing(rig):
    rig.shim(**REVIEWER)
    ask = send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    indexed(rig, ask.id)
    assert query_owed(R)["awaiting"] == []


def test_the_worker_unbinding_ends_the_wait(rig):
    rig.shim(**REVIEWER)
    planner = rig.shim(**PLANNER)
    ask = send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    indexed(rig, ask.id)
    planner.stop()
    wait_until(lambda: P not in rig.daemon.bindings)
    assert query_owed(R)["awaiting"] == []


def test_ask_ttl_ends_the_wait(rig):
    rig.shim(**REVIEWER), rig.shim(**PLANNER)
    ask = send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    indexed(rig, ask.id)
    rig.clock.advance(constants.ASK_TTL + 1)
    assert query_owed(R)["awaiting"] == []


def test_a_no_reply_message_not_threaded_to_the_ask_does_not_end_the_wait(rig):
    rig.shim(**REVIEWER), rig.shim(**PLANNER)
    ask = send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    fyi = send(rig, "reviewer", "fyi", "--new", "--posture", "none", who=PLANNER)
    indexed(rig, fyi.id)
    assert query_owed(R)["awaiting"] == [P]
    assert indexed(rig, ask.id).posture == "once"


def test_a_new_message_not_marked_no_reply_ends_the_wait(rig):
    rig.shim(**REVIEWER), rig.shim(**PLANNER)
    send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    new = send(rig, "reviewer", "starting now", "--new", who=PLANNER)
    indexed(rig, new.id)
    assert query_owed(R)["awaiting"] == []


def test_a_no_reply_reply_threaded_to_the_ask_ends_the_wait(rig):
    rig.shim(**REVIEWER), rig.shim(**PLANNER)
    ask = send(rig, "planner", "do it", "--posture", "once", who=REVIEWER)
    answer = reply(rig, ask.id, "done", who=PLANNER)
    assert line_zero(answer).posture == "none", "reply posture's default for a once-ask"
    indexed(rig, answer.id)
    assert query_owed(R)["awaiting"] == []


def test_an_unverified_ask_starts_no_awaiting(rig):
    rig.shim(**PLANNER)
    ask = raw_post(rig, 2, R, "::to planner [reply-once]\ndo it")
    indexed(rig, ask.id)
    assert rig.daemon.sent_by_id[ask.id].verified is False
    assert query_owed(R)["awaiting"] == []


# undelivered

def test_mail_claimed_but_not_injected_is_undelivered_and_injection_clears_it(rig):
    reviewer = rig.shim(**REVIEWER)
    send(rig, "reviewer", "task")
    wait_for_queue(reviewer, 1)
    assert query_owed(R)["undelivered"] is True
    reviewer.on_turn_boundary()          # the flush injects it and reports `injected`
    assert wait_until(lambda: query_owed(R)["undelivered"] is False)


def test_a_reconnect_after_a_daemon_restart_whose_holding_ids_do_not_read_undelivered(
        rig, reboot):
    reviewer = rig.shim(**REVIEWER, autoreconnect=True, backoff=iter([0.05] * 200))
    send(rig, "reviewer", "task")
    wait_for_queue(reviewer, 1)
    reviewer.on_turn_boundary()          # injected, turn not yet complete: held_ids
    assert reviewer.held_ids
    daemon = reboot(rig)
    assert wait_until(lambda: R in daemon.bindings, timeout=5)
    assert daemon.bindings[R].handed_max == "0"
    assert query_owed(R)["undelivered"] is False


# --------------------------------------------------------------- presence

def test_roster_of_keeps_each_names_adapter_beside_its_tier():
    from moxi_comms.adapters import shim_roster
    info = {"bindings": [{"qualified": "operator", "tier": "chosen", "adapter": "claude-shim"}],
            "pending": {}}
    assert shim_roster.roster_of(info) == {"operator": {"tier": "chosen",
                                                        "adapter": "claude-shim"}}


def test_the_operators_join_and_leave_render_like_any_names():
    """`operator` joins and leaves as any name does."""
    from moxi_comms.adapters import shim_roster
    now = {"operator": {"tier": "standing", "adapter": "claude-shim"}}
    assert shim_roster.render_delta(["operator"], [], now) == (
        "(comms roster update: operator [standing] is now reachable — `moxi comms agents` "
        "is always current.)")
    assert shim_roster.render_delta([], ["operator"], {}) == (
        "(comms roster update: operator left — `moxi comms agents` is always current.)")


def test_other_joins_and_leaves_render_as_today_and_an_old_memory_has_no_adapter():
    from moxi_comms.adapters import shim_roster
    now = {"planner@proj-a": {"tier": "chosen", "adapter": "claude-shim"}}
    assert shim_roster.render_delta(["planner@proj-a"], ["operator"], now) == (
        "(comms roster update: planner@proj-a [chosen] is now reachable; operator left "
        "— `moxi comms agents` is always current.)")


def test_render_agents_lists_the_operator_as_a_row():
    """The operator's binding is a row like any other, absent when unbound."""
    from moxi_comms import roster_view
    assert roster_view.render_agents({"bindings": []}) == (
        "no live bindings — nothing is addressable right now.")
    up = roster_view.render_agents({"bindings": [
        {"qualified": "operator", "tier": "standing", "adapter": "claude-shim", "slot": 4},
        {"qualified": P, "tier": "chosen", "adapter": "claude-shim", "slot": 1}]})
    assert up.splitlines() == [
        "operator                 standing   adapter claude-shim",
        f"{P:<24} chosen     adapter claude-shim"]


def test_comms_agents_and_the_snapshot_list_the_operator(rig, bind):
    from moxi_comms.adapters import shim_roster
    bind_operator(bind)
    wait_until(lambda: "operator" in rig.daemon.bindings)
    code, out = rig.cli(["agents"], **PLANNER)
    assert code == 0
    assert [ln for ln in out.splitlines() if ln.startswith("operator ")] == [
        "operator                 chosen     adapter claude-shim"]
    envelope, online = shim_roster.snapshot(daemon_socket(), env={})
    assert online and envelope.endswith("\n" + out.rstrip("\n"))


def test_the_delta_announces_the_operator_joining_and_leaving(rig, bind, tmp_path):
    from moxi_comms.adapters import shim_roster
    state = tmp_path / "roster.json"
    shim_roster.write_state(state, {})
    op = bind_operator(bind)
    wait_until(lambda: "operator" in rig.daemon.bindings)
    envelope, _ = shim_roster.delta(daemon_socket(), state, env={})
    assert envelope == ("(comms roster update: operator [chosen] is now reachable — "
                        "`moxi comms agents` is always current.)")
    op.close()
    wait_until(lambda: "operator" not in rig.daemon.bindings)
    envelope, _ = shim_roster.delta(daemon_socket(), state, env={})
    assert envelope == ("(comms roster update: operator left — `moxi comms agents` is always "
                        "current.)")


# ------------------------------------------------- the unanswered record

def note_unanswered(shim, message_id, author, at=None):
    shim._client.notify("unanswered", identity=shim.identity, message_id=message_id,
                        author=author, **({"at": at} if at is not None else {}))


def unanswered(rig):
    with rig.daemon._lock:
        return {k[1]: dict(v) for k, v in rig.daemon.unanswered.items()}


def test_the_record_is_kept_from_the_owing_bindings_own_connection(rig):
    reviewer = rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    note_unanswered(reviewer, task.id, P)
    assert wait_until(lambda: task.id in unanswered(rig))
    assert unanswered(rig)[task.id]["owing"] == R


def test_a_forged_notify_from_another_connection_is_dropped(rig):
    rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    forger = DaemonClient(daemon_socket()).connect()
    try:
        forger.notify("unanswered", identity=R, message_id=task.id, author=P)
        forger.query("agents")                  # a round trip: the notify has landed
    finally:
        forger.close()
    assert unanswered(rig) == {}


def test_the_owing_names_payment_drops_the_record(rig):
    reviewer = rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    note_unanswered(reviewer, task.id, P)
    wait_until(lambda: task.id in unanswered(rig))
    send(rig, "planner", "asking back", "--posture", "once", who=REVIEWER)
    assert task.id in unanswered(rig), "asking back pays nothing"
    reply(rig, task.id, "done", who=REVIEWER)
    assert wait_until(lambda: task.id not in unanswered(rig))


def test_a_payment_already_in_the_index_records_nothing(rig):
    reviewer = rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    paid = reply(rig, task.id, "done", who=REVIEWER)
    indexed(rig, paid.id)
    note_unanswered(reviewer, task.id, P)
    reviewer._client.query("agents")
    assert unanswered(rig) == {}


def test_owed_ttl_drops_the_record(rig):
    reviewer = rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    note_unanswered(reviewer, task.id, P, at=rig.clock())
    wait_until(lambda: task.id in unanswered(rig))
    rig.clock.advance(constants.OWED_TTL + 1)
    code, out = rig.cli(["agents", "--seats"], **PLANNER)
    assert "unanswered" not in out and unanswered(rig) == {}


def test_the_owing_binding_ending_drops_the_record(rig):
    reviewer = rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    note_unanswered(reviewer, task.id, P)
    wait_until(lambda: task.id in unanswered(rig))
    reviewer.stop()
    assert wait_until(lambda: unanswered(rig) == {})


def test_comms_agents_lists_the_record(rig):
    reviewer = rig.shim(**REVIEWER)
    task = send(rig, "reviewer", "build it")
    note_unanswered(reviewer, task.id, P, at=rig.clock() - 40 * 60)
    wait_until(lambda: task.id in unanswered(rig))
    code, out = rig.cli(["agents", "--seats"], **PLANNER)
    assert f"unanswered: {R} owes {P} msg {task.id}, 40m" in out.splitlines()
    code, out = rig.cli(["agents"], **PLANNER)
    assert "unanswered" not in out, "the default view lists messageable names only"


def test_the_authors_delta_shows_each_record_naming_it_once(rig, tmp_path):
    from moxi_comms.adapters import shim_roster
    reviewer = rig.shim(**REVIEWER)
    state = tmp_path / "roster.json"
    env = {"MOXI_COMMS_NAME": "planner", "MOXI_COMMS_SCOPE": "global"}
    shim_roster.delta(daemon_socket(), state, env=env)               # seed
    task = send(rig, "reviewer", "build it")
    note_unanswered(reviewer, task.id, P)
    wait_until(lambda: task.id in unanswered(rig))
    envelope, _ = shim_roster.delta(daemon_socket(), state, env=env)
    assert envelope == f"(comms: {R} ended a turn without answering your msg {task.id}.)"
    assert shim_roster.delta(daemon_socket(), state, env=env)[0] is None, "once each"
    other, _ = shim_roster.delta(daemon_socket(), tmp_path / "other.json",
                                 env={"MOXI_COMMS_NAME": "critic", "MOXI_COMMS_SCOPE": "global"})
    assert other is None, "only the author is told"


# ------------------------------------------------------- no hook

def test_comms_writes_no_claude_hook_settings():
    """A Claude session's Stop reaches comms through turns' holder (`claude_adapter`),
    so the launcher writes no hook settings at all."""
    from moxi_comms.adapters import session
    assert not hasattr(session, "hook_settings")


# ================================================= the ledger and the Stop

T0 = 1790000000.0                           # 2026-09-21T...; any fixed instant will do
STAMP = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0))
LEAD_Q = "lead"
QUIET = {"stop_hook_active": False, "task_types": [], "crons": 0}
AGAIN = {"stop_hook_active": True, "task_types": [], "crons": 0}


def msg(mid, author=LEAD_Q, *, tier="chosen", attribution="verified", **extra):
    """A delivered payload from `author`, stamped T0."""
    extra.setdefault("timestamp", STAMP)
    return payload(message_id=str(mid),
                   author={"name": author, "tier": tier, "attribution": attribution},
                   **extra)


class Answers:
    """The `owed` query, answered from a table a test sets, and every call recorded."""

    def __init__(self):
        self.calls = []
        self.sent, self.referents, self.awaiting = {}, {}, []
        self.undelivered, self.floor, self.error, self.raw = False, 0.0, None, None

    def __call__(self, identity, authors, referents):
        self.calls.append((identity, dict(authors), list(referents)))
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return {"sent": {a: list(self.sent.get(a, [])) for a in authors},
                "referents": {r: self.referents.get(r) for r in referents},
                "awaiting": list(self.awaiting), "undelivered": self.undelivered,
                "floor": self.floor}


@pytest.fixture
def unit():
    """A shim with no daemon: the `owed` query answered by `Answers`, a clock 5 minutes
    past T0, reviewer declaring lead its director, registration high-water 100."""
    from moxi_comms.adapters.claude_shim import ClaudeShim
    answers = Answers()
    clock = lambda: T0 + 300                                     # noqa: E731
    shim = ClaudeShim(injector=lambda text: None, directors=(LEAD_Q,), clock=clock,
                      owed_query=answers, session_id="sess-a")
    shim.identity = R
    shim.ledger.hwm = "100"
    shim.answers = answers
    yield shim
    shim.stall.cancel()


def inject(shim, *payloads):
    """Queue and flush: the shim injects them, and the ledger opens at injection."""
    shim.queue.extend(payloads)
    shim._flush()


def owed_ids(shim):
    return sorted(shim.ledger.debts)


def blocked_on(shim, fields=QUIET):
    """One Stop; the names it blocks on, marked pending as a printed block would."""
    decision = shim.on_stop(dict(fields))
    if decision.block:
        shim.mark_blocked(decision.names)
    return decision


def boundary(shim):
    """What the control server does for a Stop the shim did not block."""
    shim.on_turn_boundary()


# ---------------------------------------------------------------- each clause

def test_owing_branch_clause_two_operator_by_name_and_by_tier():
    from moxi_comms import owed
    assert owed.owing_branch(msg(1, "operator")) == "operator"
    assert owed.owing_branch(msg(1, "alice", tier="operator")) == "operator"


def test_owing_branch_a_director_needs_the_declaration_verified_and_once():
    from moxi_comms import owed
    assert owed.owing_branch(msg(1), (LEAD_Q,)) == "director"
    assert owed.owing_branch(msg(1), ()) == "peer", "a verified once owes from anyone"
    assert owed.owing_branch(msg(1, posture="open"), (LEAD_Q,)) is None, \
        "a director's open mail owes nothing"
    assert owed.owing_branch(msg(1, attribution="labeled"), (LEAD_Q,)) is None


def test_archivist_mail_owes_as_any_authors_and_an_old_mark_counts_for_nothing():
    """An old `[operator-words]` message from archivist (open, as it parses) owes
    nothing, even from an older daemon's payload that still carries the key; archivist's
    once ask owes as a peer's or a director's does."""
    from moxi_comms import owed
    assert owed.owing_branch(msg(1, "archivist", operator_words=True, posture="open")) is None
    assert owed.owing_branch(msg(1, "archivist", posture="open")) is None
    assert owed.owing_branch(msg(1, "archivist")) == "peer"
    assert owed.owing_branch(msg(1, "archivist"), ("archivist",)) == "director"


def test_owing_branch_clause_one_no_reply_owes_nothing():
    from moxi_comms import owed
    assert owed.owing_branch(msg(1, posture="none"), (LEAD_Q,)) is None
    assert owed.owing_branch(msg(1, "operator", posture="none")) is None
    assert owed.owing_branch(msg(1, posture="once"), (LEAD_Q,)) == "director"


def test_an_old_speaker_message_from_the_operator_owes_and_one_from_a_peer_does_not():
    """An old `[speaker]` line has no posture token and parses as open: from the operator it owes, from a peer it owes nothing."""
    from moxi_comms import owed
    posture = machine_line.parse("::to reviewer@proj-a [speaker]\nfyi").posture
    assert posture == "open"
    assert owed.owing_branch(msg(1, "operator", speaker=True, posture=posture)) == "operator"
    assert owed.owing_branch(msg(1, P, speaker=True, posture=posture)) is None
    assert owed.owing_branch(msg(1, speaker=True, posture=posture), (LEAD_Q,)) is None


def test_clause_one_nothing_owes_until_it_is_injected(unit):
    unit.queue.append(msg(101))
    assert owed_ids(unit) == []
    unit._flush()
    assert owed_ids(unit) == ["101"]


def test_clause_one_an_injector_that_raises_opens_nothing(unit):
    def broken(text):
        raise RuntimeError("the channel is gone")
    unit.injector = broken
    unit.queue.append(msg(101))
    with pytest.raises(RuntimeError):
        unit._flush()
    assert owed_ids(unit) == []


def test_a_peers_open_mail_opens_no_debt_and_its_once_ask_does(unit):
    inject(unit, msg(101, P, posture="open"))
    assert owed_ids(unit) == []
    inject(unit, msg(102, P))
    assert owed_ids(unit) == ["102"] and unit.ledger.debts["102"].branch == "peer"


def test_clause_five_a_message_a_day_old_opens_no_debt_and_one_expires(unit):
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 + 300 - constants.OWED_TTL))
    inject(unit, msg(101, timestamp=old), msg(102))
    assert owed_ids(unit) == ["102"]
    unit.clock = lambda: T0 + constants.OWED_TTL + 1
    assert unit.on_stop(dict(QUIET)).block is None
    assert owed_ids(unit) == [] and unit.answers.calls == [], "step 1: nothing, no query"


def test_clause_four_a_director_answer_threaded_to_rs_payment_owes_only_on_once(unit):
    """The loop-breaker reaches no debt: a director's mail owes only on once, which it
    never breaks, and an open answer owes nothing in the first place."""
    inject(unit, msg(101))
    unit.answers.sent = {LEAD_Q: [{"id": "105", "posture": "open"}]}
    assert unit.on_stop(dict(QUIET)).block is None        # the Stop learns the payment
    assert "105" in unit.ledger.payments and owed_ids(unit) == []
    inject(unit, msg(110, reply_to="105", posture="open"))
    unit.answers.sent = {}
    unit.answers.referents = {"105": {"own": True, "posture": "open"}}
    assert unit.on_stop(dict(QUIET)).block is None
    assert owed_ids(unit) == []
    inject(unit, msg(111, reply_to="105"))
    assert unit.on_stop(dict(QUIET)).names == ("111",)


def test_clause_four_does_not_apply_to_a_reply_once(unit):
    unit.ledger.payments["105"] = T0
    inject(unit, msg(110, reply_to="105", posture="once"))
    assert unit.on_stop(dict(QUIET)).names == ("110",)


def test_clause_four_exempts_the_operator(unit):
    unit.ledger.payments["105"] = T0
    inject(unit, msg(110, "operator", reply_to="105"))
    assert unit.on_stop(dict(QUIET)).names == ("110",)


def test_clause_four_a_once_at_or_below_the_high_water_still_owes(unit):
    inject(unit, msg(110, reply_to="99"))
    unit.answers.referents = {"99": {"own": True, "posture": "open"}}
    assert unit.on_stop(dict(QUIET)).names == ("110",)


def test_clause_four_a_once_whose_referent_the_index_does_not_hold_still_owes(unit):
    inject(unit, msg(110, reply_to="105"))
    assert unit.on_stop(dict(QUIET)).names == ("110",)


def test_clause_four_rs_unprompted_message_above_the_high_water_owes(unit):
    inject(unit, msg(110, reply_to="105"))
    unit.answers.referents = {"105": {"own": True, "posture": "open"}}
    assert unit.on_stop(dict(QUIET)).names == ("110",)


def test_clause_four_threaded_to_someone_elses_message_owes(unit):
    inject(unit, msg(110, reply_to="105"))
    unit.answers.referents = {"105": {"own": False, "posture": "open"}}
    assert unit.on_stop(dict(QUIET)).names == ("110",)


def test_clause_six_a_paid_debt_leaves(unit):
    inject(unit, msg(101))
    unit.answers.sent = {LEAD_Q: [{"id": "102", "posture": "open"}]}
    assert unit.on_stop(dict(QUIET)).block is None
    assert owed_ids(unit) == []


# ---------------------------------------------------------------------- payments

@pytest.mark.parametrize("posture", ["open", "none"])
def test_a_plain_or_no_reply_message_to_the_author_pays(unit, posture):
    inject(unit, msg(101), msg(103))
    unit.answers.sent = {LEAD_Q: [{"id": "104", "posture": posture}]}
    unit.on_stop(dict(QUIET))
    assert owed_ids(unit) == [], "it pays every owed message from the author older than it"


def test_a_payment_pays_only_what_is_older_than_it(unit):
    inject(unit, msg(101), msg(105))
    unit.answers.sent = {LEAD_Q: [{"id": "103", "posture": "open"}]}
    unit.on_stop(dict(QUIET))
    assert owed_ids(unit) == ["105"]


def test_asking_back_with_once_pays_nothing(unit):
    inject(unit, msg(101))
    unit.answers.sent = {LEAD_Q: [{"id": "102", "posture": "once"}]}
    assert unit.on_stop(dict(QUIET)).names == ("101",)
    assert "102" not in unit.ledger.payments


def test_only_a_message_to_operator_pays_an_operator_debt(unit):
    """A message to archivist pays no operator debt; one to `operator` does."""
    inject(unit, msg(101, "operator"))
    base = {"referents": {}, "awaiting": [], "undelivered": False, "floor": 0.0}
    unit.answers.raw = {**base, "sent": {
        "operator": [], "archivist": [{"id": "102", "posture": "open"}]}}
    assert unit.on_stop(dict(QUIET)).names == ("101",)
    unit.answers.raw = {**base, "sent": {"operator": [{"id": "103", "posture": "open"}]}}
    unit.on_stop(dict(QUIET))
    assert owed_ids(unit) == []


def test_a_message_to_archivist_does_not_pay_a_director_debt(unit):
    inject(unit, msg(101))
    unit.answers.sent = {"archivist": [{"id": "102", "posture": "open"}]}
    assert unit.on_stop(dict(QUIET)).names == ("101",)


def test_a_payment_that_raced_the_injection_pays(unit):
    """R posted 102 after M's id (101) but before M was injected: it pays M."""
    inject(unit, msg(101))
    unit.answers.sent = {LEAD_Q: [{"id": "102", "posture": "open"}]}
    unit.on_stop(dict(QUIET))
    assert owed_ids(unit) == []


def test_the_query_asks_for_each_authors_oldest_unpaid_id_and_the_kept_referents(unit):
    inject(unit, msg(103), msg(101), msg(107, "operator"), msg(109, reply_to="104"),
           msg(111, reply_to="106", posture="once"))
    unit.on_stop(dict(QUIET))
    identity, authors, referents = unit.answers.calls[0]
    assert identity == R
    assert authors == {LEAD_Q: "101", "operator": "107"}
    assert referents == [], "a director's debt is a once, which needs no referent"


# ---------------------------------------------------------------- step by step

def test_step_one_an_empty_ledger_is_a_boundary_with_no_daemon_call(unit):
    assert unit.on_stop(dict(QUIET)).block is None and unit.answers.calls == []


@pytest.mark.parametrize("missing", ["stop_hook_active", "task_types", "crons"])
def test_step_two_a_missing_field_is_a_boundary_and_voids_the_marks(unit, missing):
    inject(unit, msg(101))
    assert blocked_on(unit).names == ("101",)
    fields = dict(AGAIN)
    del fields[missing]
    assert unit.on_stop(fields).block is None
    assert not unit.ledger.debts["101"].pending and not unit.ledger.debts["101"].counted
    assert unit.answers.calls[1:] == [], "no daemon call for a Stop missing a fact"


@pytest.mark.parametrize("fields", [
    {"stop_hook_active": "false", "task_types": [], "crons": 0},
    {"stop_hook_active": False, "task_types": "shell", "crons": 0},
    {"stop_hook_active": False, "task_types": [], "crons": True},
])
def test_step_two_an_ill_typed_field_is_missing(unit, fields):
    inject(unit, msg(101))
    assert unit.on_stop(fields).block is None


@pytest.mark.parametrize("fail", ["raise", "timeout", "not a dict"])
def test_step_three_a_failed_query_is_a_boundary_and_voids_the_marks(unit, fail):
    from moxi_comms.client import DaemonTimeout
    inject(unit, msg(101))
    blocked_on(unit)
    if fail == "raise":
        unit.answers.error = RuntimeError("unknown query kind 'owed'")
    elif fail == "timeout":
        unit.answers.error = DaemonTimeout("timeout waiting for query")
    else:
        unit.answers.raw = ["nope"]
    assert unit.on_stop(dict(AGAIN)).block is None
    assert not unit.ledger.debts["101"].pending and not unit.ledger.debts["101"].counted


def test_step_three_an_unbound_shim_cannot_ask_and_the_stop_passes(unit):
    inject(unit, msg(101))
    unit.identity = None
    assert unit.on_stop(dict(QUIET)).block is None and unit.answers.calls == []


def test_one_undecidable_debt_never_suppresses_another(unit):
    """The floor's second half: 101 is older than the floor, 103 is not."""
    inject(unit, msg(101), msg(103, timestamp=time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 + 200))))
    unit.answers.floor = T0 + 100
    assert unit.on_stop(dict(QUIET)).names == ("103",)
    unit.mark_blocked(("103",))
    decision = unit.on_stop(dict(AGAIN))
    assert [d.message_id for d in decision.dropped] == ["103"]
    assert owed_ids(unit) == ["101"], "the undecidable one neither blocked nor recorded"


def test_a_missing_floor_makes_every_debt_undecidable(unit):
    inject(unit, msg(101))
    unit.answers.floor = None
    assert unit.on_stop(dict(QUIET)).block is None


@pytest.mark.parametrize("fields,answer", [
    ({"task_types": ["shell"]}, {}),
    ({"task_types": ["subagent"]}, {}),
    ({"task_types": ["monitor"]}, {}),
    ({"task_types": ["something-new"]}, {}),
    ({"task_types": ["unknown"]}, {}),
    ({"crons": 1}, {}),
    ({}, {"undelivered": True}),
    ({}, {"awaiting": [P]}),
])
def test_step_four_work_that_will_wake_the_session_passes_the_stop(unit, fields, answer):
    inject(unit, msg(101))
    for key, value in answer.items():
        setattr(unit.answers, key, value)
    assert unit.on_stop({**QUIET, **fields}).block is None
    assert owed_ids(unit) == ["101"], "nothing counted or dropped while work runs"


def test_step_four_queued_mail_is_work(unit):
    inject(unit, msg(101))
    unit.queue.append(msg(102, P))
    assert unit.on_stop(dict(QUIET)).block is None


def test_step_four_harness_internal_tasks_and_recurring_crons_are_not_work(unit):
    inject(unit, msg(101))
    assert unit.on_stop({"stop_hook_active": False, "task_types": ["dream",
                                                                   "auto_mode_scan"],
                         "crons": 0}).names == ("101",)


def test_step_five_another_hooks_block_counts_and_drops_nothing(unit):
    inject(unit, msg(101))
    assert unit.turn_blocks == 0
    assert unit.on_stop(dict(AGAIN)).block is None
    assert owed_ids(unit) == ["101"] and not unit.ledger.debts["101"].counted


def test_turn_blocks_caps_a_turn_at_one_block(unit):
    inject(unit, msg(101))
    assert blocked_on(unit).names == ("101",)
    unit.ledger.open(msg(103), (LEAD_Q,), "sess-a", T0 + 300)   # arrived mid-turn
    decision = unit.on_stop(dict(AGAIN))
    assert decision.block is None, "one block per turn"
    assert [d.message_id for d in decision.dropped] == ["101"]
    boundary(unit)
    assert unit.turn_blocks == 0
    assert blocked_on(unit).names == ("103",), "the next turn may block once"


def test_a_counted_debt_is_dropped_and_never_reminded_again(unit):
    inject(unit, msg(101))
    blocked_on(unit)
    decision = unit.on_stop(dict(AGAIN))
    assert [d.message_id for d in decision.dropped] == ["101"]
    boundary(unit)
    assert "101" not in unit.ledger.debts
    assert unit.on_stop(dict(QUIET)).block is None


# ------------------------------------------------------------------ the sequences

def test_sequence_the_plain_case(unit):
    inject(unit, msg(101))
    d = blocked_on(unit)                                    # Stop 1
    assert d.names == ("101",) and unit.blocked and unit.turn_blocks == 1
    assert unit.ledger.debts["101"].pending
    d = unit.on_stop(dict(AGAIN))                           # Stop 2
    assert d.block is None and [x.message_id for x in d.dropped] == ["101"]
    boundary(unit)
    assert not unit.blocked and unit.turn_blocks == 0 and owed_ids(unit) == []


def test_sequence_the_agent_delegates_in_the_continuation(unit):
    inject(unit, msg(101))
    blocked_on(unit)                                        # Stop 1
    unit.answers.awaiting = [P]                             # sent the worker --posture once
    assert unit.on_stop(dict(AGAIN)).block is None          # Stop 2: voided
    boundary(unit)
    debt = unit.ledger.debts["101"]
    assert not debt.pending and not debt.counted
    unit.answers.awaiting = []                              # the worker's answer wakes R
    assert blocked_on(unit).names == ("101",)               # Stop 3
    d = unit.on_stop(dict(AGAIN))                           # Stop 4
    assert [x.message_id for x in d.dropped] == ["101"]


@pytest.mark.parametrize("where", ["queued in the shim", "routed by the daemon"])
def test_sequence_the_workers_answer_lands_during_the_continuation(unit, where):
    inject(unit, msg(101))
    blocked_on(unit)                                        # Stop 1
    if where == "queued in the shim":
        unit.queue.append(msg(102, P, posture="open"))      # a blocked turn flushes nothing
    else:
        unit.answers.undelivered = True
    assert unit.on_stop(dict(AGAIN)).block is None          # Stop 2: voided
    assert not unit.ledger.debts["101"].counted
    boundary(unit)                                          # flushes the answer
    unit.answers.undelivered = False
    assert blocked_on(unit).names == ("101",)               # Stop 3: reminded again
    d = unit.on_stop(dict(AGAIN))                           # Stop 4
    assert [x.message_id for x in d.dropped] == ["101"]


@pytest.mark.parametrize("how", ["esc", "api error"])
def test_sequence_esc_or_an_api_error_voids_the_mark(unit, how):
    inject(unit, msg(101))
    blocked_on(unit)
    if how == "esc":
        unit.on_turn_boundary(failed=True)          # turns reports the turn aborted
    else:
        unit.on_turn_failed()
    debt = unit.ledger.debts["101"]
    assert not debt.pending and not debt.counted and not unit.blocked
    assert blocked_on(unit).names == ("101",), "the next quiet Stop reminds again"


# ---------------------------------------------------------- the context changes

def test_clear_empties_the_ledger(unit):
    inject(unit, msg(101))
    unit.on_session_start("clear", "sess-b")
    assert owed_ids(unit) == []


def test_a_resume_into_another_session_empties_it_and_the_same_session_keeps_it(unit):
    inject(unit, msg(101))
    unit.on_session_start("resume", "sess-a")
    assert owed_ids(unit) == ["101"]
    unit.on_session_start("compact", "sess-b")
    assert owed_ids(unit) == ["101"], "a compaction keeps them"
    unit.on_session_start("resume", "sess-b")
    assert owed_ids(unit) == []


def test_a_resume_with_no_session_id_empties_it(unit):
    inject(unit, msg(101))
    unit.on_session_start("resume", None)
    assert owed_ids(unit) == []


def test_a_new_shim_process_starts_with_an_empty_ledger():
    from moxi_comms.adapters.claude_shim import ClaudeShim
    assert ClaudeShim().ledger.debts == {}


# --------------------------------------------------------- the reminder's words

def test_the_reminder_for_one_director_debt():
    from moxi_comms import owed
    d = owed.Debt("4611686018427999600", LEAD_Q, "director", None, "open", T0, None)
    assert owed.reminder_text([d], T0 + 12 * 60) == (
        "comms: you are ending this turn without answering lead's msg "
        "4611686018427999600 (directive, 12m ago). Answer it now with\n"
        '  moxi comms reply 4611686018427999600 "the result, the blocker, or your question"\n'
        "If you need to ask your director or a peer before you can answer, add --posture "
        "once: that keeps it open. If it needs no answer, stop again.")


def test_the_reminder_for_one_operator_debt():
    from moxi_comms import owed
    d = owed.Debt("7", "operator", "operator", None, "open", T0, None)
    assert owed.reminder_text([d], T0 + 60) == (
        "comms: you are ending this turn without answering the operator's msg 7 "
        "(directive, 1m ago). Answer it now with\n"
        '  moxi comms reply 7 "the result, the blocker, or your question"\n'
        "If you already answered the operator at the terminal, stop again. If it needs "
        "no answer, stop again.")


def test_the_reminder_for_the_operator_drops_ask_back_too():
    from moxi_comms import owed
    d = owed.Debt("7", "operator", "operator", None, "open", T0, None)
    text = owed.reminder_text([d], T0 + 60)
    assert "the operator's msg 7 (directive, 1m ago)" in text
    assert "ask your director" not in text and owed.TERMINAL in text


def test_the_reminder_keeps_ask_back_when_any_debt_is_to_a_director():
    from moxi_comms import owed
    debts = [owed.Debt("7", "operator", "operator", None, "open", T0, None),
             owed.Debt("8", LEAD_Q, "director", None, "open", T0, None)]
    text = owed.reminder_text(debts, T0 + 60)
    assert owed.ASK_BACK in text and owed.TERMINAL in text


def test_the_reminder_lists_three_oldest_first_and_counts_the_rest():
    from moxi_comms import owed
    debts = [owed.Debt(str(i), LEAD_Q, "director", None, "open", T0, None)
             for i in (15, 11, 14, 12, 13)]
    lines = owed.reminder_text(debts, T0 + 60).splitlines()
    assert lines[0] == "comms: you are ending this turn without answering:"
    assert [line.split()[2] for line in lines[1:4]] == ["11", "12", "13"]
    assert lines[4] == "  and 2 more"
    assert lines[6] == '  moxi comms reply <id> "the result, the blocker, or your question"'


def test_the_reminder_never_asks_for_progress():
    from moxi_comms import owed
    d = owed.Debt("7", LEAD_Q, "director", None, "open", T0, None)
    assert "progress" not in owed.reminder_text([d], T0).lower()


# ------------------------------------------ end to end on the rig

@pytest.fixture
def pair(rig):
    """lead (global) and reviewer, whose declared director it is; both bound, so
    each one's messages are verified."""
    lead = rig.shim(slot=6, name="lead", global_=True)
    rev = rig.shim(**REVIEWER, directors=(LEAD_Q,))
    return lead, rev


def arrive(shim, message):
    """Wait for `message` to reach `shim`, then inject it (a boundary flushes)."""
    assert wait_until(lambda: any(p["message_id"] == message.id for p in shim.queue)), \
        f"{message.id} never reached {shim.identity}"
    shim.on_turn_boundary()


def settle(rig, shim, *after):
    """The Stop's query and settle, without the decision: what is owed now. `after`
    are ids that must be in the sent index first (the daemon routes asynchronously)."""
    for mid in after:
        indexed(rig, mid)
    authors, referents = shim.ledger.query_args()
    if authors:
        shim.ledger.settle(shim.owed_query(shim.identity, authors, referents), rig.clock())
    return owed_ids(shim)


def test_the_first_table_every_exchange_the_rule_creates_ends(rig, pair):
    lead, rev = pair
    m1 = send(rig, R, "build the fix", "--posture", "once", who=LEAD)
    arrive(rev, m1)
    assert owed_ids(rev) == [m1.id]                                  # 1: owes
    m2 = reply(rig, m1.id, "A or B?", "--posture", "once", who=REVIEWER)
    assert settle(rig, rev, m2.id) == [m1.id]                        # 2: not a payment
    m3 = reply(rig, m2.id, "B", who=LEAD)
    assert line_zero(m3).posture == "none"
    arrive(rev, m3)
    assert settle(rig, rev) == [m1.id]                               # 3: owes nothing
    m4 = reply(rig, m1.id, "done, commit abc", who=REVIEWER)
    assert line_zero(m4).posture == "none"
    assert settle(rig, rev, m4.id) == [] and m4.id in rev.ledger.payments   # 4: pays 1
    code, out = rig.cli(["reply", m4.id, "good"], **LEAD)            # 5: takes no reply
    assert code == 2 and "is marked [no reply]" in out


def test_the_second_table_an_unprompted_report(rig, pair):
    lead, rev = pair
    m1 = send(rig, LEAD_Q, "found a bug in X", who=REVIEWER)         # 1: nothing owed
    m2 = reply(rig, m1.id, "good, fix it", "--posture", "once", who=LEAD)
    arrive(rev, m2)
    assert settle(rig, rev, m1.id) == [m2.id]                        # 2: owes
    m3 = reply(rig, m2.id, "fixed, commit abc", who=REVIEWER)
    assert settle(rig, rev, m3.id) == []                             # 3: pays 2
    m4 = reply(rig, m3.id, "thanks", who=LEAD)
    arrive(rev, m4)
    assert settle(rig, rev) == []                                    # 4: owes nothing


def test_examples_a_no_reply_from_the_director_owes_nothing(rig, pair):
    lead, rev = pair
    arrive(rev, send(rig, R, "fyi, main moved", "--posture", "none", who=LEAD))
    assert owed_ids(rev) == []


def test_examples_a_once_ask_after_the_payment_owes(rig, pair):
    lead, rev = pair
    m1 = send(rig, R, "build it", "--posture", "once", who=LEAD)
    arrive(rev, m1)
    paid = reply(rig, m1.id, "done", who=REVIEWER)
    assert settle(rig, rev, paid.id) == []
    ask = send(rig, R, "which commit?", "--posture", "once", who=LEAD)
    arrive(rev, ask)
    assert settle(rig, rev) == [ask.id]


def test_examples_old_marked_archivist_mail_owes_nothing(rig, pair):
    """An `[operator-words]` line an older comms wrote reaches reviewer and owes
    nothing."""
    lead, rev = pair
    rig.shim(slot=5, name="archivist", global_=True)
    words = raw_post(rig, 5, "archivist", f"::to {R} [operator-words]\nhe asks: done?")
    arrive(rev, words)
    assert owed_ids(rev) == []


def test_examples_unmarked_archivist_mail_owes_nothing(rig, pair):
    lead, rev = pair
    arrive(rev, send(rig, R, "passed on", "--audience", "operator", who=ARCHIVIST))
    assert owed_ids(rev) == []


def test_examples_operator_mail_owes_and_only_operator_pays_while_the_operator_is_bound(
        rig, pair, bind):
    """With the operator bound, a message to archivist still pays nothing."""
    lead, rev = pair
    bind_operator(bind)
    wait_until(lambda: "operator" in rig.daemon.bindings)
    status = raw_post(rig, 3, "operator", f"::to {R}\nstatus?")
    arrive(rev, status)
    assert owed_ids(rev) == [status.id]
    assert rev.ledger.debts[status.id].branch == "operator"
    answer = send(rig, "archivist", "building, 10 minutes", who=REVIEWER)
    assert settle(rig, rev, answer.id) == [status.id]
    to_him = send(rig, "operator", "building, 10 minutes", who=REVIEWER)
    assert settle(rig, rev, to_him.id) == []


def test_examples_a_message_to_archivist_does_not_pay_the_operator_while_it_is_unbound(
        rig, pair):
    lead, rev = pair
    status = raw_post(rig, 3, "operator", f"::to {R}\nstatus?")
    arrive(rev, status)
    answer = send(rig, "archivist", "building", who=REVIEWER)
    assert settle(rig, rev, answer.id) == [status.id]
    to_him = send(rig, "operator", "building", who=REVIEWER)
    assert settle(rig, rev, to_him.id) == []


def test_examples_the_operators_own_lane_owes(rig, pair):
    lead, rev = pair
    typed = rig.post_as_operator([R], "status?")
    arrive(rev, typed)
    assert owed_ids(rev) == [typed.id]


def test_examples_a_peer_owes_nothing(rig, pair):
    lead, rev = pair
    rig.shim(**PLANNER)
    arrive(rev, send(rig, R, "can you look at this?"))
    assert owed_ids(rev) == []


def test_end_to_end_the_plain_case_is_reminded_once_then_recorded(rig, pair, tmp_path):
    from moxi_comms.adapters import shim_roster
    lead, rev = pair
    lead_memory = tmp_path / "lead-roster.json"
    lead_env = {"MOXI_COMMS_NAME": "lead", "MOXI_COMMS_GLOBAL": "1"}
    shim_roster.delta(daemon_socket(), lead_memory, env=lead_env)     # seed
    task = send(rig, R, "build the fix", "--posture", "once", who=LEAD)
    wait_for_queue(rev, 1)
    shim_drive.turn_ended(rev)                                        # injects it
    assert owed_ids(rev) == [task.id]
    # The Stop comes after the daemon has heard of the injection, as it does when a
    # model takes the turn: until then the daemon counts the message undelivered.
    assert wait_until(lambda: rig.daemon.injected_through.get(R) == task.id)
    decision = shim_drive.stop_and_end(rev, **QUIET)
    assert decision.block and f"lead's msg {task.id}" in decision.block
    decision = shim_drive.stop_and_end(rev, **AGAIN)
    assert not decision.block and owed_ids(rev) == [] and not rev.blocked
    assert wait_until(lambda: (R, task.id) in rig.daemon.unanswered)
    code, out = rig.cli(["agents", "--seats"], **PLANNER)
    assert f"unanswered: {R} owes lead msg {task.id}" in out
    envelope, _ = shim_roster.delta(daemon_socket(), lead_memory, env=lead_env)
    assert f"ended a turn without answering your msg {task.id}" in envelope


def test_end_to_end_a_payment_in_the_continuation_clears_the_debt(rig, pair):
    lead, rev = pair
    task = send(rig, R, "build the fix", "--posture", "once", who=LEAD)
    wait_for_queue(rev, 1)
    shim_drive.turn_ended(rev)                                        # injects it
    assert wait_until(lambda: rig.daemon.injected_through.get(R) == task.id)
    assert shim_drive.stop_and_end(rev, **QUIET).block
    answer = reply(rig, task.id, "done, commit abc", who=REVIEWER)
    indexed(rig, answer.id)
    decision = shim_drive.stop_and_end(rev, **AGAIN)
    assert not decision.block and owed_ids(rev) == []
    assert rig.daemon.unanswered == {}, "a paid debt is never recorded"


def test_end_to_end_a_delegation_with_reply_once_waits_without_a_reminder(rig, pair):
    lead, rev = pair
    worker = rig.shim(**PLANNER)
    task = send(rig, R, "build the fix", "--posture", "once", who=LEAD)
    arrive(rev, task)
    ask = send(rig, "planner", "please build it", "--posture", "once", who=REVIEWER)
    indexed(rig, ask.id)
    assert rev.on_stop(dict(QUIET)).block is None, "awaiting the worker is work"
    answer = reply(rig, ask.id, "built", who=PLANNER)
    indexed(rig, answer.id)
    wait_for_queue(rev, 1)
    rev.on_turn_boundary()                          # the worker's answer wakes R
    # `injected` is a notify on the binding's connection and the owed query opens its
    # own, so the query can be served first and read the answer as `undelivered`.
    assert wait_until(lambda: int(rig.daemon.injected_through.get(rev.identity) or 0)
                      >= int(answer.id))
    assert rev.on_stop(dict(QUIET)).names == (task.id,)
    assert worker


# ------------------------------------------------- the context block,

QUIET_RULE = ("Message your director only when it needs you (the result, a blocker, a "
              "question for the operator); progress goes in your records.")


def test_the_quiet_rule_is_the_director_line_and_the_block_has_no_speaker_sentence():
    from moxi_comms.context import CONTEXT_BLOCK, DIRECTOR_LINE
    assert QUIET_RULE == DIRECTOR_LINE and QUIET_RULE not in CONTEXT_BLOCK
    assert "[speaker]" not in CONTEXT_BLOCK
    assert len([s for s in CONTEXT_BLOCK.split(". ") if s.strip()]) == 7, "seven"


# ------------------------------------------------------- the glossary entry

@pytest.mark.parametrize("form", ["owed", "reminder", "payment", "Owed"])
def test_the_owed_entry_and_its_aliases_resolve(form):
    from moxi_comms import glossary
    assert glossary.lookup(form) is glossary.lookup("owed") is not None


def test_the_owed_entry_says_what_a_debt_is():
    from moxi_comms import glossary
    text = glossary.lookup("owed").text
    assert text.startswith("a reply you owe. A `[reply-once]` message owes one reply, "
                           "whoever sent it, unless it is marked `(unverified)`. So does "
                           "mail from the operator, unless it is marked `[no reply]` or "
                           "`[reply-optional]`.")
    assert text.endswith("the wait ends when it answers.")


def test_comms_help_owed_prints_it(rig):
    code, out = rig.cli(["help", "owed"], **PLANNER)
    assert code == 0 and out.startswith("owed — a reply you owe.")


# ------------------------------------------------------------- a few joins, end to end

def test_the_shim_keeps_its_first_registrations_high_water_id(rig):
    send(rig, "critic", "before the shim")
    before = last(rig).id
    rev = rig.shim(**REVIEWER)
    assert rev.ledger.hwm == rig.daemon.bindings[R].replay_hwm
    assert int(rev.ledger.hwm) >= int(before)


# ------------------- only a message to operator pays an operator debt

def test_the_unanswered_record_drops_only_on_a_message_to_operator(rig):
    """daemon `_pays`: a message to archivist pays no operator debt."""
    from moxi_comms.daemon import SentEntry

    to_archivist = SentEntry(id="200", author=R, tags=("archivist",), posture="open",
                           reply_to=None, at=0.0, verified=True)
    to_him = SentEntry(id="201", author=R, tags=("operator",), posture="open",
                       reply_to=None, at=0.0, verified=True)
    assert rig.daemon._pays(to_archivist, R, "operator", "100") is False
    assert rig.daemon._pays(to_him, R, "operator", "100") is True

