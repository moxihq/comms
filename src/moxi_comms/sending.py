"""The one send path: `moxi comms send` and `reply` and `moxi_comms.api.send` run it.

Every check a send makes, the machine line, the post and the nudge are here, and none of
it prints. `run` hands back a `SendResult`: the outcome, the message id, one `Recipient`
per resolved name, the refusal when nothing was posted, the NOTEs, and `lines()`, the
verb's text. The CLI renders `lines()` and returns `exit_code`; a library caller branches
on the data. The rules the checks keep are the CLI's module docstring.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field

from . import (attribution, cache, claimfile, claimhold, config, constants, directory,
               machine_line, policy)
from .membership import sessions_of
from .protocol import CLI_PROTOCOL, ProtocolError
from .client import DaemonClient, DaemonError, DaemonOffline, DaemonTimeout
from .gateway import LOCAL, GatewayError
from .paths import OutsideRuntime, directory_db
from .provision import NAMES
from .scoping import (GLOBAL, identity_of, project_key, qualify, tag_matches)
from .sender import (MalformedStamp, OperatorRefused, SendFailed, SenderIdentity,
                     credential_for, operator_credential, operator_from_env, post,
                     post_as_operator, sender_from_env)

log = logging.getLogger("comms.send")


EXIT_OK, EXIT_REFUSED, EXIT_BLOCKED, EXIT_OFFLINE, EXIT_FAILED = 0, 2, 3, 4, 5
#: Refused for the cache: comms could not read a recipient's cache (an `unknown` read, a
#: name that unbound mid-check, a failed `session` query), so nothing was sent and the
#: sender may send again in a moment. A cold recipient is posted and held instead,
#: exit 0. Kept apart from EXIT_REFUSED so a caller can branch on it without reading
#: text: callers read it by number, and as `getattr(moxi_comms.cli, "EXIT_COLD", 6)`.
#: Do not renumber.
EXIT_COLD = 6

#: An `agent-N` name's tier: its held mail ends with its binding.
TIER_EPHEMERAL = attribution.TIER_EPHEMERAL
#: A standing identity's tier: a provisioned identity, never refused as never bound.
TIER_STANDING = attribution.TIER_STANDING

#: What the operator's lane wears on the verb's own surface. Not a wire name: the
#: record's `post_as_user` takes the display off the operators row, so this is only what
#: the resolve query is scoped by and what a log line calls the sender. It is also the
#: operator's identity as a recipient, which `--held` is ignored for.
OPERATOR = "operator"

#: A recipient that is not bound, with or without `--held`: a new session's
#: start releases what waits, and a resume releases it on a warm read or after its
#: first turn.
NOT_BOUND_NOTE = ("NOTE: {name} is not bound right now. The message waits and reaches it "
                  "when a session of that name starts, or, if a session resumes, at the "
                  "resume when its cache is still warm and after its first turn when it "
                  "is not.")

#: `--held` to a bound chosen or standing recipient.
HELD_NOTE = ("NOTE: {name} reads this after its own next turn, or when its session is "
             "cleared, compacted or restarted. Nothing wakes it for this message alone.")

#: A bound recipient whose session has no turns endpoint yet: its adapter holds the mail.
WAITING_NOTE = ("NOTE: {name} is bound, but its session has had no turns endpoint for "
                "{age}, so its adapter holds the message until it finds one. A session "
                "that does not load turns' Claude plugin never gets one; `moxi comms "
                "doctor` lists the sessions waiting.")

#: `--held` to a bound `agent-N`: only its current session releases it.
HELD_NOTE_SESSION_ONLY = ("NOTE: {name} reads this after its own next turn in this session "
                          "(when the operator types into it, or its first turn after a "
                          "/clear). Nothing wakes it for this message alone.")

#: `--held` with the operator among the recipients: he has no session,
#: so it goes to him as an ordinary message. Before the outcome line, exit 0.
HELD_IGNORED_OPERATOR = ("NOTE: --held was ignored for operator: the operator has no "
                         "session to hold it for, so it went to him as an ordinary "
                         "message.")

#: `Context.daemon()`'s refusal of a daemon whose verb protocol is not this comms'
#: (`protocol.CLI_PROTOCOL`). Raised as `DaemonOffline`, so every verb exits 4.
VERB_PROTOCOL_MISMATCH = ("the comms daemon speaks verb protocol {daemon} and this comms "
                          "speaks {verb}; restart the daemon (moxi comms service status names "
                          "its unit), then run this again. Nothing was sent.")


class VerbProtocolMismatch(DaemonOffline):
    """The daemon answers another verb protocol than this comms speaks: offline to every
    verb (exit 4), and said in its own words."""


# --------------------------------------------------------------------------- the result

#: What a send came to. `cache-unread` is exit 6: a recipient's cache comms could not
#: read, so nothing was posted.
OUTCOME_SENT, OUTCOME_WOULD_SEND, OUTCOME_REFUSED, OUTCOME_CACHE_UNREAD = (
    "sent", "would-send", "refused", "cache-unread")
OUTCOME_BLOCKED, OUTCOME_OFFLINE, OUTCOME_FAILED = "blocked", "offline", "failed"
OUTCOMES = (OUTCOME_SENT, OUTCOME_WOULD_SEND, OUTCOME_REFUSED, OUTCOME_CACHE_UNREAD,
            OUTCOME_BLOCKED, OUTCOME_OFFLINE, OUTCOME_FAILED)
#: Each outcome's exit code, the verb's number for it.
EXIT_CODES = {OUTCOME_SENT: EXIT_OK, OUTCOME_WOULD_SEND: EXIT_OK,
              OUTCOME_REFUSED: EXIT_REFUSED, OUTCOME_CACHE_UNREAD: EXIT_COLD,
              OUTCOME_BLOCKED: EXIT_BLOCKED, OUTCOME_OFFLINE: EXIT_OFFLINE,
              OUTCOME_FAILED: EXIT_FAILED}
_BY_EXIT = {EXIT_REFUSED: OUTCOME_REFUSED, EXIT_COLD: OUTCOME_CACHE_UNREAD,
            EXIT_BLOCKED: OUTCOME_BLOCKED, EXIT_OFFLINE: OUTCOME_OFFLINE,
            EXIT_FAILED: OUTCOME_FAILED}

#: How a posted message reaches one recipient:
#:   live       delivered now (a warm, fresh or no-cache read);
#:   held-cold  held for the recipient's next release point, its cache read cold;
#:   held-flag  held because the send carried `[held]`;
#:   waiting    the recipient is bound and its session has no endpoint, so its adapter
#:              holds the mail until it finds one;
#:   unbound    no session holds the name; the message waits for one that does;
#:   operator   the operator, who has no session to hold for.
DELIVERY_LIVE, DELIVERY_HELD_COLD, DELIVERY_HELD_FLAG = "live", "held-cold", "held-flag"
DELIVERY_WAITING, DELIVERY_UNBOUND, DELIVERY_OPERATOR = "waiting", "unbound", "operator"
DELIVERIES = (DELIVERY_LIVE, DELIVERY_HELD_COLD, DELIVERY_HELD_FLAG, DELIVERY_WAITING,
              DELIVERY_UNBOUND, DELIVERY_OPERATOR)


@dataclass(frozen=True)
class Refusal:
    """Why nothing was posted: a stable `code` to branch on and the verb's `text`."""

    code: str
    text: str


@dataclass(frozen=True)
class Recipient:
    """One resolved name of a send. `cold_at` is line zero's value for it, the time (epoch
    seconds) its cache goes or went cold, None when it is delivered live without a
    clock or the send carried `[held]`. `reason` says why the mail waits, None when it
    does not."""

    qualified: str
    delivery: str
    cold_at: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SendResult:
    """What a send did, as data. `message_id` is set exactly when the message was posted;
    `refusal` exactly when the outcome is not `sent` or `would-send`. `recipients` holds
    one `Recipient` per resolved name on a send that posted or would post. `notes` are
    the NOTE lines, in the order the verb says them. `lines()` is the verb's text."""

    outcome: str
    message_id: str | None = None
    recipients: tuple = ()
    refusal: Refusal | None = None
    notes: tuple = ()
    said: tuple = ()

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]

    @property
    def posted(self) -> bool:
        return self.message_id is not None

    def lines(self) -> list[str]:
        """The verb's output, one string per line, exactly as `moxi comms send` prints
        it."""
        return [line for said in self.said for line in str(said).split("\n")]


@dataclass
class _Capture:
    """What a running send has said and kept, for its result."""

    said: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    refusal: Refusal | None = None
    plan: object = None
    operator_lane: bool = False
    message_id: str | None = None


# --------------------------------------------------------------------------- the sender

@dataclass(frozen=True)
class Sender:
    """Who a send is from. Each form is answered as the verb answers this process:

      * `Sender.session()`: this process's stamps and its session's claim socket
        (`MOXI_COMMS_CLAIM_SOCKET`), or the operator's lane when this terminal is it;
      * `Sender.socket(path)`: the claim socket at `path` (one under comms' runtime
        directory), asked from inside its session;
      * `Sender.claim(server)`: a `ClaimServer` this process holds, through its socket;
      * `Sender.operator(user_id)`: the operator's lane, refused inside a session.
    """

    kind: str
    path: str | None = None
    user_id: str | None = None

    @classmethod
    def session(cls) -> "Sender":
        return cls("session")

    @classmethod
    def socket(cls, path) -> "Sender":
        return cls("socket", path=str(path))

    @classmethod
    def claim(cls, server) -> "Sender":
        return cls("claim", path=str(server.socket_path))

    @classmethod
    def operator(cls, user_id: str) -> "Sender":
        return cls("operator", user_id=str(user_id))

    def environ(self, base) -> dict:
        """`base` as the verb would see this sender's process: the claim socket named,
        or the operator's stamp set with no claim socket."""
        from .sender import OPERATOR_VAR

        env = dict(base)
        if self.kind in ("socket", "claim"):
            env[claimhold.CLAIM_SOCKET_ENV] = self.path
        elif self.kind == "operator":
            env.pop(claimhold.CLAIM_SOCKET_ENV, None)
            env[OPERATOR_VAR] = self.user_id
        return env


class Context:
    """Everything the verbs need that is not an argument.

    Which backend the verb talks to is the selection seam: `--gateway`, then
    `MOXI_COMMS_GATEWAY`, then the config file, then the default (`local`). On the mock and
    on `local` the record is a file this process can open, so it opens it.

    `deadline` is seconds, passed to every daemon call, to the claim socket's call and to
    the local record's busy timeout; None keeps each one's own default (the verb's).

    A send runs with a capture open (`run`): what it says is kept for its `SendResult`
    instead of printed, with the refusal and the NOTEs as data beside it.
    """

    def __init__(self, gateway=None, db_path=None, socket_path=None, env=None, out=None,
                 backend=None, clock=None, deadline: float | None = None):
        self.env = env if env is not None else os.environ
        self.deadline = deadline
        self.capture: _Capture | None = None
        #: The verb's clock: the single-reply check reads an ask's age against it.
        #: Injectable, so a rig can age an ask.
        self.clock = clock or time.time
        self.db_path = db_path or directory_db()
        self.socket_path = socket_path
        self.out = out or sys.stdout
        self.backend = backend
        self._gateway = gateway
        self._backend_name = None

    def backend_name(self) -> str:
        """Which backend this process is on, by the selection seam and nothing else.

        Resolved once per verb (flag > MOXI_COMMS_GATEWAY > the config file > the
        default) and kept, because a flip is a config line plus a daemon restart. Asked
        by the nudge, which is the local backend's alone: a gateway handed in by a test
        rig is still whatever the box selected, so the question is put to the seam and
        never to the object.
        """
        if self._backend_name is None:
            from . import gateway as gw
            self._backend_name = gw.backend_name(self.backend, env=self.env)
        return self._backend_name

    @property
    def gateway(self):
        if self._gateway is None:
            from . import gateway as gw
            name = self.backend_name()
            if name == gw.MOCK:
                from .mock_gateway import MockGateway
                store = self.env.get("MOXI_COMMS_MOCK_GATEWAY")
                if not store:
                    raise SystemExit(
                        "comms is pointed at the mock record and has no store: set "
                        "MOXI_COMMS_MOCK_GATEWAY=<store.json>.")
                self._gateway = MockGateway(store)
            else:
                # In-process, like the mock and for the same reason: the record is a
                # file this process may open. Reads are direct WAL reads and writes are
                # direct inserts, so `moxi comms send` lands and `moxi comms history` answers with
                # the daemon down; delivery is what waits for the daemon's boot rescan.
                self._gateway = gw.open_gateway(name, env=self.env,
                                                busy_timeout=self.deadline)
        return self._gateway

    @contextlib.contextmanager
    def db(self):
        """Read-only directory access; the writer role owns the file (0600)."""
        conn = directory.connect(self.db_path, read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def daemon(self) -> DaemonClient:
        """A connection to the daemon, once it has answered `ping` with this comms' verb
        protocol. An answer without `cli_protocol` reads 0. A mismatch, or a ping
        that fails, closes it and raises `DaemonOffline`, which every verb answers with
        exit 4, so no verb writes a token or reads an answer its daemon does not
        speak."""
        client = (DaemonClient(self.socket_path) if self.deadline is None else
                  DaemonClient(self.socket_path, timeout=self.deadline)).connect()
        try:
            answer = client.request("ping")
        except DaemonError as e:
            client.close()
            raise DaemonOffline(f"comms offline: the daemon did not answer ping ({e})") from e
        except DaemonOffline:
            client.close()
            raise
        spoken = answer.get("cli_protocol", 0)
        if spoken != CLI_PROTOCOL:
            client.close()
            raise VerbProtocolMismatch(VERB_PROTOCOL_MISMATCH.format(daemon=spoken,
                                                                     verb=CLI_PROTOCOL))
        return client

    def say(self, text: str = "") -> None:
        if self.capture is not None:
            self.capture.said.append(text)
        else:
            print(text, file=self.out)

    def refuse(self, code: str, text: str) -> None:
        """Say a refusal, and keep it as the send's refusal: nothing was posted."""
        if self.capture is not None:
            self.capture.refusal = Refusal(code, text)
        self.say(text)

    def note(self, text: str) -> None:
        """Say a NOTE, and keep it among the send's notes."""
        if self.capture is not None:
            self.capture.notes.append(text)
        self.say(text)


def _keep(ctx, **facts) -> None:
    """Keep facts of the send (its plan, its message id) for its `SendResult`, when a
    capture is open."""
    capture = getattr(ctx, "capture", None)
    if capture is not None:
        for name, value in facts.items():
            setattr(capture, name, value)


def _refuse(ctx, code: str, text: str) -> None:
    """`ctx.refuse`, or `ctx.say` on a context without one (a caller's own stand-in for
    `_bodies`, which needs only `say` and `gateway`)."""
    refuse = getattr(ctx, "refuse", None)
    if refuse is None:
        ctx.say(text)
    else:
        refuse(code, text)




def _reply(args, ctx: Context) -> int:
    """`moxi comms reply <id>`: read the message it answers, then send to the name its
    author (or, for your own message, its recipients) holds."""
    try:
        original = ctx.gateway.get_message(args.message_id)
    except GatewayError as e:
        # A read that raised is not "no such message": the original may carry an audience
        # the reply has to copy, and posting blind would drop it.
        _refuse(ctx, "record-unreadable",
                f"{e}\nThe record could not be read, so there is nothing to reply to yet. "
                "Nothing was sent.")
        return EXIT_FAILED
    if original is None:
        _refuse(ctx, "no-such-message",
                f"no message {args.message_id} in the record — nothing to reply to.")
        return EXIT_REFUSED
    with ctx.db() as db:
        author = attribution.from_record(db, original)
    if author.tier == "unknown":
        _refuse(ctx, "unknown-author",
                f"message {args.message_id} has an author comms cannot identify — "
                "there is no name to reply to.")
        return EXIT_REFUSED
    # Whom it goes to waits for the sender: a reply to your own message goes to that
    # message's recipients, not back to you (`_reply_targets`). One room: the reply goes
    # to #names whatever room the referent sits in.
    return _send(args, ctx, tags=None, reply_to=original.id, channel_room=NAMES,
                 referent=original, author=author)




#: A reply to your own message whose line zero is not a machine line: no recipient to
#: read, so nothing to send to.
REPLY_OWN_UNREADABLE = ("msg {id} is your own, and comms cannot read whom it was sent to, "
                        "so a reply has no one to go to. Send it as a new message with "
                        "moxi comms send. Nothing was sent.")
#: A reply to your own message that went to no one but you.
REPLY_OWN_ALONE = ("msg {id} is your own and went to no one but you, so a reply has no one "
                   "to go to. Send it as a new message with moxi comms send. Nothing was sent.")


def _identity_named(tag: str) -> str:
    """The identity a tag names, spelled as `qualify` spells it: the one rule,
    `scoping.identity_of`."""
    return identity_of(tag)


def _reply_targets(ctx: Context, sender, author, referent):
    """`moxi comms reply`'s recipients, once the sender is known: `(tags, the "replying to"
    line)`, or an exit code once a refusal is said.

    Someone else's message: its author. Your own (the author's qualified name is the
    sender's, whatever its attribution): the identities it was sent to, each line-zero
    tag as written, read by the one name rule and never in the sender's scope, with the
    sender left out. Each identity once, in line-zero order."""
    if author.qualified != sender.qualified:
        return [author.qualified], (
            f"replying to {author.qualified} "
            f"({author.tier}, {attribution.rendered(author.attribution)}) in {NAMES}")
    ml = machine_line.parse(referent.content)
    if ml is None:
        _refuse(ctx, "reply-own-unreadable", REPLY_OWN_UNREADABLE.format(id=referent.id))
        return EXIT_REFUSED
    seen, tags = set(), []
    for tag in ml.recipients:
        identity = _identity_named(tag)
        if identity == sender.qualified or identity in seen:
            continue
        seen.add(identity)
        tags.append(tag)
    if not tags:
        _refuse(ctx, "reply-own-alone", REPLY_OWN_ALONE.format(id=referent.id))
        return EXIT_REFUSED
    names = ", ".join(_identity_named(t) for t in tags)
    return tags, (f"replying to your own msg {referent.id}, so it goes to that message's "
                  f"recipients: {names} in {NAMES}")


def send_only_refusal(qualified: str, env=None) -> str:
    """The send and reply verbs' refusal of a send-only recipient, in the words the
    config file's `[policy] labels` gives that identity (else "a send-only identity")."""
    what = policy.for_env(env).refusal_words(qualified)
    return (f"{qualified} is {what} and receives no mail; nothing was sent. A brief that "
            "wants a report names whom to report to.")


def _inside_a_session() -> bool:
    """Is this process inside a live comms session by the membership rule?"""
    live = claimfile.live_records()
    return bool(live) and bool(sessions_of(os.getpid(), claimfile.membership_table(live)))


def _claim_sender(ctx: Context, path):
    """The identity this process sends as, read from its session's claim: `(sender,
    state)`, or an exit code once a refusal is said."""
    try:
        answer = (claimhold.call(path, "claim") if ctx.deadline is None else
                  claimhold.call(path, "claim", timeout=ctx.deadline))
    except (OSError, ProtocolError):
        answer = claimhold.dead_adapter_answer(path)
        if answer is None:
            _refuse(ctx, "adapter-gone",
                    "this session's comms adapter is gone and it holds no comms "
                    "identity — nothing was sent.")
            return EXIT_REFUSED
    refusal = (answer.get("error") if not answer.get("ok") else answer.get("line"))
    if refusal:
        _refuse(ctx, "claim-refused", f"{refusal} — nothing was sent.")
        return EXIT_REFUSED
    claim = answer.get("claim") or {}
    slot = claim.get("slot")
    sender = SenderIdentity(qualified=claim["qualified"], scope=claim["scope"],
                            slot_id=slot if isinstance(slot, int) else None,
                            name=claim["name"])
    return sender, answer.get("state")


def _send(args, ctx: Context, *, tags, reply_to, channel_room, referent=None,
          author=None) -> int:
    project = project_key(env=ctx.env)
    try:
        # A claim socket outside comms' runtime directory is asked nothing.
        claim_path = claimhold.socket_from_env(ctx.env)
    except OutsideRuntime as e:
        _refuse(ctx, "outside-runtime", f"{e} Nothing was sent.")
        return EXIT_REFUSED
    if claim_path:
        if (ctx.env.get("MOXI_COMMS_OPERATOR") or "").strip():
            _refuse(ctx, "operator-in-session",
                    "comms cannot tell who this process is: it carries the operator's "
                    "own stamp inside an agent's session — nothing was sent. The "
                    "operator's lane is its own terminal.")
            return EXIT_REFUSED
        got = _claim_sender(ctx, claim_path)
        if isinstance(got, int):
            return got
        sender, state = got
        return _send_resolved(args, ctx, sender=sender, operator_id=None, tags=tags,
                              reply_to=reply_to, channel_room=channel_room,
                              project=project, referent=referent, claim_state=state,
                              author=author)
    try:
        operator_id = operator_from_env(ctx.env)
    except OperatorRefused:
        # The operator's stamp and an agent's claim in one environment. Neither is
        # preferred: a claimed session is never the operator, and a verb that picked one
        # of the two would be inventing the answer to "who is this". The sentence names
        # no variable and no seat -- both are in this process's log, where `detail` went.
        _refuse(ctx, "operator-and-claim",
                "comms cannot tell who this process is: it carries the operator's own "
                "stamp and an agent's claim at the same time — nothing was sent. A "
                "claimed session is never the operator, and comms will not choose "
                "between them on your behalf. The operator's lane is its own terminal. "
                "(Both stamps are named in this process's comms log.)")
        return EXIT_REFUSED
    if operator_id is not None and _inside_a_session():
        # The operator's lane is a terminal outside every session: a process inside a
        # live session is refused it, so an agent process that strips its stamp still
        # cannot post as the operator.
        _refuse(ctx, "operator-in-session",
                "comms cannot tell who this process is: it carries the operator's own "
                "stamp inside an agent's session — nothing was sent. The operator's "
                "lane is its own terminal.")
        return EXIT_REFUSED
    if operator_id is not None:
        # The operator is not an agent and borrows nothing: no bundle, no seat, no name
        # on the wire (the record reads the display off the operators row). Its identity
        # is the global `operator`; no scope of anyone's decides what a name means.
        sender = SenderIdentity(qualified=OPERATOR, scope=GLOBAL)
        return _send_resolved(args, ctx, sender=sender, operator_id=operator_id, tags=tags,
                              reply_to=reply_to, channel_room=channel_room, project=project,
                              referent=referent, author=author)
    # The env path is closed: only a live session holding a name sends under it (the
    # operator's ruling), so a process with no claim socket, outside the operator's
    # lane, sends under no identity. A stamp is no live session
    # holding a name. `sender_from_env` is read only to NAME, in the refusal, the
    # identity the stamp claims; nothing is ever sent as it.
    stamped = _stamped_identity(ctx.env, project)
    if stamped is None:
        # No stamp and no socket: nothing to say on the wire. The cure is the launcher's:
        # a session gets its claim socket from its comms adapter, and nothing an agent
        # exports gives it one.
        _refuse(ctx, "no-identity",
                "comms has no identity for this process: nothing was sent. A session "
                "sends as its identity through its own claim socket, which its comms "
                "adapter gives it at launch, and this process has none. Launching is "
                "the operator's; ask them.")
        return EXIT_REFUSED
    if any(claimfile.names_identity(r, stamped) for _k, r in claimfile.live_records()):
        # A live session holds the name (as a claim, a waiting launch claim included),
        # and this process is not inside it: say so.
        _refuse(ctx, "not-in-session",
                f"this process is not inside the session that holds {stamped} — "
                "nothing was sent. A session sends as its identity through its own claim "
                "socket.")
        return EXIT_REFUSED
    _refuse(ctx, "no-claim-socket",
            f"this process has no comms claim socket, so it sends under no identity — "
            f"nothing was sent. Its launch stamp names {stamped}, but a stamp is not a "
            "live session holding a name: a session sends as its identity through its own "
            "claim socket.")
    return EXIT_REFUSED


def _stamped_identity(env, project) -> str | None:
    """The qualified identity a launch stamp claims, for a refusal to name, or None when
    there is no stamp. A malformed stamp still names its identity (`sender_from_env`
    logs the malformed value); it is refused like any other stamp."""
    try:
        sender = sender_from_env(env, project)
    except MalformedStamp:
        name = env.get("MOXI_COMMS_NAME")
        scope = GLOBAL if env.get("MOXI_COMMS_GLOBAL") else (env.get("MOXI_COMMS_SCOPE") or GLOBAL)
        return qualify(name, scope)
    return sender.qualified if sender is not None else None


def _send_resolved(args, ctx: Context, *, sender, operator_id, tags, reply_to,
                   channel_room, project, referent=None, claim_state=None,
                   author=None) -> int:
    """Everything a send does once it knows WHO is sending: the checks, the machine
    line, the length cap, the post and the nudge.

    Two lanes reach here and they differ in exactly one statement each -- the credential
    resolved and the door posted through. `operator_id` is None for an agent and the
    operator's user id for the operator's own terminal. Everything between is shared on
    purpose: the operator types the same machine line, obeys the same
    `max_content_length`, prints the same `sent <id>` line and
    nudges the daemon the same way. A lane with its own copy of that would drift.

    Every refusal is made by `_send_checks`, in its one order, before anything is
    posted; this function composes, posts and prints what the checks planned. Two
    refusals follow the checks here, because they need the composed bodies and the
    credential: a message too long to send at all (`_bodies`) and a credential that will
    not resolve. `--check` runs all of this and stops right before the post
    (`_say_would_send`), so it refuses wherever send would, in send's words.

    `author` is `moxi comms reply`'s: the referent's author, from which `_reply_targets` takes
    the recipients now that the sender is known, and says the "replying to" line. A reply
    is never held to the never-bound check: its names come from the record.
    """
    typed_names = author is None
    if not typed_names:
        got = _reply_targets(ctx, sender, author, referent)
        if isinstance(got, int):
            return got
        tags, line = got
        ctx.say(line)
    code, plan = _send_checks(args, ctx, sender=sender, operator_id=operator_id, tags=tags,
                              reply_to=reply_to, project=project, referent=referent,
                              claim_state=claim_state, typed_names=typed_names)
    if plan is None:
        return code
    _keep(ctx, plan=plan, operator_lane=operator_id is not None)

    # The correlation token goes INTO the machine line: a post carries no reply field,
    # so the record is the only place a reply's referent can live,
    # and it lives there self-describingly. The marks go as keywords, never read off
    # `args`.
    bodies = _bodies(args, ctx, plan.resolved, reply_to, plan.audience,
                     next_turn=plan.flagged, cold_at=plan.cold_at, posture=plan.posture,
                     **plan.marks)
    if bodies is None:
        return EXIT_REFUSED             # over the cap; _bodies said why
    cred = token = channel_id = None
    try:
        with ctx.db() as db:
            if operator_id is None:
                cred, _channel = credential_for(ctx.gateway, db, sender, room=channel_room)
            else:
                # The operator's lane needs a credential like every other lane:
                # the holder's own token out of the store, read the declared way.
                token, channel_id = operator_credential(db, room=channel_room, env=ctx.env)
    except (SendFailed, GatewayError) as e:
        # a credential that will not resolve is a send that did NOT happen: same exit
        # code, same loudness. "No credential" must never read as "nothing to say".
        _refuse(ctx, "credential", str(e))
        return EXIT_FAILED
    if getattr(args, "check", False):
        # `--check` stops here, at the post: every refusal above is send's own, said in
        # send's words with send's exit code, and nothing below has run. No post, no
        # nudge, no record.
        _say_would_send(ctx, plan, len(bodies))
        return EXIT_OK
    (body,) = bodies
    try:
        msg = (post(ctx.gateway, cred, body, username=sender.qualified)
               if operator_id is None else
               post_as_operator(ctx.gateway, operator_id, channel_id, body, token=token))
    except (SendFailed, GatewayError) as e:
        _refuse(ctx, "post-failed", str(e))
        return EXIT_FAILED
    _keep(ctx, message_id=msg.id)
    ctx.say(f"sent {msg.id} to {', '.join(plan.resolved)} in {channel_room}")
    _nudge_daemon(ctx)
    held, tiers = plan.held, plan.tiers
    if held:
        # Posted, and queued by the daemon for each cold one's next release point: one
        # line each, right after the `sent` line, which scripts read for the id.
        for state in held:
            ctx.say(cache.hold_line(state))
    # What an open thread's sender learns from its own send: none of these opens with a
    # caller's line marker or carries an id.
    for line in plan.sent_lines:
        ctx.say(line)
    _say_notes(ctx, plan)
    if held:
        # The outcome line, last: `tail -1` says the message was held, and when
        # each held recipient reads it.
        ctx.say(_held_last_line(held, tiers=tiers, operator=operator_id is not None))
    return EXIT_OK


def _say_would_send(ctx: Context, plan, count: int) -> None:
    """What a passing `--check` prints: `would send <N> message(s) to <names>`, then the
    `held:` line the post would print for each recipient the daemon would hold, then
    every NOTE the post would print, in the post's order. Not the post's outcome lines
    ("Sent. Open thread …", "Sent and held …"): nothing was sent."""
    ctx.say(f"{WOULD_SEND} {count} message(s) to {', '.join(plan.resolved)}")
    if plan.held:
        for state in plan.held:
            ctx.say(cache.hold_line(state))
    _say_notes(ctx, plan)


def _say_notes(ctx: Context, plan) -> None:
    """Every NOTE a send prints, from its plan: one per recipient not bound, the `--held`
    NOTE per bound recipient, then the checks' own. The post and `--check` both print
    them through here, so a check says what the send would; each was planned by
    `_send_checks` before anything was posted."""
    unbound = {q for q, _tier in plan.deferred}
    for qualified, _tier in plan.deferred:
        ctx.note(NOT_BOUND_NOTE.format(name=qualified))
    if plan.flagged:
        for qualified in dict.fromkeys(plan.resolved):
            if qualified in unbound or qualified == OPERATOR:
                continue
            if plan.tiers.get(qualified) == TIER_EPHEMERAL:
                # An `agent-N` holds mail only while bound: name only its own session's
                # turns, and say what drops it.
                ctx.note(HELD_NOTE_SESSION_ONLY.format(name=qualified) + " "
                         + _session_only_sentence([qualified]))
                continue
            ctx.note(HELD_NOTE.format(name=qualified))
    held = {state.target for state in plan.held}
    if not plan.flagged:
        for qualified in dict.fromkeys(plan.resolved):
            if qualified in plan.waiting and qualified not in held:
                ctx.note(_waiting_note(qualified, plan.waiting[qualified]))
    for note in plan.notes + plan.ask_notes:
        ctx.note(note)


def _waiting_note(qualified: str, seconds) -> str:
    """The NOTE for a bound recipient with no endpoint, with how long it has had none."""
    age = (cache.spoken_span(seconds) if isinstance(seconds, (int, float))
           else "a while")
    return WAITING_NOTE.format(name=qualified, age=age)


#: The first words of a passing `send --check`'s first line.
WOULD_SEND = "would send"


@dataclass
class SendPlan:
    """What `_send_checks` hands the post when nothing refused: who it goes to, what
    line zero carries, and what the send prints beside its `sent` lines."""

    resolved: list
    deferred: list
    tiers: dict
    held: list               # the cold recipients' states, on a send that is not flagged
    audience: str | None
    posture: str | None
    flagged: bool            # line zero carries `[held]` (`--held`, to a non-operator)
    cold_at: tuple | None    # line zero's `[cold-at:…]` values, None on a flagged send
    marks: dict
    sent_lines: list
    notes: list
    ask_notes: list
    #: Bound recipients whose session has no turns endpoint, each with how long it has
    #: had none (seconds, None when the daemon did not say).
    waiting: dict = field(default_factory=dict)


def _send_checks(args, ctx: Context, *, sender, operator_id, tags, reply_to, project,
                 referent=None, claim_state=None, typed_names=True):
    """Every check a send makes before anything is posted, in one order: `(exit code,
    None)` once a refusal is said, or `(None, SendPlan)`.

      1. the flag checks, before the daemon: the `--reply-to` digits, the audience, the
         posture requirement, `--new` with `--reply-to`, the `--cap` shape,
         the config file's `block`;
      2. the daemon block: resolution (with, on a `moxi comms send`, the never-bound check),
         and the `session` query for each bound recipient;
      3. the single-reply refusal;
      4. the reply checks: read the referent, then a reply to `[no reply]`, `--posture
         open` on a reply to a once ask, the open-thread
         check and the rest of the `--cap` refusals;
      5. the cache reads: refused for a read comms cannot make, held for a cold one,
         and the send's cold-at for each recipient;
      6. the audience table, on the referent read in step 4.

    A posture refusal never applies to the exempt sender (`_exempt`): the operator.
    """
    # ---- 1. the flag checks ---------------------------------------------------------
    if reply_to is not None and not str(reply_to).isdigit():
        # The token IS the machine line: a non-digit id would fail line zero's own
        # grammar, and the post would land in the record as prose -- addressed to
        # nobody, routed nowhere, with nothing said about it. Refuse at the verb.
        _refuse(ctx, "reply-to-shape",
                f"--reply-to {reply_to!r} is not a message id (message ids are "
                "digits). The reply token rides the machine line, so a malformed id "
                "would make the whole line prose and the message would reach no one. "
                "Nothing was sent.")
        return EXIT_REFUSED, None
    typed_audience = getattr(args, "audience", None)
    audience = machine_line.normalize_audience(typed_audience)
    if typed_audience is not None and audience is None:
        # Same reason as the id above: the mark rides line zero, and a value outside the
        # tag grammar would make the whole line prose.
        _refuse(ctx, "audience-shape",
                f"--audience {typed_audience!r} is not a name comms can carry (letters, "
                "digits, `.`, `_`, `-`, and one optional `@scope`). The mark rides the "
                "machine line, so a malformed one would make the whole line prose and "
                "the message would reach no one. Nothing was sent.")
        return EXIT_REFUSED, None
    typed = getattr(args, "posture", None)
    cap_text = getattr(args, "cap", None)
    no_cap = bool(getattr(args, "no_cap", False))
    exempt = _exempt(sender)
    new_send = reply_to is None
    if new_send and typed is None and not exempt:
        _refuse(ctx, "posture-required", POSTURE_REQUIRED)
        return EXIT_REFUSED, None
    if getattr(args, "new", False) and reply_to is not None:
        _refuse(ctx, "new-with-reply-to",
                "--new says this message is not a reply, and --reply-to makes it one. "
                "Nothing was sent. Drop --new to send the reply, or drop --reply-to to "
                "send a new message.")
        return EXIT_REFUSED, None
    cap = None
    if cap_text is not None and no_cap:
        _refuse(ctx, "cap-both", CAP_BOTH)
        return EXIT_REFUSED, None
    if cap_text is not None:
        text = str(cap_text).strip()
        if not text.isdigit() or int(text) < 1:
            _refuse(ctx, "cap-shape", CAP_SHAPE)
            return EXIT_REFUSED, None
        cap = int(text)
    if _blocked(ctx):
        # Advisory only: enforcement is at daemon routing. Refusing here is a
        # courtesy to the operator, never the mechanism.
        _refuse(ctx, "blocked",
                "block is on in comms' config — not sending. (Advisory check: "
                "enforcement is at daemon routing, which is symmetric.)")
        return EXIT_BLOCKED, None

    # ---- 2. the daemon block --------------------------------------------------------
    try:
        client = ctx.daemon()
    except VerbProtocolMismatch as e:
        _refuse(ctx, "protocol-mismatch", str(e))
        return EXIT_OFFLINE, None
    except DaemonOffline as e:
        _refuse(ctx, "offline",
                f"{e}\nThe daemon performs tier/route resolution; without it the verb "
                "cannot tell an ephemeral typo from a chosen name.")
        return EXIT_OFFLINE, None
    # The cache read: every sender, every bound recipient of every adapter, `--held`
    # included: an unknown read refuses a flagged send too. The `session` query
    # runs HERE, on the connection the resolve loop already holds, before the `finally`
    # below closes it; its answer carries the daemon's own verdict, and the age and
    # lifetime the send's cold-at is computed from.
    held_typed = bool(getattr(args, "held", False))
    targets = []            # a cache.Target for each bound recipient, with its verdict
    t_ask = {}              # qualified -> the verb's clock just before it read the name
    tiers = {}              # qualified -> its tier, from the resolve answer
    # The single-reply check: a plain send only -- no `--reply-to`
    # (which `reply` always has), no `--new` -- and never from an exempt sender.
    check_asks = reply_to is None and not getattr(args, "new", False) and not exempt
    nominated, ask_notes = [], []
    try:
        resolved, deferred = [], []
        for tag in tags:
            try:
                r = client.query("resolve", name=tag)
            except DaemonError as e:
                _refuse(ctx, "resolve-refused", _refused_tag(tag, e))
                return EXIT_REFUSED, None
            if r.get("send_only"):
                # The whole post, before anything is posted: a send-only identity
                # receives nothing, bound or not, and a reply to one of its messages
                # resolves to it and is refused the same way.
                _refuse(ctx, "send-only", send_only_refusal(r["qualified"]))

                return EXIT_REFUSED, None
            if getattr(args, "standing", False) and not r.get("standing_row"):
                # The sender DECLARED a standing recipient. Nothing in the record or the
                # tag distinguishes an undeclared standing name from a chosen one, so the
                # declaration is the only place the honest error can live -- and it lives
                # at the verb, never in the machine line.
                _refuse(ctx, "not-standing",
                        f"{tag}: declared standing, but {r['qualified']!r} has no standing "
                        "row in the directory — it would be treated as a chosen name "
                        f"(resolved tier: {r['tier']}"
                        + (", currently bound" if r["bound"] else ", unbound")
                        + "). Nothing was sent. Drop --standing to send it as a chosen "
                        "name, or have the provisioner declare the row first.")
                return EXIT_REFUSED, None
            if (typed_names and r["deferred"] and r.get("tier") != TIER_STANDING
                    and r["qualified"] != OPERATOR
                    and r.get("ever_bound") is False
                    and not getattr(args, "new_name", False)):
                # A name the custody journal has never seen bound, typed on a `moxi comms
                # send`: most likely a typo, and its mail would wait for a session that
                # never starts. `--new-name` says the agent has not started yet. A daemon
                # that cannot read the journal answers no `ever_bound`, and the send goes
                # on as before. The operator is always known, standing row or not: a box
                # provisioned by default writes none, and sending to it keeps working.
                _refuse(ctx, "never-bound", _never_bound_refusal(tag, r))
                return EXIT_REFUSED, None
            resolved.append(r["qualified"])
            tiers[r["qualified"]] = r.get("tier")
            if r["deferred"]:
                deferred.append((r["qualified"], r["tier"]))
            if r["qualified"] not in t_ask:
                # `t_ask` is no later than the daemon's read, so the cold-at computed
                # from it is never later than the read's last turn plus its lifetime.
                t_ask[r["qualified"]] = ctx.clock()
                if r["bound"]:
                    targets.append(cache.session_target(client, r["qualified"]))
        if claim_state == claimfile.BOUND:
            pass             # the claim carries the slot the daemon bound it on
        elif claim_state == claimfile.OFFLINE:
            # The adapter holds the claim but is not connected. A binding of the name
            # the daemon lists belongs to another connection; none, or a lookup that
            # fails, and the post goes out as the claim on its own slot.
            try:
                listed = client.query("agents").get("bindings", [])
            except (DaemonError, DaemonOffline):
                listed = []
            if any(b.get("qualified") == sender.qualified for b in listed):
                _refuse(ctx, "bound-elsewhere",
                        f"{sender.qualified} is bound by another session; this one does "
                        "not hold it — nothing was sent.")
                return EXIT_REFUSED, None
        # No other agent sender reaches here: with the env path closed, every agent
        # sends as its claim, so the per-send binding lookup a stamped sender needed
        # is gone. The operator spends no bundle.
        if check_asks:
            # The daemon NOMINATES; the verb decides below, on the record.
            nominated, note = _nominated_asks(client, sender, resolved)
            if note:
                ask_notes.append(note)
    finally:
        client.close()

    # ---- 3. the single-reply refusal ------------------------------------------------
    if nominated:
        # Right after the daemon block and AHEAD of the cache reads: each nominated ask,
        # and the replies after it, read back from the record and judged by `open_for`.
        # Only a predicate read true refuses; a read that fails lets the send through
        # with a NOTE.
        open_asks, read_notes = _open_asks(ctx, sender, nominated, resolved)
        ask_notes += read_notes
        if open_asks:
            _refuse(ctx, "single-reply",
                    _ask_refusal(open_asks, tags, resolved, now=ctx.clock(), args=args))
            return EXIT_REFUSED, None

    # ---- 4. the reply checks --------------------------------------------------------
    notes = []
    marks = {"open_mark": False, "cap": None, "no_cap": False, "thread_mark": None}
    sent_lines = []
    posture = typed
    if new_send:
        open_mark = typed == machine_line.POSTURE_OPEN
        if (cap is not None or no_cap) and not open_mark:
            _refuse(ctx, "cap-no-thread", CAP_NO_THREAD)
            return EXIT_REFUSED, None
        if open_mark:
            marks.update(open_mark=True, cap=cap, no_cap=no_cap)
            sent_lines.append(_open_line(cap, no_cap, ctx))
    else:
        if referent is None:
            # `reply` hands the original in; `send --reply-to` reads it here, before
            # any posture check, through the gateway this verb already holds.
            try:
                referent = ctx.gateway.get_message(str(reply_to))
            except (DaemonOffline, GatewayError) as e:
                # Posting as typed on a failed read would silently drop an audience the
                # referent may carry. A referent that is simply NOT in the record is a
                # different fact, and it posts as typed below.
                _refuse(ctx, "referent-unreadable",
                        f"{e}\nThe message this answers ({reply_to}) could not be read, "
                        "so comms cannot copy its audience onto the reply. Nothing was "
                        "sent.")
                return (EXIT_OFFLINE if isinstance(e, DaemonOffline) else EXIT_FAILED), None
        names_text = ",".join(tags)
        got = _reply_checks(args, ctx, sender=sender, exempt=exempt, typed=typed,
                            cap=cap, no_cap=no_cap, referent=referent,
                            reply_to=str(reply_to), names_text=names_text,
                            resolved=resolved)
        if isinstance(got, int):
            return got, None
        posture, reply_marks, reply_lines, reply_notes = got
        marks.update(reply_marks)
        sent_lines += reply_lines
        notes += reply_notes

    # ---- 5. the cache reads ---------------------------------------------------------
    # A read comms cannot make refuses the WHOLE post, exit 6, nothing posted:
    # an `unknown` verdict, a name that unbound while comms checked it, a failed query.
    # The sender sends again in a moment. A `cold` read posts and the message waits in
    # the queue (every adapter); a passing read delivers. `--held` reads like any
    # send, and its recipients wait as flagged whatever the read.

    states = [cache.kind_of(target) for target in targets]
    refused = [state for state in states
               if state.kind not in cache.PASSING and state.kind != cache.COLD]
    if refused:
        _refuse(ctx, "cache-unread", _hold_text(refused,
                           others=len(set(resolved)) > len({s.target for s in refused})))
        return EXIT_COLD, None
    # `[held]` only when a recipient other than the operator takes it.
    flagged = held_typed and any(q != OPERATOR for q in resolved)
    if held_typed and OPERATOR in resolved:
        notes.append(HELD_IGNORED_OPERATOR)
    held = [] if flagged else [state for state in states if state.kind == cache.COLD]
    # The send's cold-at, which the daemon reads when its own read is unknown: on every
    # line zero without `[held]`, never beside it. One value per tag, in line zero's tag order.
    read = {state.target: state for state in states}
    cold_at = None if flagged else tuple(_cold_at(read.get(q), t_ask[q])
                                         for q in resolved)

    # ---- 6. the audience table -------------------------------------------------------
    if reply_to is not None:
        audience, refusal = _reply_audience(referent, audience)
        if refusal:
            _refuse(ctx, "audience-differs", refusal)
            return EXIT_REFUSED, None
    waiting = {t.label: t.endpoint_for for t in targets if t.endpoint is False}
    return None, SendPlan(resolved=resolved, deferred=deferred, tiers=tiers, held=held,
                          audience=audience, posture=posture, flagged=flagged,
                          cold_at=cold_at, marks=marks, sent_lines=sent_lines,
                          notes=notes, ask_notes=ask_notes, waiting=waiting)


#: A `moxi comms send` to an identity comms has never seen bound, without `--new-name`.
NEVER_BOUND = ("{tag}: comms has never seen {name} bound, so no agent of that name has "
               "started. If it has not started yet, send it again with --new-name and the "
               "message waits for its first session; if it has, check the name (moxi comms "
               "agents lists who you can message).{seen} Nothing was sent.")
#: Added to `NEVER_BOUND` when the journal has seen the same name bound in other scopes.
NEVER_BOUND_SEEN = " {name} was seen bound only as {seen}; address {that} if it is who you mean."


def _never_bound_refusal(tag: str, r: dict) -> str:
    """`NEVER_BOUND` for one resolve answer, naming the identities of the same name the
    journal has seen bound elsewhere (`seen_as`), when the daemon lists any."""
    name = r["qualified"]
    seen_as = [q for q in (r.get("seen_as") or ()) if q != name]
    seen = ""
    if seen_as:
        that = (f"that identity as {seen_as[0]}" if len(seen_as) == 1 else
                "that identity by its full name")
        seen = NEVER_BOUND_SEEN.format(name=name, seen=_and(seen_as), that=that)
    return NEVER_BOUND.format(tag=tag, name=name, seen=seen)


def _blocked(ctx: Context) -> bool:
    """The config file's `block`, read at this use (`read = "use"`) from this process's
    config, the one the daemon enforces it from."""
    return config.load().block


def _exempt(sender) -> bool:
    """Is this sender exempt from every posture refusal? The operator, by the name every
    lane of his carries, and no one else."""
    return sender.qualified == OPERATOR


def _cold_at(state, t_ask: float):
    """One recipient's cold-at value: the send's reading of its cache,
    as delivery acts on it.

    A `warm` or `cold` read with both an age and a lifetime: `floor(t_ask - age +
    ttl)`, when the cache goes cold (already past for `cold`). A `fresh` or `no-cache`
    read: `-`, deliver live. A "wait" reading with no lifetime: `floor(t_ask)`, a time
    already reached; that is a recipient not bound (`state` None) and a `cold` with no
    lifetime. A `warm` read with no lifetime is counted the same way: nothing says how
    long it stays warm.
    """
    if state is not None and state.kind in (cache.FRESH, cache.NO_CACHE):
        return machine_line.COLD_AT_LIVE
    if (state is not None and state.kind in (cache.WARM, cache.COLD)
            and state.age_seconds is not None and state.ttl_seconds is not None):
        return max(0, math.floor(t_ask - state.age_seconds + state.ttl_seconds))
    return max(0, math.floor(t_ask))


def _configured_cap(ctx: Context) -> int:
    """The default reply cap: `[threads] reply_cap` in comms' config file, read at each
    check, or its default with no table. A config that will not load raises."""
    return config.load(env=ctx.env).threads.reply_cap



def _open_line(cap, no_cap: bool, ctx: Context) -> str:
    """What an open thread's opener is told after its `sent` line: the cap and how to
    change it."""
    if no_cap:
        return OPEN_SENT_NO_CAP
    if cap is None:
        try:
            cap = _configured_cap(ctx)
        except Exception:  # noqa: BLE001 -- the line informs; the check reads it again
            cap = constants.DEFAULT_REPLY_CAP
    return OPEN_SENT.format(n=cap, replies="reply" if cap == 1 else "replies")


# ------------------------------------------------------------------ the posture texts

#: A new send with no `--posture`, from a sender that is not exempt.
POSTURE_REQUIRED = ("Every new moxi comms send states what it wants back with --posture: none "
                    "for no reply, optional for a reply only if you have something to "
                    "add, once for a single answer, or open for a back-and-forth. Nothing "
                    "was sent.")
#: A reply to a verified `[no reply]`.
NO_REPLY_REFUSED = ("msg {id} from {author} is marked [no reply], so it takes no reply. If "
                    "you have something new, send it as a new message: moxi comms send "
                    '{names}{new} --posture none|optional|once|open "text". Nothing was '
                    "sent.")
#: `--posture open` on a reply to a once ask or an optional message: `{what}` is the
#: referent's own words (`ONCE_ASK`, `OPTIONAL_MESSAGE`).
ONCE_NOT_OPEN = ("A reply to a {what} closes it: leave out --posture, or add "
                 "--posture once to ask something back. A back-and-forth starts with a new "
                 'send: moxi comms send {names}{new} --posture open "text". Nothing was sent.')
ONCE_ASK, OPTIONAL_MESSAGE = "[reply-once] ask", "[reply-optional] message"
#: A reply in an open thread that is closed: by its cap, by idle, by a none reply.
CLOSED_BY_CAP = ("msg {root} opened an open thread that has had its {cap} replies, so it "
                 "is closed. To go on, start a new send: moxi comms send {names}{new} --posture "
                 'none|optional|once|open "text". Nothing was sent.')
CLOSED_BY_IDLE = ("msg {root} opened an open thread that has had no message for over an "
                  "hour, so it is closed. To go on, start a new send: moxi comms send "
                  '{names}{new} --posture none|optional|once|open "text". Nothing was '
                  "sent.")
CLOSED_BY_NONE = ("msg {root} opened an open thread that {closer} closed with msg {id}. To "
                  "go on, start a new send: moxi comms send {names}{new} --posture "
                  'none|optional|once|open "text". Nothing was sent.')
#: The `--cap` and `--no-cap` refusals.
CAP_BOTH = "--cap and --no-cap say opposite things. Pass one of them. Nothing was sent."
CAP_SHAPE = "--cap takes a whole number of replies, 1 or more. Nothing was sent."
CAP_NO_THREAD = ("--cap and --no-cap set the reply limit of an open thread, and this "
                 "message does not open one or reply in one. Nothing was sent.")
CAP_CLOSED = ("This thread is closed, so its reply limit can't change. Send it again "
              "without --cap or --no-cap. Nothing was sent.")
CAP_NOT_OPENER = ("Only {opener}, who opened this thread, can change its reply limit. Send "
                  "it again without --cap or --no-cap. Nothing was sent.")
CAP_BELOW = "This is reply {k} in its thread, so --cap must be {k} or more. Nothing was sent."
#: What the sender of a message in an open thread is told after its `sent` line.
OPEN_SENT = ("Sent. Open thread, closes after {n} {replies}; change it with --cap N or "
             "--no-cap on any reply of yours in this thread.")
OPEN_SENT_NO_CAP = ("Sent. Open thread, no reply cap; change it with --cap N on any reply "
                    "of yours in this thread.")
ONE_LEFT_SENT = "Sent. One reply left in this thread."
LAST_SENT = "Sent. That was this thread's last reply; it is now closed."
#: The open-thread check could not run; the reply goes through.
THREAD_NOTE_MISSING = ("NOTE: the open-thread check did not run (msg {id} it answers is not "
                       "in the record), so it did not hold this reply.")
THREAD_NOTE_LONG = ("NOTE: the open-thread check did not run (the thread is longer than "
                    "comms reads back), so it did not hold this reply.")
THREAD_NOTE_ERROR = ("NOTE: the open-thread check did not run ({error}), so it did not "
                     "hold this reply.")
IDLE_NOTE = "NOTE: the idle check did not run ({error}), so it did not hold this reply."


# ------------------------------------------------------------------ the reply posture

#: What a reply's referent is, for the posture table: a verified `[no reply]`; in an
#: open thread that is not closed; a verified `[reply-once]` or `[reply-optional]`
#: someone else wrote, in an open thread that is closed; anything else in a closed open
#: thread; a `[reply-once]` or `[reply-optional]` in no open thread; anything else.
REF_NO_REPLY, REF_IN_THREAD, REF_ONCE_CLOSED, REF_CLOSED, REF_ONCE, REF_OTHER = (
    "no-reply", "in-thread", "once-closed", "closed", "once", "other")

#: What `_send_posture` answers in place of a posture when the table refuses.
REFUSE_NO_REPLY, REFUSE_ONCE_OPEN, REFUSE_CLOSED = "refuse-no-reply", "refuse-once-open", \
    "refuse-closed"


def _send_posture(typed, kind: str, *, exempt: bool = False) -> str | None:
    """The posture a reply carries, or a refusal tag: the reply table, top to bottom.

    `typed` is the `--posture` the sender passed, or None. `kind` is the referent's row
    (`REF_*`). An exempt sender is never refused: what it passes is written as passed,
    and with nothing passed a reply to a verified `[no reply]` goes out `[no reply]`, a
    reply the closed row would refuse goes out open, and every other reply takes the
    "nothing passed" column. None is the open posture with nothing in the slot.
    """
    open_, none, once = (machine_line.POSTURE_OPEN, machine_line.POSTURE_NONE,
                         machine_line.POSTURE_ONCE)
    if exempt:
        if typed is not None:
            return typed
        if kind in (REF_NO_REPLY, REF_ONCE_CLOSED, REF_ONCE):
            return none
        return open_
    if kind == REF_NO_REPLY:
        return REFUSE_NO_REPLY
    if kind == REF_CLOSED:
        return REFUSE_CLOSED
    if kind in (REF_ONCE_CLOSED, REF_ONCE):
        if typed == open_:
            return REFUSE_ONCE_OPEN
        return typed or none
    return typed or open_


@dataclass
class ThreadRead:
    """The open-thread check's read of the record for one reply."""

    open: bool = False           # the referent sits below a stated open root
    root: object = None
    opener: str | None = None
    count: int = 0               # verified members
    cap: int | None = None       # the cap in force before this reply; None for no cap
    closed: str | None = None    # "cap", "idle" or "none"
    closed_cap: int | None = None
    closer: tuple | None = None  # (author, message id) of the none close
    note: str | None = None      # the check did not run: why
    idle_note: str | None = None  # the idle clause did not run: why


def _reply_checks(args, ctx: Context, *, sender, exempt, typed, cap, no_cap,
                  referent, reply_to, names_text, resolved):
    """Step 4 of `_send_checks` for a reply: an exit code once a refusal is said, or
    `(posture, marks, sent lines, notes)`."""
    ml = machine_line.parse(referent.content) if referent is not None else None
    author = None
    verified = False
    if referent is not None:
        with ctx.db() as db:
            author = attribution.from_record(db, referent)
        verified = author.attribution == attribution.VERIFIED

    def new_flag() -> str:
        return _new_flag(ctx, sender, resolved)

    # A reply to `[no reply]`, before anything else is read.
    if (ml is not None and verified and ml.posture == machine_line.POSTURE_NONE
            and not exempt):
        _refuse(ctx, "no-reply",
                NO_REPLY_REFUSED.format(id=referent.id, author=author.qualified,
                                        names=names_text, new=new_flag()))
        return EXIT_REFUSED

    notes = []
    thread = ThreadRead()
    if referent is None:
        thread.note = THREAD_NOTE_MISSING.format(id=reply_to)
    else:
        thread = _read_thread(ctx, referent, sender)
    if thread.note:
        notes.append(thread.note)
    elif thread.idle_note:
        notes.append(thread.idle_note)

    # A `[reply-optional]` referent takes every row a `[reply-once]` one takes.
    once_like = ml is not None and ml.posture in (machine_line.POSTURE_ONCE,
                                                  machine_line.POSTURE_OPTIONAL)
    once_by_other = once_like and verified and author.qualified != sender.qualified
    if ml is not None and verified and ml.posture == machine_line.POSTURE_NONE:
        kind = REF_NO_REPLY
    elif thread.open and not thread.closed:
        kind = REF_IN_THREAD
    elif thread.open and once_by_other:
        kind = REF_ONCE_CLOSED
    elif thread.open:
        kind = REF_CLOSED
    elif once_like:
        kind = REF_ONCE
    else:
        kind = REF_OTHER
    posture = _send_posture(typed, kind, exempt=exempt)
    if posture == REFUSE_ONCE_OPEN:
        what = (OPTIONAL_MESSAGE if ml.posture == machine_line.POSTURE_OPTIONAL
                else ONCE_ASK)
        _refuse(ctx, "once-not-open",
                ONCE_NOT_OPEN.format(what=what, names=names_text, new=new_flag()))
        return EXIT_REFUSED
    if posture == REFUSE_CLOSED:
        root = thread.root.id
        if thread.closed == "cap":
            text = CLOSED_BY_CAP.format(root=root, cap=thread.closed_cap, names=names_text,
                                        new=new_flag())
        elif thread.closed == "idle":
            text = CLOSED_BY_IDLE.format(root=root, names=names_text, new=new_flag())
        else:
            closer, closed_by = thread.closer
            text = CLOSED_BY_NONE.format(root=root, closer=closer, id=closed_by,
                                         names=names_text, new=new_flag())
        _refuse(ctx, "thread-closed", text)
        return EXIT_REFUSED

    marks, lines = {}, []
    k = thread.count + 1
    if cap is not None or no_cap:
        if thread.note:
            # The check did not run: the flag is written as passed, and binds only if
            # its sender opened the thread.
            marks.update(cap=cap, no_cap=no_cap)
        elif not thread.open:
            _refuse(ctx, "cap-no-thread", CAP_NO_THREAD)
            return EXIT_REFUSED
        elif thread.closed:
            _refuse(ctx, "cap-closed", CAP_CLOSED)
            return EXIT_REFUSED
        elif sender.qualified != thread.opener:
            _refuse(ctx, "cap-not-opener", CAP_NOT_OPENER.format(opener=thread.opener))
            return EXIT_REFUSED
        elif cap is not None and cap < k:
            _refuse(ctx, "cap-below", CAP_BELOW.format(k=k))
            return EXIT_REFUSED
        else:
            marks.update(cap=cap, no_cap=no_cap)
            lines.append(_open_line(cap, no_cap, ctx))
    if thread.open and not thread.closed and not thread.note:
        in_force = None if no_cap else (cap if cap is not None else thread.cap)
        if in_force is not None:
            if k >= in_force:
                marks["thread_mark"] = machine_line.THREAD_LAST
                lines.append(LAST_SENT)
            elif k == in_force - 1 and posture != machine_line.POSTURE_NONE:
                marks["thread_mark"] = machine_line.THREAD_ONE_LEFT
                lines.append(ONE_LEFT_SENT)
    return posture, marks, lines, notes


def _read_thread(ctx: Context, referent, sender) -> ThreadRead:
    """The open-thread check's read for a reply to `referent`.

    Walk `re:` up to the root; a root that is a verified `#names` message
    carrying `[open]` makes an open thread, and any other root makes none, with no NOTE.
    Then the members, in one read after the root; the verified ones count. The cap in
    force is the newest cap token on a verified message by the opener, else the
    configured default. Closed by the cap, then by idle, then by a none reply, the first
    that holds. A read that fails, or may have been cut short, leaves the check not run
    and says why.
    """
    from .daemon import _epoch
    thread = ThreadRead()
    try:
        with ctx.db() as db:
            room = directory.room_by_name(db, NAMES)
            names_channel = room["channel_id"] if room else None
            cur = referent
            hops = 0
            while True:
                cml = machine_line.parse(cur.content)
                if cml is None or cml.reply_to is None:
                    break
                hops += 1
                if hops > constants.THREAD_WALK_LIMIT:
                    thread.note = THREAD_NOTE_LONG
                    return thread
                parent = ctx.gateway.get_message(cml.reply_to)
                if parent is None:
                    thread.note = THREAD_NOTE_MISSING.format(id=cml.reply_to)
                    return thread
                cur = parent
            root = cur
            rml = machine_line.parse(root.content)
            if (rml is None or not rml.open_mark or names_channel is None
                    or root.channel_id != names_channel):
                return thread
            root_author = attribution.from_record(db, root)
            if root_author.attribution != attribution.VERIFIED:
                return thread
            after = ctx.gateway.history(names_channel, after=root.id)
            bound = getattr(ctx.gateway, "history_bound", constants.RESCAN_LIMIT)
            if bound is not None and len(after) >= bound:
                thread.note = THREAD_NOTE_LONG
                return thread
            members, ids = [], {root.id}
            for m in sorted(after, key=lambda x: int(x.id)):
                mml = machine_line.parse(m.content)
                if mml is None or mml.reply_to not in ids:
                    continue
                ids.add(m.id)
                who = attribution.from_record(db, m)
                if who.attribution == attribution.VERIFIED:
                    members.append((m, mml, who))
        thread.open, thread.root = True, root
        thread.opener = root_author.qualified
        thread.count = len(members)
        cap = None
        token_seen = False
        for m, mml, who in reversed([(root, rml, root_author)] + members):
            if who.qualified == thread.opener and (mml.cap is not None or mml.no_cap):
                cap, token_seen = (None if mml.no_cap else mml.cap), True
                break
        if not token_seen:
            cap = _configured_cap(ctx)
        thread.cap = cap
        # 1. the cap
        for n, (m, mml, who) in enumerate(members, 1):
            if mml.thread_mark == machine_line.THREAD_LAST:
                thread.closed, thread.closed_cap = "cap", n
                return thread
        if cap is not None and thread.count >= cap:
            thread.closed, thread.closed_cap = "cap", cap
            return thread
        # 2. idle
        items = [(root, root_author)] + [(m, who) for m, _mml, who in members]
        idle, why = _thread_idle(ctx, items, sender, _epoch)
        if why is not None:
            thread.idle_note = IDLE_NOTE.format(error=why)
        elif idle:
            thread.closed = "idle"
            return thread
        # 3. closed by none
        for m, mml, who in members:
            if mml.posture == machine_line.POSTURE_NONE:
                thread.closed, thread.closer = "none", (who.qualified, m.id)
                return thread
    except Exception as e:  # noqa: BLE001 -- every failure here only relaxes
        return ThreadRead(note=THREAD_NOTE_ERROR.format(error=e))
    return thread


def _thread_idle(ctx: Context, items, sender, epoch) -> tuple[bool, str | None]:
    """(is the thread idle for this replier, or why the idle clause did not run).

    Each message's readable time: its post time if the replier wrote it; else when the
    daemon handed it to the replier; else none yet, if the daemon still holds it or has
    it pending for the replier (then the thread is not idle); else its post time, if it
    was posted since the daemon started; else when the daemon started."""
    theirs = [m for m, who in items if who.qualified != sender.qualified]
    answer = {"since": 0.0, "handed": {}, "waiting": []}
    if theirs:
        try:
            client = ctx.daemon()
            try:
                answer = client.query("readable", identity=sender.qualified,
                                      ids=[m.id for m in theirs])
            finally:
                client.close()
        except Exception as e:  # noqa: BLE001 -- a failed read lets the reply through
            return False, str(e)
    since = float(answer.get("since") or 0.0)
    handed = answer.get("handed") or {}
    waiting = set(answer.get("waiting") or ())
    times = []
    for m, who in items:
        posted_at = epoch(m.timestamp)
        if who.qualified == sender.qualified:
            times.append(posted_at)
        elif m.id in handed:
            times.append(float(handed[m.id]))
        elif m.id in waiting:
            return False, None
        elif posted_at >= since:
            times.append(posted_at)
        else:
            times.append(since)
    return ctx.clock() - max(times) >= constants.THREAD_IDLE, None


def _new_flag(ctx: Context, sender, resolved) -> str:
    """` --new` when a plain send from this sender to `resolved` would meet the
    single-reply refusal, so a cure that names a new send is one the rules accept; empty
    otherwise, and whenever that check's reads fail."""
    try:
        client = ctx.daemon()
        try:
            nominated, note = _nominated_asks(client, sender, resolved)
        finally:
            client.close()
        if note or not nominated:
            return ""
        open_asks, _notes = _open_asks(ctx, sender, nominated, resolved)
    except Exception:  # noqa: BLE001 -- a failed read lets the plain send through
        return ""
    return " --new" if open_asks else ""


# ------------------------------------------------------------------ the single-reply check

#: The NOTE a send carries when the single-reply check did not run. Each one
#: lets the send through.
ASK_CHECK_NO_ANSWER = "NOTE: the single-reply check did not run (the daemon did not answer)."


def _nominated_asks(client, sender, resolved) -> tuple[list[str], str | None]:
    """(the ask ids the daemon nominates, or a NOTE saying the check did not run).

    `DaemonOffline` cannot reach here in the ordinary way -- the verb exits 4 before
    resolution when the daemon is down -- but a daemon that closes the connection in
    between is a daemon that did not answer, and it lets the send through like one.
    """
    try:
        answer = client.query("asks", sender=sender.qualified, recipients=list(resolved))
    except (DaemonTimeout, DaemonOffline):
        return [], ASK_CHECK_NO_ANSWER
    except DaemonError as e:
        return [], f"NOTE: the single-reply check did not run (the daemon said: {e})."
    return [str(i) for i in answer.get("asks") or ()], None


def _open_asks(ctx: Context, sender, nominated, resolved) -> tuple[list, list[str]]:
    """([(ask, asker)] for each nominated ask `open_for` holds on, and NOTEs).

    Each ask is read with `get_message`, and the replies after it with `history`, through
    the gateway the verb already holds. ANY error there -- `GatewayError`,
    `DaemonOffline`, the local backend's own `sqlite3.Error`, or anything else the check
    cannot finish -- treats that ask as not open and says so; if the gateway is truly
    broken the post itself fails, with exit 5.
    """
    now = ctx.clock()
    found, notes = [], []
    try:
        db_context = ctx.db()
        with db_context as db:
            room = directory.room_by_name(db, NAMES)
            names_channel = room["channel_id"] if room else None
            for ask_id in nominated:
                try:
                    ask = ctx.gateway.get_message(ask_id)
                    asker = open_for(db, ask, sender, now=now, names_channel=names_channel,
                                     replies=lambda a=ask_id: ctx.gateway.history(
                                         names_channel, after=a))
                except Exception as e:  # noqa: BLE001 -- every failure here only relaxes
                    notes.append(f"NOTE: the single-reply check could not read msg {ask_id} "
                                 f"from the record ({e}), so it did not hold this send.")
                    continue
                # The daemon nominated it for this send's recipients; the verb keeps that
                # fact its own too, so a stale nomination can only let the send through.
                if asker is not None and asker.qualified in resolved:
                    found.append((ask, asker))
    except Exception as e:  # noqa: BLE001 -- every failure here only relaxes
        found = []
        for ask_id in nominated:
            notes.append(f"NOTE: the single-reply check could not read msg {ask_id} "
                         f"from the record ({e}), so it did not hold this send.")
    return found, notes


def open_for(db, ask, sender, *, now: float, names_channel, replies):
    """The asker, when `ask` binds `sender` to a single reply that is still owed; else
    None. Every clause must hold:

      1. `ask` is a `#names` message whose line zero parses with posture `once`;
      2. `from_record` says `verified`, and the asker is not the sender;
      3. some tag on its line zero names the sender (`_tag_matches`, by the one rule,
         with no daemon call);
      4. its timestamp is within `ASK_TTL` of `now`;
      5. no message after it, attributed by `from_record` to the sender, carries
         `re:<ask id>` and addresses the asker.

    `replies` is called only once clauses 1 to 4 hold: it reads the record after the
    ask. Whatever it raises is the caller's to handle, as a read that failed.
    """
    from .daemon import _epoch
    if ask is None or names_channel is None or ask.channel_id != names_channel:
        return None
    ml = machine_line.parse(ask.content)
    if ml is None or ml.posture != machine_line.POSTURE_ONCE:
        return None                                              # 1
    asker = attribution.from_record(db, ask)
    if asker.attribution != attribution.VERIFIED or asker.qualified == sender.qualified:
        return None                                              # 2
    if not any(_tag_matches(t, sender.qualified) for t in ml.recipients):
        return None                                              # 3
    if now - _epoch(ask.timestamp) > constants.ASK_TTL:
        return None                                              # 4
    for m in replies():                                          # 5
        if m.channel_id != names_channel or int(m.id) <= int(ask.id):
            continue
        rml = machine_line.parse(m.content)
        if rml is None or rml.reply_to != ask.id:
            continue
        if attribution.from_record(db, m).qualified != sender.qualified:
            continue
        if any(_tag_matches(t, asker.qualified) for t in rml.recipients):
            return None
    return asker


def _cure_flags(args) -> str:
    """The flags the sender typed that the reply also takes, as they go on the cure line:
    `--held` (every recipient takes it), `--standing`, and `--posture` when it was none,
    optional or once. A reply to a once ask refuses `--posture open`, so it is not
    carried.
    `--audience` is left out: a reply takes the ask's."""
    if args is None:
        return ""
    flags = ["--held"] if getattr(args, "held", False) else []
    if getattr(args, "standing", False):
        flags.append("--standing")
    typed = getattr(args, "posture", None)
    if typed in (machine_line.POSTURE_NONE, machine_line.POSTURE_OPTIONAL,
                 machine_line.POSTURE_ONCE):
        flags.append(f"--posture {typed}")
    return "".join(f" {flag}" for flag in flags)


def _ask_refusal(open_asks, tags, resolved, *, now: float, args=None) -> str:
    """What a refused plain send prints: who asked, when, and both cures."""
    from .daemon import _epoch
    open_asks = sorted(open_asks, key=lambda pair: int(pair[0].id), reverse=True)
    newest = open_asks[0][0].id
    cure = (f'moxi comms reply {newest} "text"' if len(resolved) == 1 else
            f'moxi comms send {",".join(tags)} --reply-to {newest} "text"') + _cure_flags(args)
    tail = ("If it is something else, add --new and it goes as a new message. Nothing "
            "was sent.")
    if len(open_asks) == 1:
        ask, asker = open_asks[0]
        return "\n".join([
            f"{asker.qualified} asked you for a single reply in msg {ask.id} "
            f"({_ago(now - _epoch(ask.timestamp))}), and it is still open. If this "
            "message is that reply, send it with",
            f"  {cure}",
            f"so it threads and closes the ask. {tail}"])
    lines = [f"{len(open_asks)} single-reply asks to you are still open from the "
             "recipients of this message:"]
    lines += [f"  msg {ask.id} from {asker.qualified} "
              f"({_ago(now - _epoch(ask.timestamp))})" for ask, asker in open_asks[:3]]
    if len(open_asks) > 3:
        lines.append(f"  (and {len(open_asks) - 3} more)")
    lines += ["If this message is the reply to one of them, send it threaded to that one, "
              "as in",
              f"  {cure}",
              f"so it closes that ask. {tail}"]
    return "\n".join(lines)


def _names_of(held) -> str:
    """`a's`, `a's and b's`, `a's, b's and c's`."""
    return _and([f"{state.target}'s" for state in held])


def _and(words) -> str:
    """`a`, `a and b`, `a, b and c`."""
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _refusal_reason(state) -> str:
    """Why one recipient refuses the post, as it goes on the refusal's first line: comms
    could not read its cache, it unbound mid-check, or the `session` query failed."""
    q = state.target
    if state.reason == cache.NOT_BOUND:
        return f"{q} unbound while comms was checking it"
    if state.adapter is None:
        return (f"comms could not ask the daemon about {q} "
                f"({state.reason or 'no reason given'})")
    return f"comms could not read {q}'s cache ({state.reason or 'no reason given'})"


def _hold_text(refused, *, others: bool = False) -> str:
    """The refusal for reads comms cannot make: exit 6, nothing posted.
    It opens with "Nothing was sent:" and each refused recipient's reason, says how to
    reach the others when the post had others, tells the sender to send it again, and
    closes with "Nothing was sent." -- so the first line and the last both say it."""
    refused = list(refused)
    lines = ["Nothing was sent: " + "; ".join(map(_refusal_reason, refused)) + "."]
    if others:
        lines.append("To reach the other recipients now, send it again without "
                     f"{_and(state.target for state in refused)}.")
    lines.append("Send it again in a moment; a read that succeeds holds or delivers it.")
    lines.append("Nothing was sent.")
    return "\n".join(lines)


def _session_only_sentence(names) -> str:
    """What drops an `agent-N`'s held mail: every way its
    binding ends."""
    if len(names) == 1:
        return (f"{names[0]} is a session-only name: if the session ends, its comms "
                "connection drops or the comms daemon restarts first, the message is "
                "dropped.")
    return (f"{_and(names)} are session-only names: if one's session ends, its comms "
            "connection drops or the comms daemon restarts first, the message is dropped "
            "for it.")


def _held_last_line(held, *, tiers: dict, operator: bool = False) -> str:
    """A held send's last line: for each held recipient, only the releases that exist
    for its tier. A chosen or standing name reads it after its own next turn, or when
    its session is cleared, compacted or restarted; an `agent-N` only in its current
    session, and its mail is dropped when its binding ends. One clause per kind
    present."""
    names = [state.target for state in held]
    session_only = [q for q in names if tiers.get(q) == TIER_EPHEMERAL]
    lasting = [q for q in names if q not in session_only]
    clauses = []
    if lasting:
        clauses.append(
            f"{lasting[0]} reads this after its own next turn, or when its session is "
            "cleared, compacted or restarted."
            if len(lasting) == 1 else
            f"{_and(lasting)} read this after their own next turns, or when their "
            "sessions are cleared, compacted or restarted.")
    if session_only:
        clauses.append(
            f"{session_only[0]} reads this after its own next turn in this session (when "
            "the operator types into it, or its first turn after a /clear)."
            if len(session_only) == 1 else
            f"{_and(session_only)} read this after their own next turns in their current "
            "sessions (when the operator types into them, or their first turns after a "
            "/clear).")
        clauses.append(_session_only_sentence(session_only))
    one = len(names) == 1
    line = "Sent and held: " + " ".join(clauses) + (
        f" Nothing wakes {'it' if one else 'them'} for this message alone.")
    if operator:
        line += (f" To reach {'it' if one else 'them'} now, type into "
                 f"{'its session' if one else 'their sessions'} yourself.")
    return line


def _reply_audience(referent, passed: str | None) -> tuple[str | None, str | None]:
    """(the audience the reply carries, or a refusal).

    "The original carries" means the original's LINE ZERO. An audience the daemon filled
    in at delivery is not on line zero, so it is not copied here; the daemon's own index
    carries that chain. A referent that is not in the record, or whose line zero is
    prose, carries nothing, and the reply posts as typed.
    """
    ml = machine_line.parse(referent.content) if referent is not None else None
    original = ml.audience if ml is not None else None
    if original is None or passed is None or passed == original:
        return (original or passed), None
    return None, (
        f"message {referent.id} is for audience {original!r}, and a reply cannot change "
        f"its audience to {passed!r}: the audience belongs to the exchange, which the ask "
        f"set. Nothing was sent. Drop --audience (the reply carries {original!r} on its "
        "own), or, if this is for someone else, send it as a new message rather than a "
        "reply.")


def _nudge_daemon(ctx: Context) -> None:
    """Tell the daemon a post is in the record.

    On the local backend the CLI writes the record itself, so nothing else tells the
    daemon the row is there: its pump would find it at the next `poll_interval`. One
    fire-and-forget frame on the socket the daemon already owns cuts that wait to the
    width of a unix send. Local only, by the seam and not by the gateway object: the
    mock fans out in-process, so it has no nudge to take.

    No answer is read and none is expected. Two misses are accepted and NOT coded
    around: an old daemon answers `unknown op` to a frame nobody waits for, and a CLI
    whose XDG_RUNTIME_DIR is unset names the STATE fallback socket, where the nudge
    lands nowhere and delivery waits one poll interval.

    The operator's lane posts through `post_as_user` and needs this line too. It
    lands here by the same route an agent's post does: `_send_resolved` nudges on
    whatever reached the record, whichever door it went through, so the operator's typed
    message is delivered at the width of a unix send and not at the next poll.
    """
    if ctx.backend_name() != LOCAL:
        return
    try:
        client = ctx.daemon()
    except DaemonOffline:
        return                  # no daemon, the poll will find it
    try:
        client.notify("nudge")
    finally:
        client.close()


def _bodies(args, ctx: Context, resolved, reply_to, audience=None, *,
            next_turn: bool = False, cold_at=None, posture: str | None = None,
            open_mark: bool = False, cap: int | None = None, no_cap: bool = False,
            thread_mark: str | None = None) -> list[str] | None:
    """The postable body for one `send`, as a one-item list, or None when it is over
    the cap and refused; the refusal is printed here.

    The gateway seam declares `max_content_length`, the one cap on one message
    (`gateway.MAX_CONTENT_LENGTH`). Comms does not split: over the cap, nothing is sent.

    Only `args.text` is read off `args`. `[held]` (`next_turn`), the cold-at, `posture`,
    `[open]`, the cap tokens and the thread mark are keywords the verb decided on its
    checks; None is the open posture with nothing in the slot, exactly as `format_line`
    has it.
    """
    line = machine_line.format_line(resolved, reply_to=reply_to, audience=audience,
                                    next_turn=next_turn, cold_at=cold_at, posture=posture,
                                    open_mark=open_mark, cap=cap, no_cap=no_cap,
                                    thread_mark=thread_mark)
    body = f"{line}\n{args.text}"
    limit = ctx.gateway.max_content_length
    if len(body) <= limit:
        return [body]
    _refuse(ctx, "too-long",
            f"that message is {len(body)} characters with its routing line, over the "
            f"{limit}-character cap on one message. Nothing was sent. Put the content "
            "somewhere it can live (a file, a note, a paste) and send a pointer to it "
            "instead.")
    return None


def _ago(seconds) -> str:
    """How long ago, in the units a person reads at a terminal."""
    if seconds is None:
        return "never"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


#: Moved to `scoping.tag_matches`; the old name stays for the callers here.
_tag_matches = tag_matches


def _refused_tag(tag: str, error) -> str:
    """A resolution refusal as the verb prints it: the daemon's sentence, under the tag
    it was for. A sentence that already opens with the tag (a retired identity's) is
    printed as it came, so the tag is not said twice."""
    text = str(error)
    return text if text.startswith(f"{tag} ") else f"{tag}: {text}"



# --------------------------------------------------------------------------- the run

def run(ctx: Context, args, *, tags=None) -> SendResult:
    """Run one send and hand back what it did. Nothing is printed: what the verb says is
    in the result's `lines()`.

    `args` carries the verb's fields by their flag names: `text`, `posture`, `cap`,
    `no_cap`, `audience`, `new`, `held`, `standing`, `new_name`, `check`, and either
    `reply_to` (with `tags`, the names) or `message_id` (the reply verb: the names come
    from the message it answers). The sender is the one `ctx.env` describes."""
    capture, outer = _Capture(), ctx.capture
    ctx.capture = capture
    try:
        try:
            if getattr(args, "message_id", None) is not None:
                code = _reply(args, ctx)
            else:
                code = _send(args, ctx, tags=list(tags or ()),
                             reply_to=getattr(args, "reply_to", None), channel_room=NAMES)
        except config.ConfigError as e:
            # A config file that does not load refuses the send, naming the key and the
            # fix.
            ctx.refuse("config", str(e))
            code = EXIT_REFUSED
    finally:
        ctx.capture = outer
    return _result(code, capture, check=bool(getattr(args, "check", False)))


def _result(code: int, capture: _Capture, *, check: bool) -> SendResult:
    said, notes = tuple(capture.said), tuple(capture.notes)
    if code != EXIT_OK:
        outcome = _BY_EXIT.get(code, OUTCOME_FAILED)
        refusal = capture.refusal or Refusal(outcome, "\n".join(map(str, said)))
        return SendResult(outcome=outcome, refusal=refusal, notes=notes, said=said)
    recipients = (_recipients(capture.plan) if capture.plan is not None else ())
    if check:
        return SendResult(outcome=OUTCOME_WOULD_SEND, recipients=recipients, notes=notes,
                          said=said)
    message_id = None if capture.message_id is None else str(capture.message_id)
    return SendResult(outcome=OUTCOME_SENT, message_id=message_id, recipients=recipients,
                      notes=notes, said=said)


def _recipients(plan) -> tuple:
    """One `Recipient` per resolved name, in line zero's order, from the plan the checks
    made: the plan is what the post printed its lines from, so the data and the text say
    the same.

    `waiting` is a bound recipient whose session has no turns endpoint, as the daemon
    reports it for a binding whose adapter says so (a Claude session's); a held recipient
    reads held, since its mail waits for a release point either way."""
    unbound = {q for q, _tier in plan.deferred}
    held = {state.target: state for state in plan.held}
    cold_at = {}
    for qualified, value in zip(plan.resolved, plan.cold_at or ()):
        cold_at.setdefault(qualified, value)
    out = []
    for qualified in dict.fromkeys(plan.resolved):
        if qualified == OPERATOR:
            delivery, reason = DELIVERY_OPERATOR, None
        elif qualified in unbound:
            delivery, reason = DELIVERY_UNBOUND, NOT_BOUND_NOTE.format(name=qualified)
        elif plan.flagged:
            delivery = DELIVERY_HELD_FLAG
            reason = (HELD_NOTE_SESSION_ONLY.format(name=qualified) + " "
                      + _session_only_sentence([qualified])
                      if plan.tiers.get(qualified) == TIER_EPHEMERAL
                      else HELD_NOTE.format(name=qualified))
        elif qualified in held:
            delivery, reason = DELIVERY_HELD_COLD, cache.hold_line(held[qualified])
        elif qualified in plan.waiting:
            delivery, reason = DELIVERY_WAITING, _waiting_note(qualified,
                                                               plan.waiting[qualified])
        else:
            delivery, reason = DELIVERY_LIVE, None
        at = cold_at.get(qualified)
        at = None if at is None or at == machine_line.COLD_AT_LIVE else int(at)
        out.append(Recipient(qualified, delivery, at, reason))
    return tuple(out)
