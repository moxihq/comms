"""The `attributed_message` read: one message with its author as the daemon attributes it.

A read-only daemon query returns one message's attributed author (name, tier, verified
or unverified) and its recipients, read by the daemon's own attribution code. A program
outside comms asks it for a message an agent points it at, and takes the sender and text
from the answer.

Every rig test here runs on the mock and on the local backend (`BOTH_BACKENDS`).
"""

from __future__ import annotations

import pytest
from conftest import queued, wait_for_queue, wait_until

from moxi_comms import client as client_mod
from moxi_comms import cursors, machine_line, provision
from moxi_comms.claimhold import ClaimServer
from moxi_comms.client import (ATTRIBUTED_FIELDS, DaemonClient, DaemonError,
                               DaemonOffline, attributed_message)
from moxi_comms.mock_gateway import NO_MENTIONS
from moxi_comms.paths import daemon_socket

from test_reply_enforcement import bind  # noqa: F401 -- the bare-binding fixture

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
P, R = "planner@proj-a", "reviewer@proj-a"


def raw(message_id):
    """The daemon's answer as it comes off the socket, `id` and `ok` taken off."""
    c = DaemonClient(daemon_socket()).connect()
    try:
        answer = c.query("attributed_message", message_id=str(message_id))
    finally:
        c.close()
    answer.pop("id", None)
    assert answer.pop("ok") is True
    return answer


def send(rig, to, text, who=PLANNER):
    code, out = rig.cli(["send", "--posture", "none", to, text], **who)
    assert code == 0, out
    return rig.gateway.history(rig.names_channel)[-1]


def matches_delivery(answer, payload):
    """The read says what the delivered frame said, field for field."""
    for key in ("message_id", "room", "author", "recipients", "text", "timestamp"):
        assert answer[key] == payload[key], key


# --------------------------------------------------------------------------- attribution

def test_a_verified_send_reads_as_its_delivery_shows(rig):
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    msg = send(rig, R, "please look")
    wait_for_queue(reviewer, 1)
    payload = reviewer.queue[0]
    answer = attributed_message(msg.id)
    assert payload["author"] == {"name": P, "tier": "chosen", "attribution": "verified"}
    matches_delivery(answer, payload)
    assert answer["resolved"] == [R]
    assert set(answer) == set(ATTRIBUTED_FIELDS)


def test_a_live_slot_post_under_another_name_reads_unverified_as_delivered(rig):
    """An out-of-band spender of planner's live slot wears `ghost@proj-a`: delivery
    says `labeled`, and so does the read."""
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    ghost = rig.post_from_slot(1, [R], "not the holder", display="ghost@proj-a")
    wait_for_queue(reviewer, 1)
    payload = reviewer.queue[0]
    assert payload["author"] == {"name": "ghost@proj-a", "tier": "chosen",
                                 "attribution": "labeled"}
    matches_delivery(attributed_message(ghost.id), payload)


def test_a_slot_post_nobody_held_reads_unverified_as_replay_delivered_it(rig):
    rig.post_from_slot(6, ["curator@proj-a"], "who am i", display="ghost@proj-a")
    posted = rig.gateway.history(rig.names_channel)[-1]
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 1)
    payload = curator.queue[0]
    assert payload["author"]["attribution"] == "labeled"
    matches_delivery(attributed_message(posted.id), payload)


def test_the_read_answers_for_the_messages_own_time_after_the_sender_has_gone(rig):
    """Live delivery verified it from the binding; the sender then unbinds. The read
    asks the custody journal who held the seat when the message was posted, so it still
    says verified, and names the sender, not whoever holds the slot now."""
    planner = rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    msg = send(rig, R, "before I go")
    wait_for_queue(reviewer, 1)
    planner.stop()
    assert wait_until(lambda: P not in rig.daemon.bindings)
    matches_delivery(attributed_message(msg.id), reviewer.queue[0])


def test_a_seat_bound_after_the_post_does_not_verify_it(rig):
    """A post through slot 1's webhook while nobody held slot 1, wearing planner's name:
    delivery said `labeled`. planner then binds slot 1. Asked about the binding as it
    is now, the post would read verified; the read asks about the post's own time, so
    it stays what delivery said."""
    reviewer = rig.shim(**REVIEWER)
    msg = rig.post_from_slot(1, [R], "before anyone held the seat", display=P)
    wait_for_queue(reviewer, 1)
    payload = reviewer.queue[0]
    assert payload["author"] == {"name": P, "tier": "chosen", "attribution": "labeled"}
    rig.clock.advance(5)        # the bind comes later than the post, as it would
    rig.shim(**PLANNER)
    assert wait_until(lambda: rig.daemon.by_slot.get(1) == P)
    assert rig.daemon.attribute(msg, live=True).attribution == "verified", \
        "the live answer would have been wrong here"
    matches_delivery(attributed_message(msg.id), payload)


def test_a_standing_identity_reads_verified_and_standing(rig):
    rig.shim(slot=1, name="main", scope="proj-a")
    reviewer = rig.shim(**REVIEWER)
    msg = send(rig, R, "a note", who=dict(slot=1, name="main", scope="proj-a"))
    wait_for_queue(reviewer, 1)
    payload = reviewer.queue[0]
    assert payload["author"] == {"name": "main@proj-a", "tier": "standing",
                                 "attribution": "verified"}
    matches_delivery(attributed_message(msg.id), payload)


def test_the_operator_reads_as_the_operator(rig):
    reviewer = rig.shim(**REVIEWER)
    msg = rig.post_as_operator([R], "ship it")
    wait_for_queue(reviewer, 1)
    payload = reviewer.queue[0]
    assert payload["author"] == {"name": "operator", "tier": "operator",
                                 "attribution": "verified"}
    matches_delivery(attributed_message(msg.id), payload)


# --------------------------------------------------------------------------- recipients

def test_a_multi_recipient_send_names_every_recipient(rig):
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    critic = rig.shim(slot=3, name="critic", scope="proj-a")
    msg = send(rig, "reviewer@proj-a,critic@proj-a", "both of you")
    wait_for_queue(reviewer, 1)
    wait_for_queue(critic, 1)
    answer = attributed_message(msg.id)
    assert answer["recipients"] == reviewer.queue[0]["recipients"] == \
        ["reviewer@proj-a", "critic@proj-a"]
    assert answer["resolved"] == [R, "critic@proj-a"]
    assert answer["text"] == "both of you"


def test_a_bare_operator_tag_resolves_to_the_global_binding_that_holds_it(rig, bind):
    """`resolved` is the daemon's own resolve, by the one rule: a bare
    `operator` is the global identity whoever wrote it, so from proj-a it lands on the
    global binding, and an explicit `operator@proj-a` is taken as written."""
    rig.shim(**PLANNER)
    bind(slot=4, name="operator", global_=True, adapter="codex")
    assert wait_until(lambda: "operator" in rig.daemon.bindings)
    bare = rig.post_from_slot(1, ["operator"], "to him", display=P)
    scoped = rig.post_from_slot(1, ["operator@proj-a"], "to someone else", display=P)
    assert attributed_message(bare.id)["resolved"] == ["operator"]
    answer = attributed_message(scoped.id)
    assert (answer["recipients"], answer["resolved"]) == \
        (["operator@proj-a"], ["operator@proj-a"])


def test_a_message_in_another_room_names_its_room(rig):
    """Only `#names` is routed; the read says which room a message is in, so a caller
    can tell a `::to` line in another channel of the record (delivered to nobody) from
    mail. The channel here is one the test makes: comms provisions no other room an
    agent posts in."""
    rig.shim(**PLANNER)
    channel = rig.gateway.create_channel("#elsewhere")
    cred = rig.gateway.create_webhook(channel, "elsewhere", "test/elsewhere")
    msg = rig.gateway.execute_webhook(cred, machine_line.compose([R], "in the room"),
                                      username=P, allowed_mentions=NO_MENTIONS)
    answer = attributed_message(msg.id)
    assert (answer["room"], answer["recipients"], answer["text"]) == \
        ("#elsewhere", [R], "in the room")


# --------------------------------------------------------------------------- not found

def test_an_id_the_record_does_not_hold_is_not_found(rig):
    assert attributed_message("4611686018427399999") is None
    assert raw("4611686018427399999") == {
        "message_id": "4611686018427399999", "found": False, "room": None,
        "author": None, "recipients": [], "resolved": [], "text": None,
        "timestamp": None}


@pytest.mark.parametrize("bad", ["", "  ", "abc", "12x", "-5"])
def test_an_id_that_is_not_a_message_id_is_refused(rig, bad):
    with pytest.raises(DaemonError, match="must be a message id"):
        attributed_message(bad)


def test_with_the_daemon_down_the_read_is_offline(rig):
    rig.daemon.stop()
    with pytest.raises(DaemonOffline):
        attributed_message("4611686018427387905")


# --------------------------------------------------------------------------- read-only

def test_the_read_changes_nothing(rig):
    """No hold, pend, delivery, cursor, ask or notice state moves because it was asked."""
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    msg = send(rig, R, "one")
    wait_for_queue(reviewer, 1)
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a",
                         "waiting for curator"], **PLANNER)
    assert code == 0, out
    pend = rig.gateway.history(rig.names_channel)[-1]
    assert wait_until(lambda: queued(rig.daemon, "curator@proj-a"))
    d = rig.daemon

    def state():
        with d._lock:
            return ({k: dict(v) for k, v in d.queue.items()},
                    {k: list(v) for k, v in d.sent.items()},
                    dict(d.sent_by_id), dict(d.session_owner), d._seen_max,
                    {q: (b.session_id, b.turned, set(b.delivered))
                     for q, b in d.bindings.items()},
                    cursors.read(R), cursors.read("curator@proj-a"),
                    len(rig.gateway.history(rig.names_channel)),
                    len(reviewer.queue))

    before = state()
    for _ in range(3):
        attributed_message(msg.id)
        attributed_message(pend.id)
        attributed_message("4611686018427399999")
    assert state() == before


# --------------------------------------------------------------------------- who may ask

def test_any_socket_caller_may_ask_and_every_caller_gets_the_same_answer(rig):
    """The caller rule: as for `record_message`, `session` and `owner`, any process that
    can open the daemon's 0600 socket. A connection holding no binding and one holding
    a send-only binding (as such a program may) get the same answer."""
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    msg = send(rig, R, "same for everyone")
    wait_for_queue(reviewer, 1)
    code = provision.send_only_main(["relay"], gateway=rig.gateway, store=rig.store,
                                    out=lambda _l: None)
    assert code == 0
    srv = ClaimServer("relay", "global", kind="send-only", arm=True).start()
    try:
        assert wait_until(lambda: "relay" in rig.daemon.bindings), srv.refusal
        held = srv._client.attributed_message(msg.id)
    finally:
        srv.stop()
    assert held == attributed_message(msg.id)


def test_the_read_answers_no_more_than_history_already_shows(rig):
    """It widens nothing: the answer is the message, its author as attributed, and its
    recipients. No posting credential, slot, webhook id or binding fact rides it."""
    rig.shim(**PLANNER)
    reviewer = rig.shim(**REVIEWER)
    msg = send(rig, R, "just the facts")
    wait_for_queue(reviewer, 1)
    answer = raw(msg.id)
    assert set(answer) == {"found", *ATTRIBUTED_FIELDS}
    assert set(answer["author"]) == {"name", "tier", "attribution"}
    code, out = rig.cli(["history", R], **REVIEWER)
    assert code == 0
    assert (f"{msg.id}  {answer['timestamp']}  to   {P} (chosen, verified)  "
            f"-> reviewer@proj-a") in out


def test_the_module_function_closes_its_connection(rig, monkeypatch):
    closed = []
    real = DaemonClient.close
    monkeypatch.setattr(DaemonClient, "close",
                        lambda self: (closed.append(self), real(self))[1])
    assert client_mod.attributed_message("4611686018427399999") is None
    assert len(closed) == 1
