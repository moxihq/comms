"""Per-identity delivery cursors (STATE/comms/cursors/<backend>, keyed by qualified name).

**The ADAPTER writes the cursor**, advancing to the max message_id of an injected
batch when the injected turn RUNS TO COMPLETION. The daemon only ever reads them --
"handled?" is answered by "cursor advanced past it", the one durable fact.

One stream per identity: all name-lane mail rides #names, so there are no per-room
cursors.

One DIRECTORY per backend. A cursor is a position in ONE record's id space, and two
records do not share one, so each backend's cursors are filed apart and a flip in either
direction starts from that backend's own positions. A `discord/` directory left from
before comms was local-only is never read. The backend is resolved here, not passed:
`read` and `advance` keep their signatures and no caller names one.

Every way of answering "no" or "nothing to do" here carries a NAME: see the named reasons
below, `explain` at the bottom of the file, and `moxi comms explain`, which prints them.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .paths import cursors_dir

log = logging.getLogger("comms.cursors")


# --------------------------------------------------------------- the named reasons
#
# There are EIGHT places here that answer "no" or "nothing to do". A silent deliberate
# default is indistinguishable from an accident, which makes a quiet agent a
# log-reading exercise.
#
# So every one of them has a NAME and a sentence. Seven names cover the eight places
# (an unreadable cursor file has two ways of being unreadable and one name for both),
# and `BEHIND` is the eighth name: not a path, but the answer `explain` gives when the
# cursor simply has not got there yet. Two things use the names. Each path logs its own
# (`[<name>] ...`, so a grep over one session's log finds it), and `cursors.explain`
# hands the live one to `moxi comms explain`.
#
# A name is not an error code. Most of these are the cursor rule working: `no-cursor-yet`
# and `cursor-already-past-it` are the two commonest states in a healthy fleet. What the
# name buys is that the command can say WHICH default it took.

#: No cursor file. Nothing has completed a turn under this name yet.
NO_CURSOR_YET = "no-cursor-yet"
#: A cursor file that will not parse, or parses to something that is not a record.
CURSOR_UNREADABLE = "cursor-file-unreadable"
#: A record whose `cursor` is there and is not a message id.
CURSOR_NOT_A_MESSAGE_ID = "cursor-is-not-a-message-id"
#: The cursor is at or past the message. The handled case.
ALREADY_PAST = "cursor-already-past-it"
#: The cursor is behind the message and nothing declined anything.
BEHIND = "cursor-behind-it"
#: `advance` was handed something that is not a message id. Nothing written.
TARGET_NOT_A_MESSAGE_ID = "advance-target-is-not-a-message-id"
#: The file could not be written. Read-only STATE, a full disk.
WRITE_FAILED = "cursor-write-failed"
#: `gateway.backend_name` raised, so the cursor is filed in the bare directory.
BACKEND_UNRESOLVED = "gateway-backend-unresolved"
#: Not the cursor's own: the message waits in the name's queue. `moxi comms explain` checks
#: it BEFORE the cursor, so a queued id never reads `cursor-behind-it`, whose sentence
#: would be false for it.
WAITING = "waiting"

#: One sentence per name, in the terms of the person reading it at a terminal because
#: an agent went quiet. `moxi comms explain` prints these verbatim; nothing else rewords them.
REASONS = {
    NO_CURSOR_YET: (
        "No cursor has ever been written for this identity. Nothing has completed a "
        "turn under this name, so catch-up replays from the bounded window instead of "
        "from a position."),
    CURSOR_UNREADABLE: (
        "The cursor file is there and comms cannot read it as a cursor record. It "
        "counts as no cursor at all, so mail replays rather than going missing, and "
        "every catch-up keeps replaying until the file is repaired or deleted."),
    CURSOR_NOT_A_MESSAGE_ID: (
        "The cursor file holds a value that is not a message id. Nothing can compare "
        "against it, so it is protecting nothing and the next advance overwrites it."),
    ALREADY_PAST: (
        "The cursor is at or past this message. This is the handled case: the turn "
        "completed and the advance had nothing left to do."),
    BEHIND: (
        "The cursor has not reached this message. Nothing declined anything. The turn "
        "that would advance it has not completed."),
    TARGET_NOT_A_MESSAGE_ID: (
        "An advance was asked for with something that is not a message id. Nothing was "
        "written. A cursor that lies about what was handled loses mail; a batch that "
        "replays only shows it twice."),
    WRITE_FAILED: (
        "The cursor file could not be written. The turn still completed and the record "
        "still holds the mail, so the next catch-up shows it again."),
    WAITING: (
        "This message waits in the queue for the name's next release point: its next "
        "session start, /clear, compaction, or a turn of its own on a warm cache."),
    BACKEND_UNRESOLVED: (
        "The gateway backend would not resolve, so this cursor is read and written in "
        "the bare directory instead of under a backend. A position filed there belongs "
        "to no particular record."),
}

#: The two reasons no reader can see. They happen inside the adapter process at the
#: moment of the write, leave nothing behind on disk, and comms keeps no receipts
#: ledger to put them in. `moxi comms explain` names them and says where to look instead.
WRITE_SIDE_REASONS = (TARGET_NOT_A_MESSAGE_ID, WRITE_FAILED)


def _resolve_backend() -> tuple[str | None, str | None]:
    """(backend, the named reason it could not be resolved) -- see `_backend`."""
    from . import gateway

    try:
        return gateway.backend_name(None, env=os.environ), None
    except Exception as e:  # ConfigError, GatewayError, and whatever config growth adds
        log.warning("[%s] could not resolve the gateway backend for a cursor path (%s); "
                    "using the bare cursors directory", BACKEND_UNRESOLVED, e)
        return None, BACKEND_UNRESOLVED


def _backend() -> str | None:
    """Which record these cursors count in, or None when that cannot be resolved.

    The same resolution every adapter process and the daemon already share
    (`gateway.backend_name`: flag > MOXI_COMMS_GATEWAY > config.toml > `local`), read
    fresh on each call: a flip is a config line plus a daemon restart, so nothing here
    needs to cache across one.

    None is the degraded answer, and it lands where everything else in this module lands.
    `advance` runs inside the adapter's Stop hook, so an unreadable `config.toml` or a
    MOXI_COMMS_GATEWAY naming a backend that does not exist must not raise out of it. The
    caller then files the cursor in the bare directory, and a later resolution that
    succeeds starts fresh -- toward replay, never toward loss.

    Named `BACKEND_UNRESOLVED`. It is a state the cursor can sit in for weeks, so
    `moxi comms explain` reports it on its own line rather than folding it into the verdict.
    """
    return _resolve_backend()[0]


def _dir(backend: str | None) -> Path:
    """`cursors_dir() / <backend>`, except for the mock, which keeps the bare directory.

    The mock's bare path is pinned by `tests/test_paths.py`, and the mock is one
    in-memory record per store file with no flip to make, so there is nothing for a
    segment to separate.
    """
    from .gateway import MOCK

    base = cursors_dir()
    return base if backend is None or backend == MOCK else base / backend


def _path(qualified: str) -> Path:
    """Where this identity's cursor is WRITTEN. Every advance lands under the backend."""
    return _dir(_backend()) / f"{qualified}.json"


def read(qualified: str) -> str | None:
    """The identity's cursor, or None when there is not a usable one.

    Every way of being unusable answers the same way, and "no cursor" degrades toward
    replay and pending -- never toward loss. TypeError is in the tuple for the JSON-LIST
    case: valid JSON, wrong shape, so `["cursor"]` raises instead of KeyErroring and the
    exception escaped into the daemon's routing pass. The shape check does the same job
    for a `cursor` that parsed but is not a message id (a dict, a null), which every
    caller would only have taken an `int()` to.

    Named `NO_CURSOR_YET` and `CURSOR_UNREADABLE`. Answering identically and silently
    would be the worst of the seven: a corrupt cursor file and a brand-new
    agent look the same from here, and both replay everything. The absent file stays
    silent, because that is the state every identity starts in. The unusable one now
    logs, once per read, because nothing else on the box will ever mention it.
    """
    p = _path(qualified)
    if not p.exists():
        return None
    try:
        cursor = json.loads(p.read_text())["cursor"]
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
        log.warning("[%s] %s's cursor file will not read as a cursor record (%s); "
                    "treating it as no cursor, so its mail replays", CURSOR_UNREADABLE,
                    qualified, e)
        return None
    if isinstance(cursor, (str, int)):
        return cursor
    log.warning("[%s] %s's cursor file holds a %s where a message id belongs; treating "
                "it as no cursor, so its mail replays", CURSOR_UNREADABLE, qualified,
                type(cursor).__name__)
    return None


def read_meta(qualified: str) -> dict | None:
    """The whole cursor record (diagnostics), or None when there is not a usable one.

    Same posture as `read`: valid JSON of the wrong SHAPE (a list, a bare number) is not
    a record, and answering None is how every unusable file answers here. It carries the
    same two names, `NO_CURSOR_YET` and `CURSOR_UNREADABLE`, and stays quiet where `read`
    does: this is the diagnostic read, so a caller that got here already has `read`'s
    line for the same file.
    """
    p = _path(qualified)
    if not p.exists():
        return None
    try:
        record = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return record if isinstance(record, dict) else None


def advance(qualified: str, message_id: str) -> str | None:
    """Monotonic advance. Returns the cursor after the write, or None if it did not move.

    **This runs inside the adapter's Stop hook**, which is the same blast radius the
    daemon-side cursor reads were guarded for: a corrupt cursor file must
    not raise out of a hook. A hook that dies takes the turn boundary with it, and the
    turn boundary is the whole cursor mechanism. So every way of failing lands somewhere
    safe, and "safe" always means the same direction:

      * a CURRENT cursor that is not a message id is unusable state, and unusable state
        is *no* cursor -- the monotonic comparison is skipped and the new one is written.
        The old value could not have been protecting anything: nothing can read it either.
      * a `message_id` that is not a message id is a caller bug, and writing it would
        make the cursor lie about what was handled. Nothing is written and nothing is
        raised: the batch replays, which is the direction the cursor always errs in.
      * a file the process cannot write (a read-only STATE dir, a full disk) is logged
        and skipped. The turn still completed; the record still holds the mail; the next
        catch-up shows it again.

    Degrading toward replay is never loss. Degrading toward a raise out of the hook is.

    All four of the ways out of here are named: `TARGET_NOT_A_MESSAGE_ID`,
    `ALREADY_PAST`, `CURSOR_NOT_A_MESSAGE_ID` and `WRITE_FAILED`. Only two of them are
    visible to a later reader, because only two leave the cursor file changed. The other
    two are why `moxi comms explain` ends by naming them and pointing at the session log.
    """
    try:
        target_id = int(message_id)
    except (TypeError, ValueError):
        log.warning("[%s] refusing to advance %s's cursor to %r: that is not a message "
                    "id. Nothing was written; the batch will replay.",
                    TARGET_NOT_A_MESSAGE_ID, qualified, message_id)
        return None
    current = read(qualified)
    if current is not None:
        try:
            if int(current) >= target_id:
                # The one that said nothing at all (the card's own excerpt). It stays at
                # DEBUG: in a healthy fleet this fires on every duplicate advance, so a
                # warning here would bury the reasons worth reading.
                log.debug("[%s] %s's cursor is at %s, which is not behind %s; nothing "
                          "to advance", ALREADY_PAST, qualified, current, target_id)
                return current
        except (TypeError, ValueError):
            # Same reading as the daemon's: a cursor that is not a number is not
            # authority, so it is not an obstacle either.
            log.warning("[%s] cursor for %s is not a message id (%r); treating it as "
                        "unset and writing the one this turn earned",
                        CURSOR_NOT_A_MESSAGE_ID, qualified, current)
    record = {"cursor": str(target_id),
              "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    target = _path(qualified)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{qualified}.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        log.exception("[%s] could not write %s's cursor; it stays where it was and the "
                      "mail replays (never lost, only seen twice)", WRITE_FAILED,
                      qualified)
        return None
    return record["cursor"]


def explain(qualified: str, message_id: str | None = None) -> dict:
    """This identity's cursor as it stands right now, and the named reason it stands there.

    Everything here comes off the disk at the moment of the call: the file, its
    position, its timestamp, which backend directory it was found in. No ledger is
    consulted, because comms keeps none.

    `message_id` is the message being asked about, normally the last one delivered to
    this identity. With one, `reason` is `ALREADY_PAST` or `BEHIND` or whichever way the
    file is unusable. Without one, `reason` is only about the file, and comparing is the
    caller's business.

    The two `WRITE_SIDE_REASONS` can never come back from here. They happen in the
    adapter process while the cursor is being written and leave nothing behind, so the
    honest thing is to name them and say where they are logged, which is what
    `moxi comms explain` does with them.
    """
    backend, backend_reason = _resolve_backend()
    path = _dir(backend) / f"{qualified}.json"
    read_path = path
    record: dict | None = None
    reason: str | None = None
    if not read_path.exists():
        reason = NO_CURSOR_YET
    else:
        try:
            parsed = json.loads(read_path.read_text())
        except (json.JSONDecodeError, OSError):
            reason = CURSOR_UNREADABLE
        else:
            if isinstance(parsed, dict) and isinstance(parsed.get("cursor"), (str, int)):
                record = parsed
            else:
                reason = CURSOR_UNREADABLE
    cursor = None if record is None else record["cursor"]
    position = None
    if record is not None:
        try:
            position = int(cursor)
        except (TypeError, ValueError):
            reason = CURSOR_NOT_A_MESSAGE_ID
    if position is not None and message_id is not None:
        try:
            reason = (ALREADY_PAST if position >= int(message_id) else BEHIND)
        except (TypeError, ValueError):
            reason = None           # the caller handed us something that is not an id
    return {"backend": backend,
            "backend_reason": backend_reason,
            "backend_sentence": REASONS.get(backend_reason),
            # where the next advance WRITES, and where this read came FROM: one file.
            "path": str(path),
            "read_path": str(read_path),
            "exists": read_path.exists(),
            # exactly what `read` would return for this identity, so the command and
            # the daemon's `classify` are never looking at two different values
            "cursor": cursor,
            "updated_at": None if record is None else record.get("updated_at"),
            "reason": reason,
            "sentence": REASONS.get(reason)}
