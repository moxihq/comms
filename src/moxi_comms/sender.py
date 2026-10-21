"""The send path: resolve a credential, post the machine line + text, retry, then LOUD.

A 429 is retried honoring Retry-After, bounded (finite attempts, then stop).
Exhaustion is a loud failure at the verb -- the one place a message could actually be
lost, so it never fails quietly.

Every post suppresses mentions. Logs carry ids only -- never bodies, never resolved
webhook URLs.

THE OPERATOR'S LANE lives here too. It is a second lane, not a second identity scheme:
`MOXI_COMMS_OPERATOR=<user_id>` is the trigger, a claimed session is never the operator, and
the post presents the `operator` holder's own token exactly as an agent's post presents
its bundle's. `post_as_user` is what it reaches, not `execute_webhook`, and that is the
only difference below the verb.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import constants, directory, tokens
from .gateway import NO_MENTIONS, RateLimited
from .provision import NAMES
from .scoping import GLOBAL, qualify

log = logging.getLogger("comms.send")

#: The operator lane's trigger. A stamp the launcher never passes to a
#: session: it is typed in the operator's own terminal and nowhere else.
OPERATOR_VAR = "MOXI_COMMS_OPERATOR"

#: The stamps that say a session is CLAIMED. Either one beside the trigger is a refusal.
CLAIM_VARS = ("MOXI_COMMS_NAME", "MOXI_COMMS_SLOT")


class SendFailed(Exception):
    """Loud. The message was NOT posted."""


class OperatorRefused(Exception):
    """The operator's stamp and an agent's claim in one environment.

    Letting `MOXI_COMMS_NAME=operator` in any shell resolve to `post_as_user` would reopen
    the fabricated-identity class. The lane needs a credential AND a claimed session is
    ineligible for it: a shell holding both stamps is a shell whose identity nobody can
    name, so neither is preferred and the send is refused.

    Raised rather than answered with `None` for the same reason `MalformedStamp` is: the
    verb has to tell "there is no operator here" from "there are two senders here" and
    say the true thing about each. `detail` is machinery-facing (it names the variables);
    the agent-facing refusal is the CLI's, and it carries no seat vocabulary.
    """

    def __init__(self, message: str, *, detail: str):
        super().__init__(message)
        self.detail = detail


class MalformedStamp(Exception):
    """The launch stamp exists and is BROKEN -- which is not the same as absent.

    Raised rather than answered with `None` so the verb can tell the two apart and say
    the true thing about each. `detail` is machinery-facing (it names the variable and
    the value); the agent-facing refusal is the CLI's, and it carries no seat vocabulary.
    """

    def __init__(self, message: str, *, detail: str):
        super().__init__(message)
        self.detail = detail


@dataclass
class SenderIdentity:
    """Who the verb posts as: a slot's bundle, or a standing identity's own webhook."""
    qualified: str
    scope: str
    slot_id: int | None = None
    name: str | None = None

    @classmethod
    def for_slot(cls, slot_id: int, qualified: str, scope: str,
                 name: str | None = None) -> "SenderIdentity":
        return cls(qualified=qualified, scope=scope, slot_id=int(slot_id), name=name)

    @classmethod
    def for_agent(cls, name: str, scope: str) -> "SenderIdentity":
        return cls(qualified=qualify(name, scope), scope=scope, name=name)


def credential_for(gateway, db, sender: SenderIdentity, room: str = NAMES):
    """A STANDING identity posts from its own bundle -- that is what makes its messages
    verifiable by `webhook_id -> row` forever, including in replay. Everyone else posts
    from the slot bundle they claimed, and their replayed attribution is display-derived.

    The credential follows the identity and nothing else: it is always the sender's own
    bundle, or the slot the claim assigned. The wire's `username` is the sender's
    qualified name, because that display IS what attribution reads back off a slot-owned
    webhook -- so the credential spent and the name worn can never come apart.

    A missing credential is reported to the SENDER, which is an agent, so it names the
    identity and the room and stops there: which bundle was searched, and which seat it
    belonged to, is provisioner inventory. Whether the claim carried a slot or not makes
    no difference to what the agent can do about it -- the cure is the operator either
    way -- so both misses say the same thing.

    The credential's OWNER goes down with the pointer. It follows the
    ROW that was actually resolved, not the identity's shape: a chosen-tier sender carries
    both a name and a slot, and it spends the SLOT bundle, so the owner has to be decided
    in the same branch that picks the row or the two could come apart. The owner is what
    lets the secrets chain find that bundle's own token in the store; it is a store key
    and nothing else -- no token material passes through this module in either direction.
    """
    room_row = directory.room_by_name(db, room)
    if room_row is None:
        raise SendFailed(f"{room} is not provisioned")
    channel_id = room_row["channel_id"]
    slot_id = sender.slot_id
    row, owner = None, None
    if sender.name:
        row = directory.agent_webhook(db, sender.name, sender.scope, channel_id)
        if row is not None:
            owner = tokens.owner_for_standing(sender.name, sender.scope)
    if row is None and slot_id is not None:
        row = directory.slot_webhook(db, slot_id, channel_id)
        owner = tokens.owner_for_slot(slot_id)
    if row is None:
        raise SendFailed(f"no {room} posting credential is provisioned for "
                         f"{sender.qualified} — nothing was sent. Provisioning is the "
                         "operator's; ask them.")
    return gateway.resolve_secret(row["bws_secret_id"], owner=owner), channel_id


def operator_from_env(env) -> str | None:
    """The operator's user id when this shell IS the operator's lane, else None.

    The terminal is the operator's lane, and `MOXI_COMMS_OPERATOR=<user_id>` is how a
    terminal says so. The launcher never passes it to a session, so nothing an agent
    runs inherits it.

    A CLAIMED session is never the operator. `MOXI_COMMS_NAME` or `MOXI_COMMS_SLOT` beside the
    trigger raises `OperatorRefused`, with the variables in the LOG and in `detail` and
    neither in the sentence an agent reads (agent-facing surfaces carry no seat
    vocabulary).
    """
    user_id = (env.get(OPERATOR_VAR) or "").strip()
    if not user_id:
        return None
    claimed = [var for var in CLAIM_VARS if env.get(var)]
    if claimed:
        log.warning("%s is set beside %s; a claimed session is never the operator and the "
                    "verb refuses rather than pick one of them",
                    OPERATOR_VAR, ", ".join(claimed))
        raise OperatorRefused(
            "this process carries the operator's stamp and an agent's claim at once",
            detail=f"{OPERATOR_VAR} is set beside {', '.join(claimed)}")
    return user_id


def operator_credential(db, room: str = NAMES, *, store=None, env=None):
    """The operator's lane resolved the way an agent's is: the room, then the credential.

    Returns `(token, channel_id)`, the mirror of what `credential_for` returns, so the
    verb's two lanes read the same below this line.

    The token comes out of the `operator` holder's file through
    `TokenStore.read_secret`, THE DECLARED READ; `moxi-comms-provision` wrote it there on the
    local backend in the run that stored its hash in the record. It goes to the one
    caller that presents it and nowhere else -- no token material is logged, returned
    twice, or kept.
    """
    room_row = directory.room_by_name(db, room)
    if room_row is None:
        raise SendFailed(f"{room} is not provisioned")
    holder = tokens.owner_for_role(tokens.ROLE_OPERATOR)
    st = store if store is not None else tokens.TokenStore(env=env)
    try:
        token = st.read_secret(holder, tokens.OPERATOR_SECRET_ID)
    except tokens.TokenStoreError as e:
        # The store's own refusals are already caller-shaped (0600 is the whole
        # mechanism); pass the sentence through rather than write a second one.
        raise SendFailed(str(e)) from None
    if not token:
        raise SendFailed(
            "the operator's posting credential is not on this box — nothing was sent. "
            "Provisioning writes it into the operator's own store file; run "
            "moxi-comms-provision, or ask the operator.")
    return token, room_row["channel_id"]


def post(gateway, cred, content: str, *, username: str,
         attempts: int = constants.RETRY_ATTEMPTS, sleep=time.sleep):
    """Post with bounded 429 retry honouring Retry-After. Exhaustion raises, loudly.

    `content` already carries the whole machine line, `re:` correlation token included:
    a reply is not a transport option here, it is a line the record keeps.
    """
    return _bounded(lambda: gateway.execute_webhook(cred, content, username=username,
                                                    allowed_mentions=NO_MENTIONS),
                    as_=username, attempts=attempts, sleep=sleep)


def post_as_operator(gateway, user_id: str, channel_id: str, content: str, *, token: str,
                     attempts: int = constants.RETRY_ATTEMPTS, sleep=time.sleep):
    """The operator's post: the same message, a different door, and a credential.

    `content` is whatever the verb composed, machine line and all -- the operator types
    the same line every other sender does, and the verb's length cap applies to this lane
    by the same seam fact (`max_content_length`), because the cap is a property of a
    MESSAGE and not of the webhook route.

    The token rides down as a keyword and the backend decides what it is worth: the local
    gateway compares it against `meta('operator_token_hash')`, while the mock accepts
    and ignores it so the seam is one shape on both. The bounded
    retry is the same policy here as on the webhook path, so the two share one loop.
    """
    return _bounded(lambda: gateway.post_as_user(user_id, channel_id, content, token=token),
                    as_=f"operator {user_id}", attempts=attempts, sleep=sleep)


def _bounded(send, *, as_: str, attempts: int, sleep):
    """The bounded retry, once, for both lanes: the transport states the fact, the verb
    owns the
    policy, and exhaustion is LOUD in the same words whichever door was used."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            msg = send()
            log.info("posted message_id=%s channel=%s as=%s attempt=%d",
                     msg.id, msg.channel_id, as_, attempt)
            return msg
        except RateLimited as e:
            last = e
            log.warning("429 on attempt %d/%d; retry_after=%ss", attempt, attempts,
                        e.retry_after)
            if attempt >= attempts:
                break
            sleep(min(e.retry_after, constants.RETRY_MAX_SLEEP))
    raise SendFailed(
        f"SEND FAILED: rate limited on all {attempts} attempts "
        f"(last Retry-After {getattr(last, 'retry_after', '?')}s). The message was NOT "
        "posted and is NOT in the record. Nothing will retry it for you.")


def sender_from_env(env, project: str) -> SenderIdentity | None:
    """The identity a launch stamp claims. **Nothing is sent as it.** A send reads its
    identity from the session's claim socket (`cli._claim_sender`); with no socket the
    send verb reads this only to name, in its refusal, the identity the stamp claims,
    because only a live session holding a name sends under it. `moxi comms cache` reads it to
    gate its caller when there is no claim socket.

    The harness stamps the claim into the environment at claim time.

    `MOXI_COMMS_NAME` (with `MOXI_COMMS_SCOPE` / `MOXI_COMMS_GLOBAL`) is the claim; `MOXI_COMMS_SLOT` only
    ever says which bundle to spend. **No name, no identity**: identity comes from the
    claim-stamped environment, full stop, and a process with no claim is refused at the
    verb. A slot alone used to synthesize `slot-3` and wear it on the wire, a fabricated
    identity: `slot-3` names a seat, not an agent, so nothing a recipient reads it
    against can be true, and agent-facing surfaces carry no slot vocabulary at all.

    **Three answers, not two.** A non-numeric `MOXI_COMMS_SLOT` is a
    BROKEN STAMP, and a broken stamp is neither a claim nor the absence of one:

      * no `MOXI_COMMS_NAME` -> `None`. There is no claim; the verb refuses with "no identity".
      * a valid stamp   -> a `SenderIdentity`.
      * a name with a malformed value beside it -> `MalformedStamp`. Answering `None`
        here would make the verb say "no identity" to a
        process that plainly has one -- false, and it would point the reader at the wrong
        repair. The two OTHER candidates were both refused: falling back to name-only
        would have the verb quietly repair a stamp only the launcher may write, and a
        `ValueError` traceback is not a refusal at all. So: a third refusal, naming the
        defect that is actually there. The launcher owns the stamp; relaunching is the
        cure; and nothing about the message the agent typed was wrong.
    """
    name = env.get("MOXI_COMMS_NAME")
    if not name:
        return None
    # A stamp with no `MOXI_COMMS_SCOPE` is the global identity: `project` is the session's
    # project, never its scope.
    scope = GLOBAL if env.get("MOXI_COMMS_GLOBAL") else (env.get("MOXI_COMMS_SCOPE") or GLOBAL)
    slot = env.get("MOXI_COMMS_SLOT")
    if slot:
        try:
            slot_id = int(slot)
        except (TypeError, ValueError):
            # The value goes in the LOG and in `detail`, both machinery-facing. It does
            # not go in the agent's refusal: seats are provisioner inventory and
            # agent-facing surfaces carry no slot vocabulary.
            log.warning("MOXI_COMMS_SLOT=%r is not a slot number; the launch stamp for %s is "
                        "malformed and the verb refuses rather than repair it", slot, name)
            raise MalformedStamp(
                f"the launch stamp for {qualify(name, scope)} is malformed",
                detail=f"MOXI_COMMS_SLOT={slot!r} is not a slot number") from None
        return SenderIdentity.for_slot(slot_id, qualify(name, scope), scope, name=name)
    return SenderIdentity.for_agent(name, scope)
