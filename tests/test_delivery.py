"""Send -> route -> digest -> inject -> complete -> cursor advance -> reply.

The one queue: mail to a name that is not bound is a row in the queue, released at the
name's next release point (at a bind, on a passing read) with `held: true` and the
`[held] sent …, waited …` mark, and deleted only when the adapter names it handled.
Replay past the cursor stays for mail with no row: the tests that exercise replay and its bounds post while the daemon is down,
beyond the boot rescan's window (`posted_while_down`), which is that case.
"""

import itertools
import re
import time

from conftest import send_turn, wait_for_queue, wait_until, write_config

from moxi_comms import cli, constants, cursors, machine_line
from moxi_comms.daemon import Daemon


def _queue(rig, q):
    """The ids with a row for `q` in the daemon's memory."""
    with rig.daemon._lock:
        return set(rig.daemon.queue.get(q, {}))


def _waiting(rig, q):
    """`agents`' `waiting` count for `q`, 0 when it has none."""
    return rig.daemon._query({"kind": "agents"})["waiting"].get(q, {}).get("count", 0)


def posted_while_down(rig, monkeypatch, post):
    """Run `post` while the daemon is down, then boot it with a rescan window that holds
    only a later filler message, so what `post` made has no row: it reaches a name by
    replay past its cursor (mail posted while the daemon was down beyond the boot
    rescan's window), not by a release."""
    rig.daemon.stop()
    made = post()
    rig.post_from_slot(6, ["main@proj-b"], "filler: the one message the boot rescan reads")
    monkeypatch.setattr(constants, "RESCAN_LIMIT", 1)
    rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    return made


def test_happy_path_end_to_end(rig):
    """A sender posts a tagged message; the daemon authenticates the webhook, parses the
    machine line at position zero, routes to the bound recipient's adapter; the shim
    holds until the turn boundary, injects ONE coalesced digest, and advances the cursor
    when the turn runs to completion."""
    planner = rig.shim(slot=1, name="planner", scope="proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")

    code, out = rig.cli(["send", "--posture", "open", "reviewer@proj-a", "please look at the parser diff"],
                        slot=planner.slot_id, name="planner", scope="proj-a")
    assert code == 0 and "sent" in out

    wait_for_queue(reviewer, 1)
    payload = reviewer.queue[0]
    assert payload["author"] == {"name": "planner@proj-a", "tier": "chosen",
                                 "attribution": "verified"}
    assert payload["recipients"] == ["reviewer@proj-a"]
    assert payload["backlog"] is False
    assert payload["text"] == "please look at the parser diff"
    assert "::to" not in payload["text"], "the machine line is transport, never content"
    assert payload["room"] == "#names"

    # nothing is injected until the turn boundary
    assert reviewer.digests == []
    digest = reviewer.on_turn_boundary()
    assert digest is not None
    assert "[comms] from planner@proj-a · msg" in digest, "chosen+verified prints neither"
    assert "please look at the parser diff" in digest
    assert "::to" not in digest

    # the cursor only advances when the injected turn RUNS TO COMPLETION
    assert cursors.read("reviewer@proj-a") is None
    reviewer.on_turn_boundary()
    assert cursors.read("reviewer@proj-a") == payload["message_id"]

    # ...and the reply threads back in the room the question was asked in
    code, out = rig.cli(["reply", payload["message_id"], "looks good"],
                        slot=reviewer.slot_id, name="reviewer", scope="proj-a")
    assert code == 0
    wait_for_queue(planner, 1)
    back = planner.queue[0]
    assert back["reply_to"] == payload["message_id"]
    assert back["author"]["name"] == "reviewer@proj-a"
    assert back["text"] == "looks good"


def test_backlog_coalesces_into_one_digest_live_arrives_singly(rig, monkeypatch):
    posted_while_down(rig, monkeypatch, lambda: [
        rig.post_from_slot(6, ["reviewer@proj-a"], text) for text in ("one", "two", "three")])
    assert _queue(rig, "reviewer@proj-a") == set(), "no row: replay's mail"
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    wait_for_queue(reviewer, 3)
    wait_until(lambda: reviewer.replaying is False)

    rig.clock.advance(3 * 3600)                    # the mail sat unread for three hours
    digest = reviewer.on_turn_boundary()
    # slot 6 is nobody's binding, so replay can only offer the display name: the batch
    # is unverified, and the header says it once for all three
    assert digest.startswith("== comms catch-up: 3 message(s) (oldest 3h ago; "
                             "attribution is unverified in replay) ==")
    assert digest.count("[comms] from") == 3
    assert "(unverified)" not in digest, "the header said it; the frames stay quiet"
    assert "backlog" not in digest, "replay is stated once, in the header"
    assert reviewer.digests == [digest]

    reviewer.on_turn_boundary()                    # completes the digest turn
    rig.post_from_slot(6, ["reviewer@proj-a"], "four")
    wait_for_queue(reviewer, 1)
    live = reviewer.on_turn_boundary()
    assert live.count("[comms] from") == 1, "live messages arrive individually"
    # A live single carries its count like any other batch -- one message announced, one
    # frame, one closing line.
    assert live.startswith("== comms: 1 message ==")
    assert live.splitlines()[-1].startswith("[comms] end · msg ")


def test_queued_mail_is_released_at_the_bind_as_one_digest_marked_held(rig):
    """Mail to a name that is not bound is a row; the bind's register releases
    the rows on a passing read, oldest first, each `held: true`, and the
    shim injects them as one digest whose frames say how long each waited.
    Released rows are not replay: each frame keeps its own attribution mark."""
    sent = [rig.post_from_slot(6, ["reviewer@proj-a"], text) for text in ("one", "two",
                                                                           "three")]
    assert wait_until(lambda: _queue(rig, "reviewer@proj-a") == {m.id for m in sent})
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    got = wait_for_queue(reviewer, 3)
    wait_until(lambda: reviewer.replaying is False)
    assert [p["message_id"] for p in got] == [m.id for m in sent]
    assert all(p["held"] is True and p["backlog"] is False for p in got)

    rig.clock.advance(3 * 3600)                    # the mail sat unread for three hours
    digest = reviewer.on_turn_boundary()
    assert digest.startswith("== comms: 3 messages ==")
    assert len(re.findall(r"\[held\] sent (\w{3} \d{2} )?\d{2}:\d{2}, waited 3h", digest)) == 3
    assert digest.count("(unverified)") == 3
    assert reviewer.digests == [digest]
    assert _queue(rig, "reviewer@proj-a") == {m.id for m in sent}, "rows wait for handled"
    send_turn(reviewer, handled=[m.id for m in sent])
    assert wait_until(lambda: _queue(rig, "reviewer@proj-a") == set())


def test_a_real_mixed_live_and_replay_digest_keeps_the_live_frames_mark(rig, monkeypatch):
    """The probe case, end to end: backlog waiting at bind, a live message landing before
    the first turn boundary, so ONE digest carries both. The header's note is about
    replay and speaks only for the replayed frame; the live arrival keeps its own
    `(unverified)`, because a frame with no mark means verified and
    that message was not. (The backlog is posted while the daemon is down, so it has no
    row and goes by replay.)"""
    posted_while_down(rig, monkeypatch, lambda: rig.post_from_slot(
        6, ["reviewer@proj-a"], "while you were out", display="ghost@proj-a"))
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    wait_for_queue(reviewer, 1)
    wait_until(lambda: reviewer.replaying is False)

    rig.post_from_slot(6, ["reviewer@proj-a"], "and this just came in",
                       display="stranger@proj-a")
    wait_for_queue(reviewer, 2)
    assert [p["backlog"] for p in sorted(reviewer.queue,
                                         key=lambda p: int(p["message_id"]))] == \
        [True, False], "one digest, genuinely mixed -- not two turns"

    digest = reviewer.on_turn_boundary()
    assert digest.splitlines()[0].startswith("== comms catch-up: 2 message(s)")
    assert "attribution is unverified in replay" in digest.splitlines()[0], \
        "true here: the only replayed frame really is unverified"
    assert "[comms] from ghost@proj-a · msg" in digest, "the header spoke for replay"
    assert "[comms] from stranger@proj-a (unverified) · msg" in digest, \
        "and not for the live arrival, which keeps its own mark"


def test_deferral_then_bind_then_release_then_handled(rig):
    """Deferral for chosen names comes along for free: an unbound chosen name's mail is
    a row in the queue, released at its bind, and gone once the adapter
    names it handled. (The roster's rendering of `waiting` is tested with the roster;
    this reads the `agents` answer it renders.)"""
    code, out = rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a",
                         "the corpus needs a pass"],
                        slot=1, name="planner", scope="proj-a")
    assert code == 0
    assert cli.NOT_BOUND_NOTE.format(name="curator@proj-a") in out

    # ...and nothing hides: waiting names and counts are visible, in the operator's view
    assert wait_until(lambda: _queue(rig, "curator@proj-a"))
    assert _waiting(rig, "curator@proj-a") == 1
    code, listing = rig.cli(["agents", "--seats"], slot=1)
    assert "curator@proj-a" in listing and "1 message(s)" in listing

    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 1)
    assert curator.queue[0]["held"] is True and curator.queue[0]["backlog"] is False
    assert curator.queue[0]["text"] == "the corpus needs a pass"
    assert wait_until(lambda: curator.replaying is False), "replay_complete must arrive"

    delivered_id = curator.queue[0]["message_id"]
    curator.on_turn_boundary()
    curator.on_turn_boundary()
    assert cursors.read("curator@proj-a") == delivered_id
    # ...and the shim's turn frame names it handled, so it stops waiting (the cursor
    # alone deletes no row: test_cold_sends and test_queue pin that with raw frames)
    assert wait_until(lambda: _waiting(rig, "curator@proj-a") == 0)
    code, listing = rig.cli(["agents", "--seats"], slot=1)
    assert "curator@proj-a" in listing and "message(s)" not in listing


def test_replay_stops_at_the_cursor(rig):
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    rig.post_from_slot(6, ["curator@proj-a"], "first")
    wait_for_queue(curator, 1)
    curator.on_turn_boundary()
    curator.on_turn_boundary()
    first_cursor = cursors.read("curator@proj-a")
    curator.stop()
    wait_until(lambda: "curator@proj-a" not in rig.daemon.bindings)

    rig.post_from_slot(6, ["curator@proj-a"], "second")
    again = rig.shim(slot=4, name="curator", scope="proj-a")
    wait_for_queue(again, 1)
    wait_until(lambda: again.replaying is False)
    assert [p["text"] for p in again.queue] == ["second"]
    assert int(again.queue[0]["message_id"]) > int(first_cursor)


def test_bounded_replay_on_a_first_bind(rig, monkeypatch):
    """Bounded replay (absent cursor) bounds mail with no row."""
    write_config(None, "[replay]\nmessages = 3\n")   # the reader's own bound
    posted_while_down(rig, monkeypatch, lambda: [
        rig.post_from_slot(6, ["curator@proj-a"], f"msg-{i}") for i in range(6)])
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 3)
    wait_until(lambda: curator.replaying is False)
    assert [p["text"] for p in curator.queue] == ["msg-3", "msg-4", "msg-5"]
    assert curator.replay_notice is not None
    digest = curator.on_turn_boundary()
    assert "comms history" in digest, "the omission is stated, never silent"
    assert "bounded replay" in digest


def test_bounded_replay_drops_mail_older_than_T(rig, monkeypatch):
    """REPLAY_T bounds mail with no row; queued mail has no age limit."""
    write_config(None, "[replay]\nhours = 1\n")
    old, fresh = posted_while_down(rig, monkeypatch, lambda: [
        rig.post_from_slot(6, ["curator@proj-a"], "ancient"),
        rig.post_from_slot(6, ["curator@proj-a"], "recent")])
    # age the first message in the record (the rig knows where each backend keeps it)
    rig.age_message(old.id, "2001-01-01T00:00:00Z")
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_until(lambda: curator.replaying is False)
    assert [p["message_id"] for p in curator.queue] == [fresh.id]
    assert curator.replay_notice is not None


#: A config file that no longer loads: an unclosed array, the slip an edit makes.
BROKEN_CONFIG = "block = false\n[replay]\nmessages = [\n"


def test_a_config_file_broken_mid_run_keeps_the_last_good_block(rig, caplog):
    """At use the daemon keeps its last good settings and reports the error once per
    change of the file. A message posted while the file is broken is routed as the last
    good `block` says, and routing goes on once the file is fixed."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    write_config(None, "block = false\n")
    rig.post_from_slot(6, ["reviewer@proj-a"], "before")
    wait_for_queue(reviewer, 1)
    # The sender's credential, read while the file loads: a sender that started then (a
    # one-shot verb started now would refuse the broken file itself, and send nothing).
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.gateway import NO_MENTIONS
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(6, "slot-6", "global"))
    finally:
        db.close()

    def post(text):
        rig.gateway.execute_webhook(cred, machine_line.compose(["reviewer@proj-a"], text),
                                    username="slot-6", allowed_mentions=NO_MENTIONS)

    write_config(None, BROKEN_CONFIG)
    caplog.set_level("ERROR", logger="comms.daemon")
    post("while broken")
    post("still broken")
    wait_for_queue(reviewer, 3)
    kept = [r for r in caplog.records if "keeping the last good settings" in r.getMessage()]
    assert len(kept) == 1, "the error is reported once per change of the file"
    assert "config.toml" in kept[0].getMessage()

    write_config(None, "block = false\n")
    rig.post_from_slot(6, ["reviewer@proj-a"], "after the fix")
    wait_for_queue(reviewer, 4)
    assert [p["text"] for p in reviewer.queue] == [
        "before", "while broken", "still broken", "after the fix"]


def test_a_config_file_broken_mid_run_keeps_the_last_good_replay_bound(rig, monkeypatch):
    """A first bind's bounded replay reads `[replay]` at use: with the file broken it
    replays by the last good bound and completes, so the bind's queued mail is not
    stranded behind a raise after `ok`."""
    write_config(None, "[replay]\nmessages = 3\n")
    posted_while_down(rig, monkeypatch, lambda: [
        rig.post_from_slot(6, ["curator@proj-a"], f"msg-{i}") for i in range(6)])
    write_config(None, BROKEN_CONFIG)
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 3)
    wait_until(lambda: curator.replaying is False)
    assert [p["text"] for p in curator.queue] == ["msg-3", "msg-4", "msg-5"]
    assert curator.replay_notice is not None
    assert "last 3 messages" in curator.on_turn_boundary()


def test_mid_turn_death_hands_the_whole_digest_again(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    sent = [rig.post_from_slot(6, ["reviewer@proj-a"], "alpha"),
            rig.post_from_slot(6, ["reviewer@proj-a"], "beta")]
    wait_for_queue(reviewer, 2)
    digest = reviewer.on_turn_boundary()
    assert digest.count("[comms] from") == 2

    reviewer.die_mid_turn()                    # the Stop hook never fires
    wait_until(lambda: "reviewer@proj-a" not in rig.daemon.bindings)
    assert cursors.read("reviewer@proj-a") is None, "no advance without completion"
    # the unbind gives every handed, unhandled id past the cursor a row
    assert _queue(rig, "reviewer@proj-a") == {m.id for m in sent}

    again = rig.shim(slot=4, name="reviewer", scope="proj-a")
    wait_for_queue(again, 2)
    wait_until(lambda: again.replaying is False)
    assert [p["text"] for p in again.queue] == ["alpha", "beta"]
    assert all(p["held"] is True for p in again.queue), "released at the bind"


def test_multicast_is_one_post_one_thread_and_per_recipient_cursors(rig):
    a = rig.shim(slot=1, name="planner", scope="proj-a")
    b = rig.shim(slot=2, name="reviewer", scope="proj-a")
    c = rig.shim(slot=3, name="main", scope="proj-a")     # standing, mixed-tier list
    code, out = rig.cli(["send", "--posture", "none", "reviewer@proj-a,main@proj-a", "standup in five"],
                        slot=a.slot_id, name="planner", scope="proj-a")
    assert code == 0
    wait_for_queue(b, 1)
    wait_for_queue(c, 1)
    mid = b.queue[0]["message_id"]
    assert c.queue[0]["message_id"] == mid, "one post, one thread"
    assert len(rig.gateway.history(rig.names_channel)) == 1

    b.on_turn_boundary()
    b.on_turn_boundary()
    assert cursors.read("reviewer@proj-a") == mid
    assert cursors.read("main@proj-a") is None, "cursors are per recipient, never shared"
    c.on_turn_boundary()
    c.on_turn_boundary()
    assert cursors.read("main@proj-a") == mid


def test_standing_attribution_is_verified_even_replayed(rig):
    rig.shim(slot=1, name="main", scope="proj-a")
    code, _ = rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a", "a note"], slot=1, name="main", scope="proj-a")
    assert code == 0
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 1)
    assert curator.queue[0]["author"] == {"name": "main@proj-a", "tier": "standing",
                                          "attribution": "verified"}


def test_replayed_slot_attribution_is_labeled_not_guessed(rig):
    """A slot webhook proves nothing about who held it; replay says so honestly."""
    rig.post_from_slot(6, ["curator@proj-a"], "who am i", display="ghost@proj-a")
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 1)
    assert curator.queue[0]["author"]["attribution"] == "labeled"
    assert curator.queue[0]["author"]["name"] == "ghost@proj-a"


def test_a_live_slot_post_under_another_name_is_labeled_by_the_display(rig):
    """Verified needs TWO facts agreeing. Slot 1 is planner@proj-a's live
    binding, but an out-of-band spender of that slot's webhook posts as `ghost@proj-a`.
    The credential proves the slot; the display is the claim, and the record will show
    `ghost@proj-a`. The holder must NOT collect verified for words they did not say."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")

    msg = rig.post_from_slot(1, ["reviewer@proj-a"], "not the holder speaking",
                             display="ghost@proj-a")
    assert msg.author_id == rig.slot_webhook_id(1), "the LIVE binding's own webhook"
    # ...and the same slot wearing its own name is byte-identical to before: verified
    rig.post_from_slot(1, ["reviewer@proj-a"], "the holder speaking",
                       display="planner@proj-a")

    wait_for_queue(reviewer, 2)
    assert reviewer.queue[0]["author"] == {"name": "ghost@proj-a", "tier": "chosen",
                                           "attribution": "labeled"}
    assert reviewer.queue[1]["author"] == {"name": "planner@proj-a", "tier": "chosen",
                                           "attribution": "verified"}
    digest = reviewer.on_turn_boundary()
    assert "from ghost@proj-a (unverified)" in digest
    assert "from planner@proj-a · msg" in digest


def test_a_mismatched_live_slot_post_takes_the_display_derived_tier(rig):
    """The mismatch label is replay's label, tier and all -- `agent-N` is ephemeral."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    rig.post_from_slot(1, ["reviewer@proj-a"], "auto-named claim", display="agent-9")
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["author"] == {"name": "agent-9", "tier": "ephemeral",
                                           "attribution": "labeled"}


def test_a_standing_identity_stays_verified_whatever_name_it_wears(rig):
    """No slot is involved: verified comes from webhook -> directory row, so the
    match gate does not reach this path."""
    from moxi_comms import directory
    from moxi_comms.mock_gateway import NO_MENTIONS
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for

    rig.shim(slot=1, name="main", scope="proj-a")          # standing, live
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db, SenderIdentity.for_agent("main", "proj-a"))
    finally:
        db.close()
    body = machine_line.compose(["reviewer@proj-a"], "from my own webhook")
    rig.gateway.execute_webhook(cred, body, username="ghost@proj-a",
                                allowed_mentions=NO_MENTIONS)
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["author"] == {"name": "main@proj-a", "tier": "standing",
                                           "attribution": "verified"}


def test_operator_is_a_first_class_sender(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    rig.post_as_operator(["reviewer@proj-a"], "ship it")
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["author"] == {"name": "operator", "tier": "operator",
                                           "attribution": "verified"}


def test_a_hand_typed_re_token_correlates_and_a_malformed_one_is_prose(rig):
    """The operator types the machine line by hand, `re:` and all. A bogus referent is
    routing data (spoofable like every tag); a MALFORMED token costs the whole line."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    first = rig.post_as_operator(["reviewer@proj-a"], "question?")
    rig.post_as_operator(["reviewer@proj-a"], "answer.", reply_to=first.id)
    wait_for_queue(reviewer, 2)
    assert reviewer.queue[0]["reply_to"] is None
    assert reviewer.queue[1]["reply_to"] == first.id

    # a spoofed id points nowhere and is delivered anyway: tags are routing, not authority
    rig.post_as_operator(["reviewer@proj-a"], "spoofed", reply_to="999999999999999999")
    wait_for_queue(reviewer, 3)
    assert reviewer.queue[2]["reply_to"] == "999999999999999999"

    # a non-digit id fails line zero's grammar, so the message is prose and never routes
    rig.gateway.post_as_user(rig.prov.operator_user_id, rig.names_channel,
                             "::to reviewer@proj-a re:nonsense\nnever routed",
                             token=rig.operator_token)
    rig.post_as_operator(["reviewer@proj-a"], "the next real one")
    wait_for_queue(reviewer, 4)
    assert [p["text"] for p in reviewer.queue][-1] == "the next real one"
    assert len(reviewer.queue) == 4


def test_tag_shaped_prose_in_the_body_is_never_routed(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    from moxi_comms.mock_gateway import NO_MENTIONS
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db, SenderIdentity.for_slot(6, "x", "global"))
    finally:
        db.close()
    rig.gateway.execute_webhook(cred, "here is what I would send:\n::to reviewer@proj-a\nhi",
                                username="ghost", allowed_mentions=NO_MENTIONS)
    rig.post_from_slot(6, ["reviewer@proj-a"], "the real one")
    wait_for_queue(reviewer, 1)
    assert len(reviewer.queue) == 1
    assert reviewer.queue[0]["text"] == "the real one"


def test_ephemeral_receives_live_but_never_deferred_mail(rig):
    ghost = rig.shim(slot=1)                       # agent-1
    rig.post_from_slot(6, ["agent-1"], "live one")
    wait_for_queue(ghost, 1)
    assert ghost.queue[0]["text"] == "live one"
    ghost.stop()
    wait_until(lambda: "agent-1" not in rig.daemon.bindings)
    # a message to a retired agent-N is inert: it resolves to nobody, never a stranger
    rig.post_from_slot(6, ["agent-1"], "stale tag")
    successor = rig.shim(slot=2)
    assert successor.identity == "agent-2"
    assert wait_until(lambda: successor.replaying is False)
    assert successor.queue == []
    assert _queue(rig, "agent-1") == set()


def test_an_old_speaker_mark_is_delivered_and_shown_as_unmarked(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    from test_old_marks import raw_post
    raw_post(rig, 6, "slot-6", "::to reviewer@proj-a [speaker]\nfyi only")
    wait_for_queue(reviewer, 1)
    assert "speaker" not in reviewer.queue[0]
    digest = reviewer.on_turn_boundary()
    assert "[speaker]" not in digest and "fyi only" in digest


def test_the_record_carries_the_machine_line_at_position_zero(rig):
    """Line zero carries the send's cold-at: for a recipient that is not
    bound, `floor(t_ask)`, a time already reached."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    before = int(time.time())
    rig.cli(["send", "--posture", "none", "--new-name", "reviewer@proj-a", "body text"], slot=1, name="planner", scope="proj-a")
    after = int(time.time())
    msg = rig.gateway.history(rig.names_channel)[-1]
    zero = msg.content.splitlines()[0]
    assert re.fullmatch(r"::to reviewer@proj-a \[cold-at:\d+\] \[no reply\]", zero), zero
    ml = machine_line.parse(msg.content)
    assert ml.text == "body text"
    (cold_at,) = ml.cold_at
    assert min(before, int(rig.clock())) <= cold_at <= max(after, int(rig.clock()))




# --------------------------------------------------------------------------- the waiting view
# The rule: `waiting(q)` counts q's rows in the one queue. A row is written
# for mail to q while q is not bound, and for every id q's binding was handed and did not
# report handled when it unbinds; it is deleted only when a binding that was handed it
# names it `handled`, never by the cursor, the unbind or its age. Replay
# is bounded by REPLAY_N / REPLAY_T only when the cursor is absent, and only for mail with
# no row.

REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
REVIEWER_Q = "reviewer@proj-a"


def _reboot(rig):
    rig.daemon.stop()
    rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    return rig.daemon


def test_an_id_claimed_from_holding_keeps_its_row_until_handled(rig):
    """A message is handed and queued in the shim, the daemon restarts, the boot rescan
    runs while the shim is offline and gives it a row, and the shim re-registers with it
    in `holding`. The register claims it, so no release hands it twice; the row stays
    until the adapter names it handled."""
    shim = rig.shim(**REVIEWER, autoreconnect=True, backoff=itertools.repeat(0.5))
    msg = rig.post_from_slot(6, [REVIEWER_Q], "handed, turn not over")
    wait_for_queue(shim, 1)
    assert cursors.read(REVIEWER_Q) is None

    daemon = _reboot(rig)                 # start() runs the rescan before it returns
    assert _queue(rig, REVIEWER_Q) == {msg.id}, "unbound at rescan time: a row"
    assert wait_until(lambda: REVIEWER_Q in daemon.bindings and not shim.offline, timeout=5)
    assert wait_until(lambda: shim.replaying is False, timeout=5)
    time.sleep(0.2)
    assert msg.id in daemon.bindings[REVIEWER_Q].delivered, "claimed from `holding`"
    assert [p["message_id"] for p in shim.queue] == [msg.id], "not handed twice"

    assert _queue(rig, REVIEWER_Q) == {msg.id}, "in the adapter's hands, not yet handled"
    assert _waiting(rig, REVIEWER_Q) == 1
    shim.on_turn_boundary()
    shim.on_turn_boundary()
    send_turn(shim, handled=[msg.id])
    assert wait_until(lambda: _waiting(rig, REVIEWER_Q) == 0)
    code, out = rig.cli(["agents", "--seats"], slot=1)
    assert REVIEWER_Q in out and "message(s)" not in out, out


def test_a_reader_racing_the_hand_over_writes_no_row_and_replay_hands_nothing_twice(rig):
    """A reader that finds an id this binding was already handed writes no row for it
    (handled mail is never queued again), and replay that reads it again fails
    its claim -- handed is handed."""
    shim = rig.shim(**REVIEWER)
    wait_until(lambda: shim.replaying is False)
    msg = rig.post_from_slot(6, [REVIEWER_Q], "live, handed")
    wait_for_queue(shim, 1)
    daemon = rig.daemon
    binding = daemon.bindings[REVIEWER_Q]
    with daemon._lock:
        daemon._queue_add(REVIEWER_Q, msg.id, None)    # a reader that raced the hand-over
    assert _queue(rig, REVIEWER_Q) == set()
    daemon._replay(binding)                            # reads it again; the claim fails
    assert _queue(rig, REVIEWER_Q) == set()
    time.sleep(0.2)
    assert [p["message_id"] for p in shim.queue] == [msg.id], "not handed twice"


def test_a_queued_row_past_REPLAY_T_is_still_released_at_a_first_bind(rig):
    """Mail handed to a session that unbinds with no cursor gets a row.
    Queued mail has no age limit and bounded replay is for mail with no row: a first bind long past REPLAY_T still gets it, by release."""
    shim = rig.shim(**REVIEWER)
    old = rig.post_from_slot(6, [REVIEWER_Q], "handed, never handled")
    wait_for_queue(shim, 1)
    shim.stop()                                       # unbind, no cursor
    assert wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
    assert _queue(rig, REVIEWER_Q) == {old.id}, "a row at unbind"
    assert _waiting(rig, REVIEWER_Q) == 1

    _reboot(rig)
    assert _queue(rig, REVIEWER_Q) == {old.id}, "the boot load keeps it"

    rig.clock.advance(constants.REPLAY_T + 3600)
    assert _waiting(rig, REVIEWER_Q) == 1, "no age limit on waiting mail"
    code, out = rig.cli(["agents", "--seats"], slot=1)
    assert "1 message(s)" in out, out

    again = rig.shim(**REVIEWER)
    (payload,) = wait_for_queue(again, 1)
    assert wait_until(lambda: again.replaying is False, timeout=5)
    assert payload["message_id"] == old.id and payload["held"] is True
    assert again.replay_notice is None, "nothing was bounded"
    assert _queue(rig, REVIEWER_Q) == {old.id}, "the row waits for handled"


def test_a_first_bind_hands_every_queued_id_and_the_unbind_keeps_the_rows(rig, monkeypatch):
    """REPLAY_N bounds replay of mail with no row, never the queue: a first bind
    releases all six rows, and an unbind with no cursor deletes none of them."""
    write_config(None, "[replay]\nmessages = 3\n")   # the reader's own bound
    posted = [rig.post_from_slot(6, ["curator@proj-a"], f"msg-{i}") for i in range(6)]
    assert wait_until(lambda: len(_queue(rig, "curator@proj-a")) == 6)
    assert _waiting(rig, "curator@proj-a") == 6

    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    wait_for_queue(curator, 6)
    assert wait_until(lambda: curator.replaying is False)
    assert [p["message_id"] for p in curator.queue] == [m.id for m in posted]
    assert curator.replay_notice is None
    assert _waiting(rig, "curator@proj-a") == 6, "handed, not handled"

    curator.stop()                                    # unbind with no cursor
    assert wait_until(lambda: "curator@proj-a" not in rig.daemon.bindings)
    assert _queue(rig, "curator@proj-a") == {m.id for m in posted}


def test_a_present_cursor_replays_mail_older_than_REPLAY_T(rig, monkeypatch):
    """The cursor is present, two messages past it are owed with no row (posted while
    the daemon was down, beyond its rescan window), and the bind comes 47h later.
    Bounded replay is for an absent cursor only, so both are replayed, and there is no
    truncation notice."""
    shim = rig.shim(**REVIEWER)
    handled = rig.post_from_slot(6, [REVIEWER_Q], "handled")
    wait_for_queue(shim, 1)
    cursors.advance(REVIEWER_Q, handled.id)           # a real, present cursor
    send_turn(shim, handled=[handled.id])
    shim.stop()
    assert wait_until(lambda: REVIEWER_Q not in rig.daemon.bindings)
    owed1, owed2 = posted_while_down(rig, monkeypatch, lambda: [
        rig.post_from_slot(6, [REVIEWER_Q], "owed one (668)"),
        rig.post_from_slot(6, [REVIEWER_Q], "owed two (671)")])
    assert cursors.read(REVIEWER_Q) == handled.id
    assert _queue(rig, REVIEWER_Q) == set(), "no row: replay's mail"

    rig.clock.advance(47 * 3600)
    again = rig.shim(**REVIEWER)
    wait_for_queue(again, 2)
    assert wait_until(lambda: again.replaying is False, timeout=5)
    assert [p["message_id"] for p in again.queue] == [owed1.id, owed2.id]
    assert all(p["backlog"] is True for p in again.queue)
    assert again.replay_notice is None, "nothing was omitted, so nothing is said"
    assert _queue(rig, REVIEWER_Q) == set()


def test_a_present_cursor_replays_more_than_REPLAY_N(rig, monkeypatch):
    """REPLAY_N, too, bounds an absent cursor only: a present one replays all it owes."""
    write_config(None, "[replay]\nmessages = 3\n")   # the reader's own bound
    curator = rig.shim(slot=3, name="curator", scope="proj-a")
    first = rig.post_from_slot(6, ["curator@proj-a"], "first")
    wait_for_queue(curator, 1)
    cursors.advance("curator@proj-a", first.id)
    send_turn(curator, handled=[first.id])
    curator.stop()
    assert wait_until(lambda: "curator@proj-a" not in rig.daemon.bindings)

    owed = posted_while_down(rig, monkeypatch, lambda: [
        rig.post_from_slot(6, ["curator@proj-a"], f"owed-{i}") for i in range(6)])
    assert _queue(rig, "curator@proj-a") == set(), "no row: replay's mail"
    again = rig.shim(slot=4, name="curator", scope="proj-a")
    wait_for_queue(again, 6)
    assert wait_until(lambda: again.replaying is False)
    assert [p["message_id"] for p in again.queue] == [m.id for m in owed]
    assert all(p["backlog"] is True for p in again.queue)
    assert again.replay_notice is None
