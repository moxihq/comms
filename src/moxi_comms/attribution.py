"""Attribution honesty.

Verification is always author `webhook_id` -> directory, never display names.
What the record alone can prove:

  * a webhook owned by an agent row      -> that standing identity, **verified**
  * the operator's user id                -> the operator, **verified**
  * a SLOT-owned webhook                  -> the slot is not an address; who held it is
                                             not in the record -- but it IS in the
                                             custody journal, and when the journal names
                                             a single holder over an interval containing
                                             the message's timestamp AND that holder's
                                             qualified name matches the display, that is
                                             two independent facts agreeing -> **verified**
                                             Otherwise the display name is all
                                             there is -> **labeled**
  * anything else                         -> `unknown`, never guessed

The daemon can do better still for LIVE delivery: it knows who holds the slot right now
without reading anything. The journal is that same match-gate run against the past --
`bindings.py` is where the interval and its edges (the boot fence, the no-tolerance
containment) are spelled out -- so live and replay now answer the same question the same
way, and only the source of the custody fact differs.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import bindings, directory
from .scoping import GLOBAL, is_auto_name, qualify, split_tag

TIER_EPHEMERAL, TIER_CHOSEN, TIER_STANDING = "ephemeral", "chosen", "standing"
TIER_OPERATOR, TIER_UNKNOWN = "operator", "unknown"

VERIFIED, LABELED = "verified", "labeled"

#: The RENDERED word for anything short of `verified`. The stored value and the payload/API
#: field stay `labeled` -- the wire is unchanged -- but nothing an agent or the operator
#: READS says `labeled` any more: `labeled` names how the fact was obtained, `unverified`
#: names what the reader must do about it, and only the second is the reader's business.
UNVERIFIED = "unverified"


def rendered(attribution: str) -> str:
    """The word a READER sees: `verified`, or `unverified` for everything else.

    One function so the two rendering surfaces (the adapters' frame line, the CLI's
    `history` / `reply`) cannot drift, and so the wire value keeps exactly one
    translation point instead of one per f-string.
    """
    return VERIFIED if attribution == VERIFIED else UNVERIFIED


@dataclass
class Author:
    name: str
    scope: str
    qualified: str
    tier: str
    attribution: str          # verified | labeled


def unknown() -> Author:
    return Author("unknown", GLOBAL, "unknown", TIER_UNKNOWN, LABELED)


def from_display(display: str) -> Author:
    name, scope = split_tag(display or "")
    scope = scope or GLOBAL
    if not name:
        return unknown()
    tier = TIER_EPHEMERAL if is_auto_name(name) else TIER_CHOSEN
    return Author(name, scope, qualify(name, scope), tier, LABELED)


def from_record(db, msg) -> Author:
    """Everything the record + directory can prove about who authored `msg`."""
    if msg.author_kind == "user":
        row = directory.operator(db, msg.author_id)
        if row:
            return Author(row["name"], GLOBAL, row["name"], TIER_OPERATOR, VERIFIED)
        return unknown()
    row = directory.webhook(db, msg.author_id)
    if row is None:
        return unknown()
    if row["agent_name"] is not None:
        # STANDING: webhook -> directory row, no slot and no journal in it. Untouched.
        name, scope = row["agent_name"], row["agent_scope"]
        return Author(name, scope, qualify(name, scope), TIER_STANDING, VERIFIED)
    return from_slot(row["slot_id"], msg)


def from_slot(slot_id, msg) -> Author:
    """A slot-owned webhook: the retroactive match-gate, then the display-derived label.

    The credential proves the SEAT. The journal says who held that seat when the message
    was posted. The display is the sender's CLAIM about who spoke, and it is what the
    record will show forever. Two of those three agreeing is what `verified` means; the
    journal is what lets the custody fact survive the socket.

    Every way of not knowing lands in the same place -- no journal, no covering interval,
    two overlapping intervals, an unparseable display, a display that names someone other
    than the holder -- and that place is the display-derived label. Ambiguity degrades
    toward unverified; it never invents a verified.
    """
    claimed = from_display(msg.author_display)
    if claimed.tier == TIER_UNKNOWN:
        return claimed                      # nothing to match against
    held = bindings.holder_at(slot_id, getattr(msg, "timestamp", None))
    if held is not None and held["qualified"] == claimed.qualified:
        return Author(held["name"], held["scope"], held["qualified"], held["tier"],
                      VERIFIED)
    return claimed
