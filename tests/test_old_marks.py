"""[R30]: records written before comms/refine still parse (design §6.0).

comms/refine removed `--operator-words`, `--via phone` and `--speaker` (§6.1-§6.3), and
with them the marks `[operator-words]`, `[via-phone]` and `[speaker]`. Records written
before then carry them. [R29] keeps the three slots in line zero's grammar, matched and
dropped, so such a line is still a machine line and not prose. Here: old line-zero
strings carrying each token, alone and together, parse with every other field intact;
and `comms history`, `comms reply <old id>` and a boot rescan of a pending old message
behave as they do for the same message without the mark.

Every rig test runs on the mock and on the local backend (`BOTH_BACKENDS`), never
Discord. The pure tests run once.
"""

from __future__ import annotations

import dataclasses
import itertools
import re

import pytest
from conftest import queued, wait_for_queue, wait_until

from moxi_comms import directory, machine_line
from moxi_comms.adapters.claude_shim import frame_line
from moxi_comms.daemon import Daemon
from moxi_comms.mock_gateway import NO_MENTIONS
from moxi_comms.paths import directory_db
from moxi_comms.sender import SenderIdentity, credential_for

PLANNER = dict(slot=1, name="planner", scope="proj-a")
REVIEWER = dict(slot=2, name="reviewer", scope="proj-a")
P, R = "planner@proj-a", "reviewer@proj-a"

TOKENS = ("operator-words", "via-phone", "speaker")
#: Every non-empty set of the removed tokens: each alone, each pair, all three.
TOKEN_SETS = [set(c) for n in (1, 2, 3) for c in itertools.combinations(TOKENS, n)]
TOKEN_IDS = ["+".join(sorted(c)) for c in TOKEN_SETS]
REMOVED_KEYS = ("operator_words", "via_phone", "speaker")


def old_line(tags, tokens, *, reply_to=None, audience=None, next_turn=False, cap=None,
             thread=None, posture=None):
    """Line zero in the pre-round grammar, each token in its old slot:

        ::to <tags> [re:<id>] [audience:<tag>] [operator-words] [via-phone] [next-turn]
             [cap:<n> | no-cap] [1 reply left | last reply] [speaker] [<posture>]
    """
    bits = [f"::to {tags}"]
    bits += [f"re:{reply_to}"] if reply_to else []
    bits += [f"[audience:{audience}]"] if audience else []
    bits += ["[operator-words]"] if "operator-words" in tokens else []
    bits += ["[via-phone]"] if "via-phone" in tokens else []
    bits += ["[next-turn]"] if next_turn else []
    bits += [f"[{cap}]"] if cap else []
    bits += [f"[{thread}]"] if thread else []
    bits += ["[speaker]"] if "speaker" in tokens else []
    bits += [f"[{posture}]"] if posture else []
    return " ".join(bits)


def fields(ml):
    """Every parsed field but the raw line."""
    return {f.name: getattr(ml, f.name) for f in dataclasses.fields(ml) if f.name != "line"}


# ------------------------------------------------------------------ the parse

SHAPES = {
    "bare": dict(tags="a"),
    "full": dict(tags="a, b", reply_to="12", audience="operator", next_turn=True, cap="cap:3",
                 thread="last reply", posture="reply-once"),
    "no-cap, open": dict(tags="a", cap="no-cap", thread="1 reply left", posture="open"),
    "no reply": dict(tags="a@proj-a", audience="op", posture="no reply"),
}


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
@pytest.mark.parametrize("tokens", TOKEN_SETS, ids=TOKEN_IDS)
def test_an_old_line_parses_with_every_other_field_intact(shape, tokens):
    kw = SHAPES[shape]
    plain = machine_line.parse(old_line(tokens=(), **kw) + "\nthe body")
    marked = machine_line.parse(old_line(tokens=tokens, **kw) + "\nthe body")
    assert plain is not None and marked is not None
    assert fields(marked) == fields(plain)
    assert marked.text == "the body"


@pytest.mark.parametrize("tokens", TOKEN_SETS, ids=TOKEN_IDS)
def test_the_old_tokens_are_matched_case_insensitively(tokens):
    line = old_line("a", tokens, reply_to="9", posture="reply-once").upper().replace("::TO A",
                                                                             "::to a")
    ml = machine_line.parse(line)
    assert ml is not None and (ml.reply_to, ml.posture) == ("9", "once")


def test_an_old_speaker_line_with_no_posture_token_parses_as_open():
    """It was open: `[speaker]` wrote no posture token."""
    ml = machine_line.parse("::to a [speaker]\nfyi")
    assert (ml.posture, ml.open_mark) == ("open", False)


def test_the_parsed_line_has_no_field_for_a_removed_mark():
    ml = machine_line.parse(old_line("a", set(TOKENS)))
    for key in REMOVED_KEYS:
        assert not hasattr(ml, key), key


@pytest.mark.parametrize("line", [
    "::to a [speaker] [via-phone]",                  # out of order
    "::to a [via-phone] [operator-words]",           # out of order
    "::to a [speaker] [speaker]",                    # doubled
    "::to a [next-turn] [via-phone]",                # after next-turn
])
def test_an_old_token_out_of_its_slot_is_still_prose(line):
    assert machine_line.parse(line) is None


def test_nothing_new_writes_a_removed_mark():
    body = machine_line.compose(["a"], "hi", reply_to="9", audience="op", posture="none")
    assert not any(f"[{t}]" in body for t in TOKENS)


# ------------------------------------------------------------- on the rig

def raw_post(rig, slot, display, body):
    """Post a body exactly as given through a slot's webhook, wearing `display`: a
    record as a pre-round comms wrote it."""
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, _ = credential_for(rig.gateway, db,
                                 SenderIdentity.for_slot(slot, display, "global"))
    finally:
        db.close()
    return rig.gateway.execute_webhook(cred, body, username=display,
                                       allowed_mentions=NO_MENTIONS)


def history_row(out, message_id):
    (row,) = [ln for ln in out.splitlines() if ln.startswith(f"{message_id} ")]
    return row


@pytest.mark.parametrize("tokens", TOKEN_SETS, ids=TOKEN_IDS)
def test_history_shows_an_old_message_as_it_shows_the_same_one_unmarked(rig, tokens):
    kw = dict(audience="operator", posture="reply-once")
    plain = raw_post(rig, 1, P, old_line(R, (), **kw) + "\nplain ask")
    marked = raw_post(rig, 1, P, old_line(R, tokens, **kw) + "\nmarked ask")
    code, out = rig.cli(["history", R])
    assert code == 0, out
    for t in TOKENS:
        assert f"[{t}]" not in out, out
    # A row past its id and time: who, the recipients, re:, the audience and the posture.
    plain_row = re.sub(r"^\d+\s+\S+", "", history_row(out, plain.id))
    marked_row = re.sub(r"^\d+\s+\S+", "", history_row(out, marked.id))
    assert marked_row == plain_row
    assert "-> reviewer@proj-a" in marked_row and "[reply-once]" in marked_row
    assert "audience: operator" in marked_row


@pytest.mark.parametrize("posture", [None, "reply-once", "no reply", "open"])
@pytest.mark.parametrize("tokens", TOKEN_SETS, ids=TOKEN_IDS)
def test_reply_to_an_old_message_posts_as_to_the_same_one_unmarked(rig, tokens, posture):
    """`comms reply <old id>`: the reply threads to it (`re:`), inherits its audience,
    and takes the posture a reply to the unmarked message takes; or both refuse alike."""
    replies = {}
    for name, toks in (("plain", ()), ("marked", tokens)):
        ask = raw_post(rig, 6, P, old_line(R, toks, audience="operator", posture=posture)
                       + f"\n{name} ask")
        code, out = rig.cli(["reply", ask.id, "the answer"], **REVIEWER)
        replies[name] = (code, re.sub(r"\d{10,}", "<id>", out))
        if code == 0:
            reply = machine_line.parse(rig.gateway.history(rig.names_channel)[-1].content)
            assert reply.reply_to == ask.id
            replies[name] += (reply.posture, reply.audience, reply.recipients)
    assert replies["marked"] == replies["plain"]
    if posture != "no reply":
        assert replies["marked"][0] == 0, replies


@pytest.mark.parametrize("tokens", TOKEN_SETS, ids=TOKEN_IDS)
def test_a_boot_rescan_routes_a_pending_old_message(rig, tokens):
    """Daemon down, the old message posted, daemon up: the rescan queues it for reviewer,
    and reviewer's bind hands it over with every other field, and no removed key."""
    rig.daemon.stop()
    old = raw_post(rig, 6, P, old_line(R, tokens, audience="operator", posture="reply-once")
                   + "\nposted while the daemon was down")
    rig.daemon = Daemon(rig.gateway, clock=rig.clock, start_trim_thread=False).start()
    assert old.id in queued(rig.daemon, R), "the rescan queues it"
    shim = rig.shim(**REVIEWER)
    (payload,) = wait_for_queue(shim, 1)
    assert payload["message_id"] == old.id
    assert (payload["posture"], payload["audience"], payload["text"]) == \
        ("once", "operator", "posted while the daemon was down")
    for key in REMOVED_KEYS:
        assert key not in payload, key
    line = frame_line(payload)
    for t in TOKENS:
        assert f"[{t}]" not in line
    assert line.endswith("[reply-once]")
    shim.on_turn_boundary()                         # inject
    shim.on_turn_boundary()                         # complete: the row goes (§3.3.3)
    assert wait_until(lambda: not queued(rig.daemon, R))


def test_a_live_old_message_is_routed_and_delivered(rig):
    """The same, with the daemon up: the routed old line reaches its recipient."""
    shim = rig.shim(**REVIEWER)
    old = raw_post(rig, 6, P, old_line(R, set(TOKENS), reply_to="5", posture="no reply")
                   + "\nfyi")
    (payload,) = wait_for_queue(shim, 1)
    assert (payload["message_id"], payload["posture"], payload["reply_to"]) == \
        (old.id, "none", "5")
    for key in REMOVED_KEYS:
        assert key not in payload, key
