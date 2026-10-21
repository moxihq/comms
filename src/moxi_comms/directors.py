"""Recipient-declared director authority.

Authority is granted by the RECIPIENT at launch, never claimed by the sender. The whole
declaration surface is `moxi-comms-claude --director <name>` (repeatable) and the codex
roster's `directors` list; a sender can no more assert authority over its reader than it
can assert a routing tag it is entitled to, and for the same reason: tags are
spoofable, names are cooperative labels, and nothing builds authorization on tag
contents.

It is INVISIBLE ON THE WIRE. No record, payload, daemon or protocol change: two things
exist and nothing else does.

  * the DECLARATION -- qualified ONCE, at launch, and stamped into the session
    environment as `MOXI_COMMS_DIRECTORS` (comma-separated, qualified) beside the identity
    stamp `MOXI_COMMS_NAME` / `MOXI_COMMS_SCOPE` / `MOXI_COMMS_SLOT`;
  * the RENDERING -- `standing_of()`, which both adapters' frame line asks about every
    delivered payload before it writes the line.

**Qualification happens exactly once.** `moxi-comms-shim --director` and `$MOXI_COMMS_DIRECTORS`
carry already-qualified names and are never re-qualified: a global identity renders bare
(`steward`, not `steward@global`), so a second pass cannot tell it from a bare name and
would quietly turn it into something else. One spelling per identity, decided at the
launcher and the roster: a bare director is the global identity, `name@scope` is that
scope's.

Directive standing requires VERIFIED attribution (webhook_id -> directory row). A
declared director whose message is not verified degrades to information standing and
the frame line says why (it renders `(unverified)` and the reason; the wire value is
still `labeled`) -- standing-tier directors are the recommended shape because they stay
verified through replay. Operator-tier messages are ALWAYS directives, declared or not. No
transitivity: one hop, declared at launch, dead with the session.

Truth in labeling: this is authority CALIBRATION, not access control. A
model can ignore its director or obey a stranger; what this fixes is the default.
"""

from __future__ import annotations

from dataclasses import dataclass

from .attribution import TIER_OPERATOR, VERIFIED
from .scoping import NAME_RE, identity_of, split_tag

ENV_VAR = "MOXI_COMMS_DIRECTORS"
SEPARATOR = ","

DIRECTIVE, INFORMATION = "directive", "information"

_DIRECTOR_NOTE = "your director"
#: The one place a deviation still needs a sentence rather than a mark: `(unverified)`
#: says what the frame knows, and this says what it COSTS -- the authority the recipient
#: granted does not apply to a message whose sender was not proven.
_DEGRADED_NOTE = ("your declared director, but this message's attribution is "
                  "unverified — information standing")


class BadDirector(ValueError):
    """A director name that is not a name. Refused where it was typed, never dropped."""


# --------------------------------------------------------------------- the declaration

def qualify_director(tag) -> str:
    """Qualify ONE declared director by the one rule every name reads by.

    A bare name is the global identity, `name@scope` is explicit, and a global identity
    renders bare (`name@global` -> `name`). The declaring session's scope plays no part.
    An `agent-N` is global and unscoped wherever it is written, exactly as the daemon
    resolves it -- so `agent-7@proj-a` normalises to `agent-7` rather than to an address
    that could never match.
    """
    if not isinstance(tag, str):
        raise BadDirector(f"director {tag!r} is not a name")
    name, at = split_tag(tag.strip())
    if not NAME_RE.match(name or ""):
        raise BadDirector(f"director {tag!r} is not a name "
                          "(expected `name` or `name@scope`)")
    if at is not None and not NAME_RE.match(at):
        raise BadDirector(f"director {tag!r} has no usable scope after '@'")
    return identity_of(tag.strip())


def declare(tags) -> list[str]:
    """The launch-time declaration: qualified, de-duplicated, order preserved."""
    declared: list[str] = []
    for tag in tags or ():
        qualified = qualify_director(tag)
        if qualified not in declared:
            declared.append(qualified)
    return declared


def stamp(directors) -> dict:
    """The env stamp, beside the identity stamp. Empty declaration stamps nothing."""
    directors = list(directors or ())
    return {ENV_VAR: SEPARATOR.join(directors)} if directors else {}


def from_env(env=None) -> tuple:
    """Read the stamp back. Already qualified -- never re-qualified (see the module doc)."""
    import os

    env = os.environ if env is None else env
    raw = env.get(ENV_VAR) or ""
    return tuple(part.strip() for part in raw.split(SEPARATOR) if part.strip())


def validated(tags) -> tuple:
    """Shape-check an already-qualified list (the adapter's own flag / env stamp)."""
    out: list[str] = []
    for tag in tags or ():
        if not isinstance(tag, str) or not tag.strip():
            raise BadDirector(f"director {tag!r} is not a name")
        name, at = split_tag(tag.strip())
        if not NAME_RE.match(name or "") or (at is not None and not NAME_RE.match(at)):
            raise BadDirector(f"director {tag!r} is not a qualified name "
                              "(expected `name` or `name@scope`)")
        if tag.strip() not in out:
            out.append(tag.strip())
    return tuple(out)


# ----------------------------------------------------------------------- the rendering

@dataclass(frozen=True)
class Standing:
    """How one delivered message presents: the frame line's lead, and why."""

    kind: str                 # directive | information
    lead: str                 # the words before the author's name
    note: str | None          # the short factual reason, or nothing to say

    @property
    def directive(self) -> bool:
        return self.kind == DIRECTIVE


def standing_of(author: dict, directors=()) -> Standing:
    """What standing this author has WITH THIS RECIPIENT. The whole authority check.

    Operator first, and unconditionally: the operator's word is a directive whether or
    not anything was declared. Then the declaration, gated on verified attribution --
    an unverified director keeps information standing and says so.
    """
    author = author or {}
    if author.get("tier") == TIER_OPERATOR:
        return Standing(DIRECTIVE, "directive from", None)
    if author.get("name") in set(directors or ()):
        if author.get("attribution") == VERIFIED:
            return Standing(DIRECTIVE, "directive from", _DIRECTOR_NOTE)
        return Standing(INFORMATION, "from", _DEGRADED_NOTE)
    return Standing(INFORMATION, "from", None)


__all__ = ["ENV_VAR", "SEPARATOR", "DIRECTIVE", "INFORMATION", "BadDirector", "Standing",
           "declare", "from_env", "qualify_director", "stamp", "standing_of", "validated"]
