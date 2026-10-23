"""The name lane's machine line.

Shape: ONE line at position zero of the message body, sigil-marked, plaintext an
operator can type, carrying the recipient tags and the marks below. Tag-shaped text
anywhere else in the body is prose and is never routed on.

Grammar:

    ::to <tag>[, <tag>]* [re:<message_id>] [audience:<tag>] [operator-words] [via-phone]
         [held | next-turn] [cold-at:<t>|-,...] [cap:<n> | no-cap]
         [1 reply left | last reply] [speaker]
         [no reply | reply-optional | reply-once | open]

  ``[operator-words]``, ``[via-phone]`` and ``[speaker]`` are marks an older comms
  wrote; read and ignored.

  * the sigil is the two characters ``::`` immediately followed by ``to`` -- chosen
    because no markdown construct starts a line with it, a quoted sentence
    never begins with it, and it is quick to type by hand.
  * a <tag> is ``name`` or ``name@scope``; charset ``[A-Za-z0-9._-]``.
  * every token after the tags is optional, appears at most once, and sits in the
    order above. Out of order or doubled, the whole line is prose. Each bracketed
    token's square brackets are typed.
  * ``re:<message_id>`` is the REPLY CORRELATION token: the referent's message id
    (digits only). A post carries no reply field, so this token is the ONLY place a
    reply's referent lives: the record is self-describing.
  * ``[audience:<tag>]`` is the AUDIENCE MARK: who the message's answer is for. The
    value is a <tag> and is LOWERCASED at parse, so ``[audience:Operator]`` and
    ``[audience:operator]`` are one audience. It is a label, not an address: nothing
    resolves it through the directory, and nothing in comms is gated on it.
  * ``[held]`` is the HELD MARK: the daemon queues the message for each recipient and
    hands it over at that recipient's next release point (its next turn, or its session
    starting, being cleared or being compacted); nothing wakes it for this message alone.
    The send verb writes it (``--held``); it has to be in the record because the daemon
    routes from the record. ``[next-turn]``, its name before 2026-10, sits in the same
    slot and parses the same, so old records still read as held (``held``).
  * ``[cold-at:<v>,...]`` is the SEND'S READING of each recipient's cache: one value per
    tag, in tag order, each a whole-second Unix time (when that recipient's cache goes
    cold, as the send read it) or ``-`` (the send read it ``fresh`` or ``no-cache``:
    deliver live). The send verb writes it on every line zero that carries no
    ``[held]``, and never beside ``[held]``; a line with no token carries no reading
    (posted before 2026-10, or not by the verb). A value count unlike the tag count makes
    the line prose. The daemon reads it only when its own read of a bound recipient is
    ``unknown`` (``cold_at``).
  * ``[cap:<n>]`` (n a whole number, 1 or more) or ``[no-cap]`` is the CAP TOKEN: the
    opener of an open thread sets its reply cap, or lifts it. Read back only from the
    opener's verified messages in a thread whose root carries ``[open]``.
  * ``[1 reply left]`` and ``[last reply]`` are the THREAD MARKS the verb computes on a
    reply in an open thread: one more reply closes it; this reply closed it. Only
    ``[last reply]`` is read back; the count is recomputed from the record each time.
  * the POSTURE slot says what the sender wants back: ``[no reply]`` is ``none``,
    ``[reply-optional]`` is ``optional`` (a reply only if the recipient has something to
    add; nothing is owed), ``[reply-once]`` is ``once``, and ``[open]`` is ``open``
    stated on a new send, which
    makes the message the root of a thread with limits. Nothing in the slot is open too,
    with no limits. Words inside ``[no reply]``, ``[1 reply left]`` and
    ``[last reply]`` are separated by one space.
  * the sigil, the ``re:`` token, the ``audience`` keyword and every bracketed token
    are matched case-insensitively (mobile autocapitalisation).
  * leading whitespace on line zero is tolerated; anything else on the line makes it
    prose, and prose is never routed. A malformed token (a non-digit id, a second
    token, a token out of position) therefore costs the whole line, never a partial
    parse.
  * like every tag it is routing/transport, not content: spoofable, typeable by hand,
    carrying no authorization, and stripped before injection.

Example message body (line zero is transport, the rest is what the model sees):

    ::to planner, reviewer@proj-b re:4611686018427387905 [no reply] The parser change
    landed; the suite is green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SIGIL = "::to"

REPLY_PREFIX = "re:"

AUDIENCE_PREFIX = "[audience:"

#: The held mark as the verb writes it, in the slot `[next-turn]` had.
HELD_TOKEN = "[held]"
#: The held mark's name before 2026-10: written by nothing now, still parsed as `held`,
#: so old records read as they were. The daemon's boot read searches the record for it
#: (lowercased), in exactly this spelling.
NEXT_TURN_TOKEN = "[next-turn]"

#: The send's cold-at token: `[cold-at:<v>,...]`, one value per tag.
COLD_AT_PREFIX = "[cold-at:"
#: A cold-at value meaning the send read the recipient `fresh` or `no-cache`: deliver
#: live. Never "no reading": that is a line with no token at all.
COLD_AT_LIVE = "-"

#: The four postures: open, none, optional and once. `POSTURES` is in the order the
#: start text names them.
POSTURE_OPEN, POSTURE_NONE, POSTURE_ONCE = "open", "none", "once"
POSTURE_OPTIONAL = "optional"
POSTURES = (POSTURE_NONE, POSTURE_OPTIONAL, POSTURE_ONCE, POSTURE_OPEN)

#: What each posture writes in line zero's posture slot. Open writes nothing, except on
#: a new send that states it, which writes `OPEN_TOKEN`.
POSTURE_TOKENS = {POSTURE_OPEN: "", POSTURE_NONE: "[no reply]",
                  POSTURE_OPTIONAL: "[reply-optional]", POSTURE_ONCE: "[reply-once]"}

#: The posture slot's token for open stated on a new send: the root of a thread with
#: limits.
OPEN_TOKEN = "[open]"

#: The cap tokens: `[cap:<n>]` sets an open thread's reply cap, `[no-cap]` lifts it.
NO_CAP_TOKEN = "[no-cap]"
CAP_PREFIX = "[cap:"

#: The thread marks the verb computes on a reply in an open thread.
THREAD_ONE_LEFT, THREAD_LAST = "one-left", "last"
THREAD_MARKS = {THREAD_ONE_LEFT: "[1 reply left]", THREAD_LAST: "[last reply]"}

_TAG = r"[A-Za-z0-9._-]+(?:@[A-Za-z0-9._-]+)?"
_LINE_RE = re.compile(
    rf"^[ \t]*::to[ \t]+(?P<tags>{_TAG}(?:[ \t]*,[ \t]*{_TAG})*)"
    r"(?:[ \t]+re:(?P<reply_to>[0-9]+))?"
    rf"(?:[ \t]+\[audience:(?P<audience>{_TAG})\])?"
    r"(?:[ \t]+\[operator-words\])?"   # a mark an older comms wrote; read and ignored
    r"(?:[ \t]+\[via-phone\])?"        # a mark an older comms wrote; read and ignored
    r"(?:[ \t]+(?P<held>\[(?:held|next-turn)\]))?"
    r"(?:[ \t]+\[cold-at:(?P<cold_at>(?:[0-9]+|-)(?:,(?:[0-9]+|-))*)\])?"
    r"(?:[ \t]+\[(?:cap:(?P<cap>[1-9][0-9]*)|(?P<no_cap>no-cap))\])?"
    r"(?:[ \t]+\[(?P<thread_mark>1 reply left|last reply)\])?"
    r"(?:[ \t]+\[speaker\])?"          # a mark an older comms wrote; read and ignored
    r"(?:[ \t]+\[(?P<posture>no reply|reply-optional|reply-once|open)\])?[ \t]*$",
    re.IGNORECASE,
)
_TAG_RE = re.compile(_TAG)

_POSTURE_OF = {"no reply": POSTURE_NONE, "reply-optional": POSTURE_OPTIONAL,
               "reply-once": POSTURE_ONCE, "open": POSTURE_OPEN}
_THREAD_MARK_OF = {"1 reply left": THREAD_ONE_LEFT, "last reply": THREAD_LAST}


@dataclass(frozen=True)
class MachineLine:
    recipients: tuple[str, ...]
    line: str                    # the raw machine line, as it sits in the record
    text: str                    # the body with the machine line stripped (injected)
    reply_to: str | None = None  # the re: token's referent; the record's own correlation
    audience: str | None = None  # the audience mark as written, lowercased; None if absent
    held: bool = False           # `[held]` (or the old `[next-turn]`): queued until a release
    #: "open" | "none" | "optional" | "once"; open for `[open]` and for no token.
    posture: str = POSTURE_OPEN
    open_mark: bool = False      # the `[open]` token: a new send that stated open
    cap: int | None = None       # the `[cap:<n>]` token's n; None when there is none
    no_cap: bool = False         # the `[no-cap]` token
    thread_mark: str | None = None  # "one-left", "last" or None
    #: The send's cold-at, one entry per tag: an int (a Unix time) or `COLD_AT_LIVE`;
    #: None when the line carries no token, so no reading.
    cold_at: tuple | None = None


def parse(body: str) -> MachineLine | None:
    """Parse position zero only. Returns None when line zero is not a machine line."""
    if not body:
        return None
    line, sep, rest = body.partition("\n")
    m = _LINE_RE.match(line)
    if m is None:
        return None
    tags = tuple(t.strip() for t in m.group("tags").split(","))
    token = (m.group("posture") or "").lower()
    mark = (m.group("thread_mark") or "").lower()
    audience = m.group("audience")
    cap = m.group("cap")
    cold_at = m.group("cold_at")
    if cold_at is not None:
        cold_at = tuple(v if v == COLD_AT_LIVE else int(v) for v in cold_at.split(","))
        if len(cold_at) != len(tags):
            return None              # malformed: one value per tag, or the line is prose
    return MachineLine(recipients=tags, line=line,
                       text=rest if sep else "", reply_to=m.group("reply_to"),
                       audience=audience.lower() if audience else None,
                       held=m.group("held") is not None,
                       posture=_POSTURE_OF.get(token, POSTURE_OPEN),
                       open_mark=token == "open",
                       cap=int(cap) if cap else None,
                       no_cap=m.group("no_cap") is not None,
                       thread_mark=_THREAD_MARK_OF.get(mark), cold_at=cold_at)


def normalize_audience(value: str | None) -> str | None:
    """An audience as the wire carries it: lowercased, or None when it is not a tag.

    The verb's check and the parser's are one grammar -- a value this returns None for
    would make line zero prose, so the verb refuses it before anything is posted.
    """
    if value is None:
        return None
    return value.lower() if _TAG_RE.fullmatch(value) else None


def resolve_posture(posture: str | None = None) -> str:
    """The posture a line is written with: `posture=None` means open."""
    if posture is None:
        return POSTURE_OPEN
    if posture not in POSTURES:
        raise ValueError(f"posture must be one of {', '.join(POSTURES)}, got {posture!r}")
    return posture


def format_line(recipients, *, reply_to: str | None = None, audience: str | None = None,
                next_turn: bool = False, posture: str | None = None,
                open_mark: bool = False, cap: int | None = None, no_cap: bool = False,
                thread_mark: str | None = None, cold_at=None) -> str:
    """Line zero. Every parameter after `recipients` is keyword-only, and each defaults
    to writing nothing. `open_mark` writes `[open]` in the posture slot, and only beside
    the open posture. `next_turn` writes `[held]` (the keyword keeps its old name: callers
    outside comms pass it so). `cold_at` writes `[cold-at:…]`: one value per recipient, an int or
    `COLD_AT_LIVE`, and never beside `[held]`."""
    recipients = list(recipients)
    tags = ", ".join(recipients)
    resolved = resolve_posture(posture)
    if open_mark and resolved != POSTURE_OPEN:
        raise ValueError(f"[open] is the open posture, not {resolved!r}")
    if cap is not None and (no_cap or int(cap) < 1):
        raise ValueError(f"a cap is a whole number of replies, 1 or more, got {cap!r}")
    if thread_mark is not None and thread_mark not in THREAD_MARKS:
        raise ValueError(f"thread_mark must be one of {', '.join(THREAD_MARKS)}")
    cold = _cold_at_token(cold_at, len(recipients), held=next_turn)
    token = OPEN_TOKEN if open_mark else POSTURE_TOKENS[resolved]
    return (f"{SIGIL} {tags}"
            + (f" {REPLY_PREFIX}{reply_to}" if reply_to else "")
            + (f" {AUDIENCE_PREFIX}{audience}]" if audience else "")
            + (f" {HELD_TOKEN}" if next_turn else "")
            + (f" {cold}" if cold else "")
            + (f" {CAP_PREFIX}{int(cap)}]" if cap is not None else "")
            + (f" {NO_CAP_TOKEN}" if no_cap else "")
            + (f" {THREAD_MARKS[thread_mark]}" if thread_mark else "")
            + (f" {token}" if token else ""))


def _cold_at_token(cold_at, count: int, *, held: bool) -> str:
    """`[cold-at:…]` for `cold_at`, or "" when it is None. Refuses (ValueError) what
    would make the line prose or say two things: a count unlike the tags', a value that
    is neither a whole-second time nor `-`, and the token beside `[held]`."""
    if cold_at is None:
        return ""
    values = tuple(cold_at)
    if held:
        raise ValueError("[cold-at:…] is never written beside [held]")
    if len(values) != count:
        raise ValueError(f"[cold-at:…] takes one value per tag: {count} tag(s), "
                         f"{len(values)} value(s)")
    for v in values:
        if v != COLD_AT_LIVE and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise ValueError(f"a cold-at value is a whole-second time or "
                             f"{COLD_AT_LIVE!r}, got {v!r}")
    return f"{COLD_AT_PREFIX}{','.join(str(v) for v in values)}]"


def compose(recipients, text: str, *, reply_to: str | None = None,
            audience: str | None = None, next_turn: bool = False,
            posture: str | None = None, open_mark: bool = False, cap: int | None = None,
            no_cap: bool = False, thread_mark: str | None = None, cold_at=None) -> str:
    """Build a postable body: machine line at position zero, then the human text."""
    line = format_line(recipients, reply_to=reply_to, audience=audience,
                       next_turn=next_turn, posture=posture, open_mark=open_mark, cap=cap,
                       no_cap=no_cap, thread_mark=thread_mark, cold_at=cold_at)
    return f"{line}\n{text}"
