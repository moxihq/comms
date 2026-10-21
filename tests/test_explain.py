"""`moxi comms explain`, and the named reasons under it.

Two halves. The first gives every quiet path in `cursors.py` a name and a
sentence, so a deliberate default stops reading as an accident. The second is the verb
that prints them next to the binding, the adapter kind, the cursor and the last
delivery -- every live fact behind a delivery, in one command.

Nothing here is allowed to write anything down. The whole point of the command is that
it reads live facts, so every test that asserts on an answer first arranges the fact
it is asking about.
"""

from __future__ import annotations

import json

import pytest
from conftest import wait_for_queue, wait_until

from moxi_comms import cursors
from moxi_comms.client import DaemonClient
from moxi_comms.daemon import (CASE_HANDLED, CASE_IGNORED, CASE_NOT_ADDRESSABLE,
                               CASE_REASONS, CASE_TURN_INCOMPLETE, CASE_WAITING)
from moxi_comms.paths import cursors_dir, daemon_socket

IDENTITY = "reviewer@proj-a"


@pytest.fixture(autouse=True)
def on_the_mock(monkeypatch):
    """This suite's rig is the mock, and the bare cursors directory is the mock's, so the
    process names the mock rather than fall through to the profile's default."""
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "mock")


def ask(identity=IDENTITY, message_id=None) -> dict:
    """The daemon's answer, taken the way a caller outside the daemon has to take it."""
    client = DaemonClient(daemon_socket()).connect()
    try:
        return client.query("explain", identity=identity, message_id=message_id)
    finally:
        client.close()


def deliver_one(rig, shim, text="for the reviewer"):
    rig.post_from_slot(6, [IDENTITY], text)
    wait_for_queue(shim, 1)
    return shim.queue[0]["message_id"]


def cursor_file(qualified=IDENTITY):
    return cursors_dir() / f"{qualified}.json"


def write_cursor(value, qualified=IDENTITY):
    p = cursor_file(qualified)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cursor": value, "updated_at": "2026-09-23T00:00:00+00:00"}))
    return p


# ------------------------------------------------------- half one: the named reasons

def test_every_named_reason_has_a_sentence():
    """A name with no sentence is a second thing to look up, which is the problem the
    card is about."""
    names = {v for k, v in vars(cursors).items()
             if k.isupper() and isinstance(v, str) and k != "REASONS"}
    assert names == set(cursors.REASONS), "every name is in the table, and only names are"
    for name, sentence in cursors.REASONS.items():
        assert sentence.endswith(".") and len(sentence) > 60, name
        assert name not in sentence, "the sentence explains the name, it does not repeat it"


def test_the_quiet_paths_each_name_themselves_in_the_log(rig, caplog):
    """The three that reported differently, and the one the backend split added. Before
    this you could not grep for any of them, because none of them said a word you could
    search for."""
    import logging
    caplog.set_level(logging.DEBUG, logger="comms.cursors")

    cursors.advance(IDENTITY, "not-an-id")
    cursors.advance(IDENTITY, "50")
    cursors.advance(IDENTITY, "40")                 # already past: silent until now
    write_cursor("not-a-message-id")
    cursors.advance(IDENTITY, "60")                 # the guard skip

    logged = "\n".join(r.getMessage() for r in caplog.records)
    for name in (cursors.TARGET_NOT_A_MESSAGE_ID, cursors.ALREADY_PAST,
                 cursors.CURSOR_NOT_A_MESSAGE_ID):
        assert f"[{name}]" in logged, name


def test_an_unresolvable_backend_names_itself(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="comms.cursors")
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "no-such-backend")

    info = cursors.explain(IDENTITY)
    assert info["backend"] is None
    assert info["backend_reason"] == cursors.BACKEND_UNRESOLVED
    assert f"[{cursors.BACKEND_UNRESOLVED}]" in "\n".join(r.getMessage()
                                                          for r in caplog.records)


def test_a_corrupt_cursor_file_stops_being_silent(caplog):
    """The worst of the seven: a corrupt cursor and a brand-new agent both answered
    None, both replay everything, and nothing anywhere said which one you had."""
    import logging
    caplog.set_level(logging.WARNING, logger="comms.cursors")

    assert cursors.explain(IDENTITY)["reason"] == cursors.NO_CURSOR_YET
    assert cursors.read(IDENTITY) is None
    assert caplog.records == [], "the absent file is where every identity starts"

    p = cursor_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert cursors.read(IDENTITY) is None, "still degrades toward replay, never a raise"
    assert cursors.explain(IDENTITY)["reason"] == cursors.CURSOR_UNREADABLE
    assert f"[{cursors.CURSOR_UNREADABLE}]" in "\n".join(r.getMessage()
                                                         for r in caplog.records)


def test_a_cursor_of_the_wrong_shape_is_unreadable_not_a_position():
    """`["cursor"]` and `{"cursor": null}` are valid JSON and are not cursors. `read`
    has always answered None to both; now the reason says which."""
    for bad in ('["cursor"]', '{"cursor": null}', '{"cursor": {"id": 1}}', '42'):
        p = cursor_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(bad)
        assert cursors.read(IDENTITY) is None
        assert cursors.explain(IDENTITY)["reason"] == cursors.CURSOR_UNREADABLE, bad


def test_explain_compares_against_the_message_it_is_asked_about():
    write_cursor("100")
    assert cursors.explain(IDENTITY, "100")["reason"] == cursors.ALREADY_PAST
    assert cursors.explain(IDENTITY, "99")["reason"] == cursors.ALREADY_PAST
    assert cursors.explain(IDENTITY, "101")["reason"] == cursors.BEHIND
    assert cursors.explain(IDENTITY)["reason"] is None, "no message, nothing to compare"
    assert cursors.explain(IDENTITY)["cursor"] == "100"


def test_a_cursor_that_is_not_a_message_id_is_its_own_reason():
    write_cursor("not-a-message-id")
    info = cursors.explain(IDENTITY, "100")
    assert info["reason"] == cursors.CURSOR_NOT_A_MESSAGE_ID
    assert info["cursor"] == cursors.read(IDENTITY), (
        "the command and the daemon's classify must never see two different values")


def test_the_write_side_reasons_are_never_answers():
    """They happen inside the adapter process at the moment of the write and leave
    nothing behind. Naming them and saying where to look is the honest thing; inventing
    a ledger to hold them is what the contract forbids."""
    write_cursor("100")
    for message_id in (None, "50", "500"):
        assert cursors.explain(IDENTITY, message_id)["reason"] not in \
            cursors.WRITE_SIDE_REASONS


def test_explain_reads_and_writes_the_same_path_the_rest_of_the_module_does(monkeypatch):
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "local")
    cursors.advance(IDENTITY, "100")
    info = cursors.explain(IDENTITY, "100")
    assert info["backend"] == "local"
    assert info["path"] == info["read_path"] == str(cursors_dir() / "local"
                                                    / f"{IDENTITY}.json")
    assert info["updated_at"]


# ------------------------------------------------------------- half two: the command

def test_the_four_cases_each_have_a_sentence():
    assert set(CASE_REASONS) == {CASE_HANDLED, CASE_NOT_ADDRESSABLE, CASE_IGNORED,
                                 CASE_TURN_INCOMPLETE}


def test_explain_classifies_with_classify_over_handed_at(rig):
    """Not a second opinion ([R25]). `_explain_query` calls `classify`, the one
    classifier, and its delivery facts come from `handed_at`."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    message_id = deliver_one(rig, reviewer)
    assert (IDENTITY, message_id) in rig.daemon.handed_at

    for step in range(3):
        if step == 1:
            # the injected frame reaches the daemon after the shim's boundary returns;
            # ask once it has, or the ask and `classify` can straddle it
            assert wait_until(lambda: rig.daemon.injected_through.get(IDENTITY))
        info = ask(message_id=message_id)
        assert info["case"] == rig.daemon.classify(IDENTITY, message_id)
        assert info["delivery"]["delivered_for"] is not None
        reviewer.on_turn_boundary()


def test_a_hand_over_the_trim_dropped_reads_age_unknown(rig):
    """[R25]: past `THREAD_IDLE` the daily trim drops the hand-over time, and the page
    says "age unknown" with no other clause."""
    from moxi_comms import constants

    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    message_id = deliver_one(rig, reviewer)
    rig.clock.advance(constants.THREAD_IDLE + 1)
    rig.daemon.trim_record()
    assert (IDENTITY, message_id) not in rig.daemon.handed_at

    info = ask(message_id=message_id)
    assert info["delivery"]["delivered_for"] is None
    assert "alarm_window" not in info
    code, out = rig.cli(["explain", IDENTITY, message_id], slot=1, name="planner",
                        scope="proj-a")
    assert code == 0, out
    (line,) = [ln for ln in out.splitlines() if ln.lstrip().startswith("delivered")]
    assert line.split("delivered", 1)[1].strip() == "age unknown", line


def test_delivered_but_the_turn_never_completed(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    message_id = deliver_one(rig, reviewer)
    reviewer.on_turn_boundary()                 # injects; the turn does not complete
    assert wait_until(lambda: rig.daemon.injected_through.get(IDENTITY))

    info = ask()
    assert info["case"] == CASE_TURN_INCOMPLETE
    assert info["bound"] is True
    assert info["binding"]["adapter"] == "claude-shim"
    assert info["binding"]["tier"] == "chosen"
    assert info["delivery"]["message_id"] == message_id
    assert info["injected_through"] == message_id
    assert info["cursor"]["reason"] == cursors.NO_CURSOR_YET
    assert "for the reviewer" in info["digest"], "the input, not only the verdict"


def test_handled_says_so_and_names_the_cursor_reason(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    message_id = deliver_one(rig, reviewer)
    reviewer.on_turn_boundary()
    reviewer.on_turn_boundary()                 # the turn completes; the cursor moves

    info = ask()
    assert info["case"] == CASE_HANDLED
    assert info["cursor"]["cursor"] == message_id
    assert info["cursor"]["reason"] == cursors.ALREADY_PAST


def test_delivered_and_ignored(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    deliver_one(rig, reviewer)
    info = ask()
    assert info["case"] == CASE_IGNORED
    assert info["injected_through"] is None
    assert info["digest"] is None


def test_an_unbound_name_is_still_explainable(rig):
    """The case a person asks about most: the session is gone. The binding facts went
    with it and the hand-over time did not, so there is still an answer. The unbind gave
    the handed, unhandled id a row (design §3.3.2 row 4), and a queued id reads `waiting`
    before anything else (§3.11)."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    message_id = deliver_one(rig, reviewer)
    reviewer.stop()
    assert wait_until(lambda: IDENTITY not in rig.daemon.bindings)

    info = ask()
    assert info["case"] == CASE_WAITING
    assert info["cursor"]["reason"] == cursors.WAITING
    assert info["bound"] is False and info["binding"] is None
    assert info["delivery"]["message_id"] == message_id
    assert info["digest"] is None, "the digest dies with the binding"
    assert info["waiting"] == [message_id], "and its mail is waiting again"


def test_a_gone_name_with_nothing_queued_is_not_addressable(rig):
    """An `agent-N` gets no row at its unbind (design §3.3.2), so the mail it was handed
    and never handled is the not-addressable case: unbound, nothing waiting, the cursor
    below it."""
    ghost = rig.shim(slot=3)
    assert ghost.identity == "agent-1"
    rig.post_from_slot(6, ["agent-1"], "for the ghost")
    wait_for_queue(ghost, 1)
    message_id = ghost.queue[0]["message_id"]
    ghost.stop()
    assert wait_until(lambda: "agent-1" not in rig.daemon.bindings)

    info = ask("agent-1")
    assert info["case"] == CASE_NOT_ADDRESSABLE
    assert info["bound"] is False and info["binding"] is None
    assert info["delivery"]["message_id"] == message_id
    assert info["waiting"] == []


def test_a_name_nothing_has_ever_happened_to_answers_rather_than_refusing(rig):
    info = ask("nobody@proj-a")
    assert info["bound"] is False
    assert info["delivery"] is None
    assert info["case"] is None
    assert info["cursor"]["reason"] == cursors.NO_CURSOR_YET


def test_the_newest_delivery_is_the_default_and_any_id_can_be_asked_for(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    first = deliver_one(rig, reviewer, "one")
    rig.post_from_slot(6, [IDENTITY], "two")
    wait_for_queue(reviewer, 2)
    second = reviewer.queue[1]["message_id"]
    assert int(second) > int(first)

    assert ask()["delivery"]["message_id"] == second
    assert ask()["delivery"]["asked_for"] is False
    asked = ask(message_id=first)
    assert asked["delivery"]["message_id"] == first
    assert asked["delivery"]["asked_for"] is True


def test_an_unclassifiable_cursor_answers_instead_of_raising(rig):
    """A cursor that is not a message id reaches `classify`'s int() and raises. The
    command says so and falls back on the cursor's own named reason."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    deliver_one(rig, reviewer)
    write_cursor("not-a-message-id")

    info = ask()
    assert info["case"] is None
    assert info["case_error"] and "ValueError" in info["case_error"]
    assert info["cursor"]["reason"] == cursors.CURSOR_NOT_A_MESSAGE_ID


def test_asking_records_nothing(rig):
    """No receipts ledger, ever. Asking twice must leave the
    daemon exactly as the first ask found it."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    deliver_one(rig, reviewer)
    before = (dict(rig.daemon.handed_at), dict(rig.daemon.injected_through),
              dict(rig.daemon.activity), {k: dict(v) for k, v in rig.daemon.queue.items()})
    first = ask()
    assert ask() == first
    assert (dict(rig.daemon.handed_at), dict(rig.daemon.injected_through),
            dict(rig.daemon.activity),
            {k: dict(v) for k, v in rig.daemon.queue.items()}) == before


# --------------------------------------------------------------------- the rendering

def test_the_verb_prints_the_facts_the_card_asked_for(rig):
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    message_id = deliver_one(rig, reviewer, "the diff needs a second pair of eyes")
    reviewer.on_turn_boundary()
    assert wait_until(lambda: rig.daemon.injected_through.get(IDENTITY))

    code, out = rig.cli(["explain", IDENTITY], slot=1, name="planner", scope="proj-a")
    assert code == 0
    assert IDENTITY in out
    assert CASE_TURN_INCOMPLETE in out                         # the verdict
    assert "claude-shim" in out                                # the adapter kind
    assert message_id in out                                   # the last delivery
    assert "chosen" in out                                     # the binding
    assert cursors.NO_CURSOR_YET in out                        # the named reason
    assert "the cursor moves on the adapter's turn boundary" in out.lower()
    assert "the diff needs a second pair of eyes" in out       # the input


def test_the_verb_names_the_two_reasons_it_can_never_answer(rig):
    """And only where they are a live hypothesis. A cursor that moved was not declined,
    so in the handled case this block is noise and stays off the page."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    deliver_one(rig, reviewer)

    code, out = rig.cli(["explain", IDENTITY], slot=1, name="planner", scope="proj-a")
    assert code == 0
    for name in cursors.WRITE_SIDE_REASONS:
        assert name in out, "named, with where to look, rather than quietly missing"

    reviewer.on_turn_boundary()
    reviewer.on_turn_boundary()                 # the cursor advances past it
    code, out = rig.cli(["explain", IDENTITY], slot=1, name="planner", scope="proj-a")
    assert code == 0 and CASE_HANDLED in out
    for name in cursors.WRITE_SIDE_REASONS:
        assert name not in out


def test_a_long_digest_is_cut_and_says_how_much_it_cut(rig):
    """Never truncate quietly: the cut says how much it cut."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    for n in range(20):
        rig.post_from_slot(6, [IDENTITY], f"message number {n}")
    wait_for_queue(reviewer, 20)
    reviewer.on_turn_boundary()
    assert wait_until(lambda: rig.daemon.injected_digest.get(IDENTITY))

    code, out = rig.cli(["explain", IDENTITY], slot=1, name="planner", scope="proj-a")
    assert code == 0
    assert "more characters" in out and "moxi comms history" in out


def test_explain_with_the_daemon_down_still_prints_the_cursor(rig):
    """The cursor is a file and the one durable fact. A blank where a binding
    would be is not an answer; saying which half is unknowable is."""
    cursors.advance(IDENTITY, "1500000000000009252")
    rig.daemon.stop()

    code, out = rig.cli(["explain", IDENTITY], slot=1, name="planner", scope="proj-a")
    assert code == 4                                            # EXIT_OFFLINE
    assert "comms offline" in out
    assert "1500000000000009252" in out


def test_explain_reaches_no_credential_and_writes_nothing(rig):
    """A READING verb. The fence is the provisioner credential, and this verb
    is on the far side of it: the daemon and one cursor file, nothing else."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    deliver_one(rig, reviewer)
    before = rig.gateway.history(rig.names_channel)

    code, _ = rig.cli(["explain", IDENTITY], slot=1, name="planner", scope="proj-a")
    assert code == 0
    assert rig.gateway.history(rig.names_channel) == before
    assert cursors.read(IDENTITY) is None, "explaining a cursor never moves it"


def test_explaining_a_retired_ephemeral_says_so(rig):
    """The daemon already refuses that name for the send verb, and the refusal is the
    whole answer to "why did my message not land". Printed as it came."""
    code, out = rig.cli(["explain", "agent-99"], slot=1, name="planner", scope="proj-a")
    assert code == 2                                            # EXIT_REFUSED
    assert "never reused" in out


@pytest.mark.parametrize("seconds,expected", [(0, "0s ago"), (45, "45s ago"),
                                              (600, "10m ago"), (7200, "2.0h ago"),
                                              (None, "never")])
def test_ages_are_readable(seconds, expected):
    from moxi_comms.cli import _ago
    assert _ago(seconds) == expected
