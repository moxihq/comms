"""Agent cards: the read side of `moxi comms whois <name>`.

A card is written ABOUT an identity, FOR its peers -- role, ownership, what to bring it
-- by the holder itself, as an ordinary file write. comms ships no writer: a write verb
would be a fifth verb that ACTS, and the acting verbs stay four. So comms' entire
relationship with card content is that it READS it, which is also what makes the surface
harness-neutral -- a codex holder, an opencode holder, or the operator with an editor
authors a card with no comms code at all.

Four properties this module exists to keep:

  * **The help model.** No daemon, no credential, no network, no database, no directory
    lookup -- one file read, and at most `CARD_READ_LIMIT` bytes of it. `EXIT_OFFLINE` is
    unreachable here by construction, and a process whose launch stamp is broken still
    gets an answer: the claim env is read DIRECTLY (`MOXI_COMMS_NAME`/`MOXI_COMMS_SCOPE`/
    `MOXI_COMMS_GLOBAL`) and `MOXI_COMMS_SLOT` is never looked at, so `sender_from_env`'s third
    answer (`MalformedStamp`) is out of this verb's scope by construction. The reader who
    most needs to know who it is about to message is often the one whose environment is
    wrong.
  * **The budget, enforced at READ.** 10 lines / 800 characters, whichever binds first.
    With no writer there is nowhere else the cap can live -- so over budget the reader
    shows the first ten lines and SAYS SO, in the envelope, in the holder's name.
    Truncate-and-say, never silent truncation and never a refusal (refusing would punish
    the reader for the author's defect). Declared residue: a holder is never told "no";
    only its readers are.
  * **A card is a DISPLAY CLAIM, never an authority surface.** Directors remain the
    authority mechanism. Nothing here enters routing, attribution or any authorization
    decision, and the fence is structural rather than promised: this module is imported
    by `cli` alone (a leaf: nothing in the package imports it), and a test walks the
    import graph to say so.
  * **A card is untrusted text rendered into a peer's context.** It gets the frame line's
    treatment: an envelope opened AND closed, every comms-authored line inside it, C0
    control characters stripped (so no card can drive a terminal), no interpretation of
    any kind, and a two-space body indent that is load-bearing -- it is the only thing
    that stops a body line reading `[comms card] end · …` from forging the footer.
    Those three pieces live in `envelope.py` and are SHARED with delivered message
    bodies.

Honesty, twice over: the header says *claims to be* self-authored, because nothing proves
a card's author is its subject any more than anything proves a name's claimant is its
owner (cooperative and unauthenticated); and the one staleness fact comms states is
the file's own mtime, omitted rather than guessed when it cannot be read.
"""

from __future__ import annotations

import codecs
import errno
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import CARD_MAX_CHARS, CARD_MAX_LINES, CARD_NATIVE_MAX, CARD_READ_LIMIT
# The indent, the scrub and the closed-envelope rule are SHARED with the delivery
# adapter's message bodies (`adapters/claude_shim.py`). `envelope` imports nothing from
# the package, so the card reader's import closure is unchanged in kind by using it.
from .envelope import INDENT, indent, scrub
from .paths import cards_dir
from .scoping import GLOBAL, NAME_RE, is_auto_name, qualify, split_tag

# Statuses, not exit codes: the exit table lives at the CLI, and the reader has
# no business knowing the numbers.
OK, MISS, REFUSED, UNREADABLE = "ok", "miss", "refused", "unreadable"

MARKS = "(claims to be self-authored, unverified)"
FOOTER = "[comms card] end · what someone says about themselves, not what comms can prove."
LIVENESS = ("A card says who someone is, never whether they are awake — `moxi comms agents` "
            "is that question.")

# `native-session`, not bare `native`: one word is too broad to reserve inside free prose.
NATIVE_RE = re.compile(r"^\s*native-session:(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Answer:
    """What the verb should print, and which class of answer it is."""

    status: str
    text: str


def card_path(qualified: str) -> Path:
    """The whole reference from identity to card: a pure function of the qualified name.

    No column, no row, no `meta` key, no declared path -- the directory keeps exactly one
    assigned writer and gains no schema, and the identity namespace IS the reference.
    """
    return cards_dir() / f"{qualified}.md"


def own_scope(env) -> str:
    """The caller's own scope, read off its claim stamp: `global` for `MOXI_COMMS_GLOBAL`,
    else `MOXI_COMMS_SCOPE`, else `global`. A process with no scope in its stamp is the global
    identity of its name, as a bind that names no scope is; the working directory's
    project is never a scope. Read only for the caller's own identity."""
    if env.get("MOXI_COMMS_GLOBAL"):
        return GLOBAL
    return env.get("MOXI_COMMS_SCOPE") or GLOBAL


def _candidates(name: str, scope: str | None, env) -> tuple[list[str], str]:
    """The one card to look for, and anything comms had to say about it (nothing now).

    A tag names one identity: `name@scope` is that scope's, and a bare name is the
    global identity, whoever asks. No liveness is read, which the header's resolved
    identity surfaces.
    """
    return [qualify(name, scope or GLOBAL)], ""


def _is_name(value: str | None) -> bool:
    """`NAME_RE` with no trailing-newline hole. `NAME_RE`'s `$` matches before a final
    newline, so `x\\n` passes `.match()` -- and this verb is the first surface to put a
    validated name straight into rendered envelope text, where a raw newline would split
    the header line in two. `fullmatch` closes it here; the package-wide repair belongs
    to whoever next touches `scoping`."""
    return bool(value) and NAME_RE.fullmatch(value) is not None


def _own_identity(env) -> str | None:
    """Who the caller is, read straight off the claim env; None when it has no claim."""
    name = env.get("MOXI_COMMS_NAME")
    if not _is_name(name):
        return None
    return qualify(name, own_scope(env))


def answer(arg: str, env) -> Answer:
    """`moxi comms whois <arg>`: validate, resolve, read one file, render."""
    name, scope = split_tag(arg)
    if not _is_name(name) or (scope is not None and not _is_name(scope)):
        # Refused BEFORE a path is composed. Path escape is structural rather than
        # defensive -- NAME_RE's charset contains no separator, so a name cannot traverse
        # -- but the shape is named the way `directors.py` names a bad director.
        return Answer(REFUSED, f"{arg!r} is not a comms identity (expected `name` or "
                               "`name@scope`). Nothing was read.")
    if is_auto_name(name):
        # The sharpest case in the recycle family, and the only one that can be closed:
        # `agent-N` is monotonic within a daemon lifetime and handed out again after a
        # restart, so a card keyed to one would be inherited by a genuine stranger.
        return Answer(REFUSED, f"{name} is an ephemeral name: it is handed out by the "
                               "daemon, describes nobody in particular, and carries no "
                               "continuity to describe. There are no cards for agent-N "
                               "names.")
    candidates, note = _candidates(name, scope, env)
    for qualified in candidates:
        path = card_path(qualified)
        try:
            data, truncated = _read(path)
        except FileNotFoundError:
            continue
        except NotACard as e:
            return Answer(UNREADABLE, _unreadable(qualified, str(e)))
        except OSError as e:
            return Answer(UNREADABLE, _unreadable(qualified, _oserror(e)))
        try:
            text = _decode(data, truncated)
        except UnicodeDecodeError:
            # Never mojibake and never a guess: the same refusal-to-fabricate posture as
            # attribution's `unknown`.
            return Answer(UNREADABLE, _unreadable(qualified, "it is not valid UTF-8"))
        return Answer(OK, render(qualified, path, text, truncated, note=note))
    return _miss(candidates, env, note=note)


# --------------------------------------------------------------------------- reading

class NotACard(Exception):
    """The path is there and it is not a regular file comms will read."""


def _read(path: Path) -> tuple[bytes, bool]:
    """At most `CARD_READ_LIMIT` bytes of a REGULAR file, and whether there was more.

    A card path is a name any local process can create, so the open is the hostile part.
    A bare `open()` gave two ways to hurt a reader that the byte cap says nothing about:

      * a **FIFO** at the path blocks the open forever, wedging every peer that looks
        that identity up -- the cap bounds what a card can cost in context and memory,
        never what it can cost in TIME;
      * a **symlink** is followed, so up to 8 KB of any file the reading process can open
        renders inside a `claims to be self-authored` envelope. No privilege is crossed
        under the single-operator model, but an AGENT boundary is: one agent would be
        choosing what lands in another's context.

    So: `O_NOFOLLOW` (a symlink is refused, not followed), `O_NONBLOCK` (a FIFO answers
    at once instead of waiting), and `S_ISREG` on the descriptor comms actually holds --
    checked after the open, on the same file, so there is no window between the check and
    the read. Everything irregular joins the existing unreadable-card arm, which already
    says whose card it is and guesses nothing.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotACard("it is not a regular file, and comms reads only regular "
                           "files here")
        chunks, size = [], 0
        while size <= CARD_READ_LIMIT:
            chunk = os.read(fd, CARD_READ_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data[:CARD_READ_LIMIT], len(data) > CARD_READ_LIMIT


def _oserror(e: OSError) -> str:
    """What to say about an open that failed. A refused symlink is named as one: the
    reader did not fail to read a card, it declined to read something else."""
    if e.errno in (errno.ELOOP, errno.EMLINK):
        return "it is a symbolic link, and comms follows none here"
    return e.strerror or str(e)


def _decode(data: bytes, truncated: bool) -> str:
    """Strict UTF-8. When the read cap bound, a trailing partial character is dropped
    rather than reported as invalid -- the cut is comms' own, not the holder's defect."""
    return codecs.getincrementaldecoder("utf-8")().decode(data, not truncated)


def _unreadable(qualified: str, reason: str) -> str:
    return (f"[comms card] {qualified} · there is a card here and comms could not read "
            f"it ({reason}). comms shows nothing rather than guessing at it; the file is "
            "the holder's to repair.")


def _miss(candidates: list[str], env, *, note: str = "") -> Answer:
    """Absence is the normal answer, not a fault: most identities have no card, and a
    parked holder has none for a long time. Exit 0, plain words, no alarm vocabulary."""
    qualified = candidates[0]
    lines = [f"[comms card] {qualified} · no card. Most identities have none; nothing is "
             "wrong."]
    me = _own_identity(env)
    if me is not None and me in candidates:
        # Only the caller's own identity is told where to write. Peer-facing output
        # carries no filesystem plumbing, the same instinct as keeping slots out of it.
        lines.append(f"You are {me} and have no card. Write up to {CARD_MAX_LINES} lines "
                     f"({CARD_MAX_CHARS} characters) at {card_path(me)} — write the whole "
                     "file and rename it into place, so a peer never reads it "
                     "half-written.")
    if note:
        lines.append(note)
    lines.append(LIVENESS)
    return Answer(MISS, "\n".join(lines))


# --------------------------------------------------------------------------- rendering

def render(qualified: str, path: Path, text: str, truncated: bool, *,
           note: str = "") -> str:
    """The closed envelope. Every comms-authored line renders INSIDE it -- an
    unenveloped notice is a line a card body could forge."""
    shown, over = _budget(text, truncated)
    out = [_header(qualified, path)]
    claimed = False
    for line in shown:
        clean = scrub(line.rstrip("\r"))
        if not claimed:
            clean, claimed = _native(clean)
        out.append(indent(clean))
    for said in (over, note):
        if said:
            out.append(indent(said))
    out.append(FOOTER)
    return "\n".join(out)


def _header(qualified: str, path: Path) -> str:
    """The identity comms actually answered for, the marks, and the file's own age."""
    age = _age(path)
    return f"[comms card] {qualified} {MARKS}" + (f" · {age}" if age else "")


def _budget(text: str, truncated: bool) -> tuple[list[str], str]:
    """The lines to show, and the over-budget notice (empty when the card fits).

    What counts as a line is fixed so the holder and the test agree: a single trailing
    newline is not a line, blank lines count, the native-session line counts, and nothing
    is exempt -- an exempt header is how a budget starts growing.
    """
    body = text[:-1] if text.endswith("\n") else text
    lines = body.split("\n") if body else []
    eligible = "\n".join(lines[:CARD_MAX_LINES])
    kept = eligible[:CARD_MAX_CHARS]
    over_lines = len(lines) > CARD_MAX_LINES
    # Measured against the lines that were ELIGIBLE to be shown, not against the whole
    # body: when the line cap binds first, the character cap may never
    # bind at all, and a notice that reports the body's character count would be stating
    # a cut comms did not make. The notice says exactly what it did.
    over_chars = len(eligible) > CARD_MAX_CHARS
    clauses = []
    if over_lines:
        # When the read cap bound, the reader did not see the whole file -- so it says
        # "more than", never a total it cannot have counted.
        count = f"more than {CARD_MAX_LINES}" if truncated else str(len(lines))
        clauses.append(f"{count} lines; comms shows the first {CARD_MAX_LINES}")
    if over_chars:
        count = f"more than {CARD_MAX_CHARS}" if truncated else str(len(eligible))
        where = f" in the first {CARD_MAX_LINES} lines" if over_lines else ""
        clauses.append(f"{count} characters{where}; comms shows the first "
                       f"{CARD_MAX_CHARS}")
    notice = f"over budget: this card is {', and '.join(clauses)}." if clauses else ""
    return (kept.split("\n") if kept else []), notice


def _native(line: str) -> tuple[str, bool]:
    """The one optional structured line, rendered IN PLACE with its marker.

    At most one claim: the first matching line carrying a value is it, and any later one
    renders as ordinary card text. Line zero's "fails whole" strictness is right for a
    ROUTED line; a card routes nothing, so discarding a holder's text would be
    gratuitous. Nothing checks the value against any roster -- verifying it would couple
    comms to a harness-owned format, which is the coupling the card surface exists to
    avoid.
    """
    m = NATIVE_RE.match(line)
    if m is None:
        return line, False
    value = m.group(1).strip()
    if not value:
        return line, False          # an empty key line is ordinary text, and no claim
    if len(value) > CARD_NATIVE_MAX:
        value = f"{value[:CARD_NATIVE_MAX]} …(truncated)"
    return f"native session (self-claimed, unverified): {value}", True


def _age(path: Path) -> str | None:
    """`written 3 days ago`, from the file's mtime -- the only staleness fact comms
    states, and the same honesty as the roster snapshot's "as of session start". A card
    carries no self-reported date: that would be one more claim, and one more line spent.
    Unreadable mtime omits the clause; nothing is guessed."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    delta = max(0.0, time.time() - mtime)
    for seconds, unit in ((86400.0, "day"), (3600.0, "hour"), (60.0, "minute")):
        if delta >= seconds:
            n = int(delta // seconds)
            return f"written {n} {unit}{'s' if n != 1 else ''} ago"
    return "written just now"
