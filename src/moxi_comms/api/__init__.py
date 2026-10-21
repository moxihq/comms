"""comms' interface 1: the module other code reaches (`moxi_comms.api`).

Everything named here is public and pinned by comms' own tests; anything else in
`moxi_comms` is private. A Moxi module reaches only this module (the contract check
refuses the rest); `moxi_comms.adapter` is the other public surface, for adapters built
outside comms.

**Sending.** There is one send path, and `moxi comms send` renders what it returns.

* `send(sender, to, text, *, posture, ...)` sends one message and returns a
  `SendResult`. `check(...)`, with the same arguments, runs every check and posts
  nothing (`moxi comms send --check`).
* `Sender`: who sends. `Sender.session()` (this process's stamps and its session's claim
  socket), `Sender.socket(path)`, `Sender.claim(server)` (a `ClaimServer` this process
  holds) or `Sender.operator(user_id)` (the operator's lane).
* `SendResult`: `outcome` (one of `OUTCOMES`), `exit_code` (the verb's number for it),
  `message_id`, `recipients` (one `Recipient` per resolved name, its `delivery` one of
  `DELIVERIES`), `refusal` (a `Refusal`, `code` and `text`, when nothing was posted),
  `notes`, and `lines()`, the verb's text. Branch on the data, never on the text.

**The other surfaces.**

* `ClaimServer`, for send-only identities held by a process of their own.
* Reads: `attributed(message_id)`, `session(identity)`, `roster()`, `owner(session_id)`,
  `record_dir()`, `daemon_socket()`.
* Names: `GLOBAL`, `qualify`, `split_tag`, `NAME_RE`.
* `replay()`: the reader catch-up bound in force, `(messages, seconds)`.
* `DaemonError`, `DaemonOffline`: what a read raises when the daemon refuses or is down.
* `moxi_comms.api.tokens`: the token store the box's own consumers read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .. import claimfile as _claimfile
from .. import config as _config
from .. import directory as _directory
from .. import machine_line as _machine_line
from .. import paths as _paths
from .. import sending as _sending
from ..claimhold import ClaimServer
from ..client import DaemonError, DaemonOffline
from ..scoping import GLOBAL, NAME_RE, qualify, split_tag
from ..sending import (DELIVERIES, EXIT_CODES, OUTCOMES, Recipient, Refusal, Sender,
                       SendResult)

#: The interface this module is.
INTERFACE = 1

#: A read's default wait for the daemon, in seconds.
READ_DEADLINE = 10.0

#: What `posture` may be: a new send states one; a reply may leave it None.
POSTURES = (_machine_line.POSTURE_NONE, _machine_line.POSTURE_OPTIONAL,
            _machine_line.POSTURE_ONCE, _machine_line.POSTURE_OPEN)


# --------------------------------------------------------------------------- sending

def send(sender: Sender, to, text: str, *, posture, reply_to=None, new: bool = False,
         audience=None, held: bool = False, cap=None, no_cap: bool = False,
         deadline: float = 30.0, standing: bool = False,
         new_name: bool = False) -> SendResult:
    """Send `text` to `to` as `sender`, with every check `moxi comms send` makes.

    `to` is a name, a comma-separated list of names, or a sequence of names. With `to`
    None and `reply_to` a message id, this is `moxi comms reply`: the message goes to that
    message's author (or, for your own message, to its recipients). With both, it is
    `send --reply-to`. `posture` is `none`, `optional`, `once` or `open`, or None (a
    reply, or the operator). `cap` is a whole number of replies. `standing` and
    `new_name` are the verb's `--standing` and `--new-name`. `deadline` is seconds,
    passed to every daemon call, to the claim socket and to the record's busy timeout.

    The result says whether it posted (`message_id`), and a post either commits or does
    not. Raises only on arguments the verb's parser would refuse (`ValueError`,
    `TypeError`)."""
    return _run(sender, to, text, posture=posture, reply_to=reply_to, new=new,
                audience=audience, held=held, cap=cap, no_cap=no_cap, deadline=deadline,
                standing=standing, new_name=new_name, check=False)


def check(sender: Sender, to, text: str, *, posture, reply_to=None, new: bool = False,
          audience=None, held: bool = False, cap=None, no_cap: bool = False,
          deadline: float = 30.0, standing: bool = False,
          new_name: bool = False) -> SendResult:
    """`send`'s every check, stopping before the post: outcome `would-send`, with the
    recipients and notes the post would have, or the refusal the send would give.
    Posts, nudges and records nothing."""
    return _run(sender, to, text, posture=posture, reply_to=reply_to, new=new,
                audience=audience, held=held, cap=cap, no_cap=no_cap, deadline=deadline,
                standing=standing, new_name=new_name, check=True)


def _run(sender, to, text, *, posture, reply_to, new, audience, held, cap, no_cap,
         deadline, standing, new_name, check) -> SendResult:
    if not isinstance(sender, Sender):
        raise TypeError(f"sender must be an api.Sender, not {type(sender).__name__}")
    if posture is not None and posture not in POSTURES:
        raise ValueError(f"posture must be one of {', '.join(POSTURES)} or None, "
                         f"not {posture!r}")
    args = SimpleNamespace(text=str(text), posture=posture, cap=cap, no_cap=bool(no_cap),
                           audience=audience, new=bool(new), held=bool(held),
                           standing=bool(standing), new_name=bool(new_name),
                           check=check, reply_to=None, message_id=None)
    tags = None
    if to is None:
        if reply_to is None:
            raise ValueError("send needs `to`, or `reply_to` alone for a reply to a "
                             "message's author")
        args.message_id = str(reply_to)
    else:
        names = to.split(",") if isinstance(to, str) else [str(t) for t in to]
        tags = [t.strip() for t in names if t.strip()]
        if not tags:
            raise ValueError("send needs `to` to name at least one recipient")
        args.reply_to = None if reply_to is None else str(reply_to)
    ctx = _sending.Context(env=sender.environ(os.environ), deadline=deadline)
    return _sending.run(ctx, args, tags=tags)


# --------------------------------------------------------------------------- reads

def _daemon(deadline: float):
    """A connection to the daemon once it has answered with this comms' protocol."""
    return _sending.Context(deadline=deadline).daemon()


def attributed(message_id, *, deadline: float = READ_DEADLINE) -> dict | None:
    """One message with its author as the daemon attributes it, or None when the record
    holds no such id: `message_id`, `room`, `author` (`name`, `tier`, `attribution`),
    `recipients`, `resolved`, `text`, `timestamp`. Raises `DaemonOffline` or
    `DaemonError`."""
    client = _daemon(deadline)
    try:
        return client.attributed_message(message_id)
    finally:
        client.close()


def session(identity: str, *, deadline: float = READ_DEADLINE) -> dict:
    """Which live session sits behind one bound name: the daemon's `session` answer
    (`bound`, `adapter`, `session_id`, `bound_at`, the cache read, ...). An unbound name
    answers `bound` False. Raises `DaemonOffline` or `DaemonError`."""
    client = _daemon(deadline)
    try:
        answer = client.query("session", identity=str(identity))
    finally:
        client.close()
    return {k: v for k, v in answer.items() if k not in ("id", "ok")}


@dataclass(frozen=True)
class Agent:
    """A bound name: its binding as the daemon holds it, and the directors its session
    declared at launch."""

    qualified: str
    name: str
    scope: str
    tier: str | None
    adapter: str | None
    slot: int | None
    directors: tuple = ()


@dataclass(frozen=True)
class Standing:
    """A standing identity's directory row. `kind` is `agent` or `send-only`."""

    qualified: str
    name: str
    scope: str
    kind: str


@dataclass(frozen=True)
class Roster:
    """Who can be messaged: the bound names, and the standing rows."""

    bound: tuple = ()
    standing: tuple = ()


@dataclass(frozen=True)
class Owner:
    """Which identity owns one session id now (`identity` None when no live binding
    does), its adapter kind, the directors it declared, and whom it awaits."""

    session_id: str
    identity: str | None
    adapter: str | None = None
    directors: tuple = ()
    awaiting: tuple = ()


def _declared_directors() -> dict:
    """qualified -> the directors its live claim declares."""
    out = {}
    for _key, record in _claimfile.live_records():
        qualified = _claimfile.held_identity(record)
        if qualified:
            claim = record.get("claim") or {}
            out.setdefault(qualified, tuple(claim.get("directors") or ()))
    return out


def roster(*, deadline: float = READ_DEADLINE) -> Roster:
    """The bound agents with their slots and declared directors, and the standing rows of
    the directory. Raises `DaemonOffline` or `DaemonError`."""
    client = _daemon(deadline)
    try:
        answer = client.query("agents")
    finally:
        client.close()
    directors = _declared_directors()
    bound = tuple(Agent(qualified=b["qualified"], name=b.get("name"), scope=b.get("scope"),
                        tier=b.get("tier"), adapter=b.get("adapter"), slot=b.get("slot"),
                        directors=directors.get(b["qualified"], ()))
                  for b in answer.get("bindings") or ())
    conn = _directory.connect(_paths.directory_db(), read_only=True)
    try:
        rows = _directory.standing_rows(conn)
        standing = tuple(Standing(qualified=qualify(r["name"], r["scope"]), name=r["name"],
                                  scope=r["scope"], kind=_directory.kind_of(r))
                         for r in rows)
    finally:
        conn.close()
    return Roster(bound=bound, standing=standing)


def owner(session_id: str, *, deadline: float = READ_DEADLINE) -> Owner:
    """Which identity owns `session_id` now, with its declared directors and whom it
    awaits. Raises `DaemonOffline`, or `DaemonError` (an empty id among them)."""
    client = _daemon(deadline)
    try:
        answer = client.query("owner", session_id=str(session_id))
    finally:
        client.close()
    identity = answer.get("owner")
    return Owner(session_id=answer.get("session_id", session_id), identity=identity,
                 adapter=answer.get("adapter"),
                 directors=_declared_directors().get(identity, ()) if identity else (),
                 awaiting=tuple(answer.get("awaiting") or ()))


def record_dir() -> Path:
    """The directory that holds the box's record file (`[local] record`, else its
    default under comms' state directory)."""
    record = _config.load().local.record
    return (Path(record).expanduser() if record else _paths.record_db()).parent


def daemon_socket() -> Path:
    """The comms daemon's socket path."""
    return _paths.daemon_socket()


def replay() -> tuple[int, int]:
    """The reader catch-up bound in force: `(messages, seconds)`, from the config file's
    `[replay] messages` and `hours`."""
    bound = _config.load().replay
    return bound.messages, bound.hours * 3600


__all__ = [
    "INTERFACE", "send", "check", "Sender", "SendResult", "Recipient", "Refusal",
    "OUTCOMES", "DELIVERIES", "EXIT_CODES", "POSTURES", "ClaimServer", "attributed",
    "session", "roster", "owner", "Roster", "Agent", "Standing", "Owner", "record_dir",
    "daemon_socket", "GLOBAL", "qualify", "split_tag", "NAME_RE", "replay",
    "DaemonError", "DaemonOffline",
]
