"""Provisioning: rooms, slot webhook bundles, standing rows.

The shape:
  * one shared room agents post in, `#names`. No inbox rooms.
  * a slot is a webhook bundle: exactly one `#names` webhook, and nothing else.
  * a standing identity gets the same bundle under its own (name, scope) -- that is
    what makes `webhook_id -> row` verification possible for it.
  * the daemon holds no writing credential.

ON THE LOCAL BACKEND, provision is also the token WRITER. One token per ROW: the
gateway mints it, hands back the plaintext once and keeps only its SHA-256, and this
module writes it into the holder's store file under the row's own `secret_id` in the same
call that created the row -- slot bundles to `owner_for_slot`, standing rows to their
owner, and the operator's lane to the `operator` holder. That is what makes `moxi-comms-provision` against
`local` run UNATTENDED: no portal, no pasting, no operator beyond running the verb.

The `store=` argument is how this module knows, and nothing else is. It is a caller's
decision because only the caller knows which backend it opened: the mock keeps the
token in its own record, and there is no token here to keep. `main()` builds the store
when the selected backend is `local`; a caller that passes none is a caller whose
backend stores its own. Asking the SELECTION seam instead was the other candidate
and it is wrong here: a gateway handed in directly (the rig, the smokes) is not the
backend the box's config names, and provision must write for the gateway it was given.

A store file lost after a run is not recovered by re-running: the record keeps only the
hash, so the row would have to be re-minted under a new webhook id and every replayed
attribution against the old one would go with it. Re-provisioning CONVERGES, it does not
rotate -- which is the same promise the directory rows have always made.

What a first start makes in an empty directory is config.toml's `[provision]`: `slots`
and `operator`, read when the step runs.
"""

from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import directory
from .config import ProvisionConfig
from .scoping import GLOBAL, is_auto_name

NAMES = "#names"
#: The rooms provision creates: `#names` alone. A slot or standing bundle is one
#: `#names` webhook.
ROOM_KINDS = {NAMES: "names"}


class ProvisionError(Exception):
    pass


@dataclass
class ProvisionResult:
    rooms: dict = field(default_factory=dict)          # room name -> channel_id
    slots: list = field(default_factory=list)          # slot ids
    standing: list = field(default_factory=list)       # (name, scope)
    operator_user_id: str = ""
    operator_token_written: bool = False   # local backend only; False means "already had one"


def _keep(store, owner, secret_id: str, cred) -> None:
    """One minted token into its holder's store file, under the ROW's own `secret_id`.

    This is the same call that created the row, so the
    plaintext the gateway handed back once never travels further than here. The write is
    `TokenStore.write_secret`, THE DECLARED WRITE.
    `store` is None on every backend that mints elsewhere, and then there is nothing to
    keep -- see the module docstring for why the caller decides that and not this module.
    """
    if store is None:
        return
    store.write_secret(owner, secret_id, cred.token)


def _operator_token(gateway, store) -> bool:
    """The operator's lane gets a credential like every other lane.

    The token rests in the `operator` holder's store file and the record keeps only its
    SHA-256 in `meta('operator_token_hash')`, exactly as a webhook row keeps only a hash.
    Answers whether it minted.

    IDEMPOTENT BY THE PAIR AGREEING: a run that finds a stored token hashing to the
    stored hash mints nothing, so re-provisioning never rotates the operator out of their
    own lane. A run that finds one half without the other mints, because half a pair is a
    lane nobody can use and there is nothing to lose by replacing it.
    """
    from . import tokens
    from .local_gateway import META_OPERATOR_TOKEN_HASH, token_hash

    owner = tokens.owner_for_role(tokens.ROLE_OPERATOR)
    held = store.read_secret(owner, tokens.OPERATOR_SECRET_ID)
    stored = gateway.meta_get(META_OPERATOR_TOKEN_HASH)
    if held and stored and _secrets.compare_digest(stored, token_hash(held)):
        return False
    token = _secrets.token_urlsafe(32)
    store.write_secret(owner, tokens.OPERATOR_SECRET_ID, token)
    gateway.meta_set(META_OPERATOR_TOKEN_HASH, token_hash(token))
    return True


def provision(gateway, db_path, *, slots: int = 3, standing=(), operator=("operator", "operator"),
              writer: str = "provisioner", daemon_client=None, store=None,
              send_only=()) -> ProvisionResult:
    """Idempotent. `standing` and `send_only` are iterables of (name, scope); a
    `send_only` pair is a standing row whose kind is `send-only`.

    `store` is the LOCAL backend's token writer (a `TokenStore`): every token minted in
    this run lands in its holder's file under the row's `secret_id`, and the operator's
    holder is minted here too. None on the mock, which keeps its own.
    """
    from . import tokens

    conn = directory.init(db_path)
    result = ProvisionResult()
    try:
        # --- rooms -------------------------------------------------------------
        for room, kind in ROOM_KINDS.items():
            channel_id = gateway.create_channel(room)
            conn.execute(
                "INSERT OR IGNORE INTO rooms(channel_id, name, kind, created_at) VALUES (?,?,?,?)",
                (channel_id, room, kind, directory.now()),
            )
            result.rooms[room] = channel_id

        # --- slots: one #names webhook each -------------------------------------
        existing = set(directory.slot_ids(conn))
        for slot_id in range(1, slots + 1):
            if slot_id not in existing:
                conn.execute("INSERT INTO slots(slot_id, created_at) VALUES (?,?)",
                             (slot_id, directory.now()))
            for room, channel_id in _names_only(result.rooms).items():
                if directory.slot_webhook(conn, slot_id, channel_id) is None:
                    key = f"comms/slot-{slot_id}/{room.lstrip('#')}"
                    cred = gateway.create_webhook(channel_id, f"slot-{slot_id}", key)
                    _keep(store, tokens.owner_for_slot(slot_id), cred.secret_id or key, cred)
                    conn.execute(
                        "INSERT INTO webhooks(webhook_id, slot_id, agent_name, agent_scope, "
                        "channel_id, bws_secret_id, created_at) VALUES (?,?,NULL,NULL,?,?,?)",
                        (cred.webhook_id, slot_id, channel_id,
                         cred.secret_id or key, directory.now()),
                    )
            result.slots.append(slot_id)

        # --- operator + writer declaration --------------------------------------
        op_id, op_name = operator
        user_id = gateway.register_principal(op_name) if not op_id.isdigit() else op_id
        conn.execute("INSERT OR IGNORE INTO operators(user_id, name, created_at) VALUES (?,?,?)",
                     (user_id, op_name, directory.now()))
        result.operator_user_id = user_id
        directory.meta_set(conn, directory.META_WRITER, writer)
        conn.commit()

        # --- the operator's own posting credential ---
        if store is not None:
            result.operator_token_written = _operator_token(gateway, store)

        # --- standing identities -------------------------------------------------
        for name, scope in standing:
            add_standing(gateway, conn, name, scope, rooms=result.rooms,
                         daemon_client=daemon_client, store=store)
            result.standing.append((name, scope))
        for name, scope in send_only:
            add_standing(gateway, conn, name, scope, rooms=result.rooms,
                         daemon_client=daemon_client, store=store,
                         kind=directory.KIND_SEND_ONLY)
            result.standing.append((name, scope))
        conn.commit()
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------- readiness

#: `[provision]`'s defaults (settings.toml): 6 slots, no standing identities, the
#: operator `operator`. A box's config.toml can say otherwise; `configured()` reads it.
DEFAULT_SLOTS = ProvisionConfig.slots
DEFAULT_OPERATOR = ("operator", ProvisionConfig.operator)


def configured(env=None) -> tuple[int, tuple[str, str]]:
    """What `moxi-comms-provision` makes when run with no flags, and what the daemon
    makes on its first start: `[provision] slots`, and the operator named
    `[provision] operator`, read from config.toml now."""
    from . import config

    cfg = config.load(env).provision
    return cfg.slots, ("operator", cfg.operator)

#: The four states of a directory, by `readiness`.
EMPTY = "empty"              # no directory, or no slots in it
UNFINISHED = "unfinished"    # slots, but provisioning stopped before the operator's credential
LOST = "lost"                # slots, and a credential the record expects is missing
COMPLETE = "complete"        # provisioned


@dataclass(frozen=True)
class Readiness:
    state: str
    slots: int
    detail: str = ""

    @property
    def provisioned(self) -> bool:
        return self.state == COMPLETE


def readiness(db_path, *, operator_hash, store) -> Readiness:
    """Is this directory provisioned? The one definition, read-only.

    Provisioned means: `directory.db` holds at least one slot and the declared writer,
    which `provision` commits together; and, when `store` is given (the local backend,
    whose tokens live only in the store), every slot webhook's token is in its holder's
    file and the operator's stored token hashes to `operator_hash`, the hash the record
    keeps. With `store` None (the mock) the directory alone answers.

    `provision` commits the directory before it mints the operator's credential, so a
    run that died in between leaves slots and the writer with no operator hash in the
    record. That is UNFINISHED, and running `provision` again finishes it. A record
    that does keep an operator hash, or a slot whose token is missing, is LOST: the
    record keeps only hashes, so re-running cannot bring a token back, and it would
    mint the operator a fresh one over the evidence.
    """
    from . import tokens
    from .local_gateway import token_hash

    path = Path(db_path)
    if not path.exists():
        return Readiness(EMPTY, 0, f"{path} does not exist")
    conn = directory.connect(path, read_only=True)
    try:
        slots = directory.slot_ids(conn)
        writer = directory.meta_get(conn, directory.META_WRITER)
        webhooks = conn.execute(
            "SELECT slot_id, bws_secret_id FROM webhooks WHERE slot_id IS NOT NULL "
            "ORDER BY slot_id").fetchall()
    finally:
        conn.close()
    if not slots:
        return Readiness(EMPTY, 0, "the directory holds no slots")
    if not writer:
        return Readiness(UNFINISHED, len(slots), "the directory has no declared writer")
    if store is None:
        return Readiness(COMPLETE, len(slots))
    missing = sorted({f"slot-{row['slot_id']}" for row in webhooks
                      if row["bws_secret_id"] not in
                      store.secret_ids(tokens.owner_for_slot(row["slot_id"]))},
                     key=lambda name: int(name.split("-")[1]))
    held = store.read_secret(tokens.owner_for_role(tokens.ROLE_OPERATOR),
                             tokens.OPERATOR_SECRET_ID)
    if not operator_hash:
        if missing:
            return Readiness(LOST, len(slots), "the token store has no token for "
                             + ", ".join(missing))
        return Readiness(UNFINISHED, len(slots), "the operator's credential was never "
                                                 "minted")
    if not held or not _secrets.compare_digest(operator_hash, token_hash(held)):
        missing.insert(0, "operator")
    if missing:
        return Readiness(LOST, len(slots), "the token store has no matching token for "
                         + ", ".join(missing))
    return Readiness(COMPLETE, len(slots))


def operator_hash_in(record_path) -> str | None:
    """The operator token hash the local record keeps, read without writing anything.
    None when there is no record or no hash in it."""
    import sqlite3

    from .local_gateway import META_OPERATOR_TOKEN_HASH

    path = Path(record_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (META_OPERATOR_TOKEN_HASH,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row[0] if row else None


LOST_REFUSAL = (
    "moxi-comms-daemon: {db} holds {slots} slot(s), but {detail}. The record keeps only "
    "hashes, so provisioning again cannot bring a lost token back, and the daemon will "
    "not start on credentials nothing can present. Restore the token store ({store}) "
    "from a backup, or move {db} and {record} aside and start the daemon again to "
    "provision a new set.")


def first_start(store, *, slots: int | None = None, operator=None):
    """The daemon's first-start step on the local backend, as a callable the daemon
    runs after its single-instance probe and before it binds.

    EMPTY provisions `slots` slots, no standing identities and `operator`, which is what
    `moxi-comms-provision` makes with no flags; each left None is `[provision]`'s, read
    when the step runs. UNFINISHED re-runs the idempotent `provision`
    with the slot count already there. COMPLETE is left alone. LOST is a refusal
    (`RuntimeError`, which `moxi-comms-daemon` turns into its exit message).
    """
    import logging

    from .local_gateway import META_OPERATOR_TOKEN_HASH

    log = logging.getLogger("comms.daemon")

    def run(gateway, db_path) -> None:
        from .tokens import TokenStoreError

        try:
            found = readiness(db_path,
                              operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH),
                              store=store)
        except TokenStoreError as e:
            raise RuntimeError(f"moxi-comms-daemon: {e}") from None
        if found.state == COMPLETE:
            return
        if found.state == LOST:
            raise RuntimeError(LOST_REFUSAL.format(
                db=db_path, slots=found.slots, detail=found.detail, store=store.root,
                record=getattr(gateway, "path", "the record")))
        want_slots, want_operator = configured()
        count = slots if slots is not None else want_slots
        if found.state == UNFINISHED:
            conn = directory.connect(db_path, read_only=True)
            try:
                count = max(directory.slot_ids(conn))
            finally:
                conn.close()
        result = provision(gateway, db_path, slots=count,
                           operator=operator if operator is not None else want_operator,
                           store=store)
        log.info("first start: provisioned %d slot(s) and the operator credential on the "
                 "local backend (%s); the operator's id is %s (MOXI_COMMS_OPERATOR)",
                 len(result.slots),
                 "finished an unfinished run" if found.state == UNFINISHED else "new",
                 result.operator_user_id)

    return run


def _names_only(rooms: dict) -> dict:
    """The `#names` entry of a room map, alone. An existing directory may still
    hold `#conference` and `#notices` rows from before comms had one room; no slot or
    standing bundle gets a webhook in them."""
    return {NAMES: rooms[NAMES]} if NAMES in rooms else {}


def add_standing(gateway, conn, name: str, scope: str = GLOBAL, *, rooms=None,
                 daemon_client=None, store=None, kind: str = directory.KIND_AGENT) -> None:
    """Create a standing row + its webhook bundle, one `#names` webhook.

    Refuses `^agent-\\d+$` in every scope, and refuses a name currently live as a
    chosen binding in the same scope (release the binding first) when a daemon client is
    available to ask.

    `kind` is the row's directory kind, `agent` or `send-only`. A row that already
    exists with the other kind is refused, not converted: turning a name that receives
    mail into one that refuses it (or back) is a decision about that identity, and a
    re-run of the provisioner is not the place to make it by accident.

    `store` is `provision`'s, threaded down for the same reason: a standing identity's
    bundle is minted here, so on the local backend its tokens are written here.
    """
    from . import tokens

    if kind not in directory.ROW_KINDS:
        raise ProvisionError(f"{kind!r} is not a standing row kind; expected one of "
                             f"{', '.join(directory.ROW_KINDS)}")
    if is_auto_name(name):
        raise ProvisionError(
            f"{name!r}: the agent-N namespace is reserved for the auto-namer, so a standing identity can't take one")
    found = conn.execute("SELECT * FROM agents WHERE name = ? AND scope = ?",
                         (name, scope)).fetchone()
    if found is not None and directory.kind_of(found) != kind:
        raise ProvisionError(
            f"{name}@{scope} is already provisioned as a standing row of kind "
            f"{directory.kind_of(found)!r}, not {kind!r}; nothing was changed")
    if daemon_client is not None:
        for b in daemon_client.query("agents")["bindings"]:
            if b["name"] == name and b["scope"] == scope and b["tier"] == "chosen":
                raise ProvisionError(
                    f"{name}@{scope} is currently live as a chosen binding "
                    f"(adapter {b['adapter']}, slot {b['slot']}); release it first")
    conn.execute("INSERT OR IGNORE INTO agents(name, scope, status, created_at, kind) "
                 "VALUES (?,?, 'active', ?, ?)", (name, scope, directory.now(), kind))
    if rooms is None:
        rooms = {r["name"]: r["channel_id"] for r in conn.execute("SELECT * FROM rooms")}
    for room, channel_id in _names_only(rooms).items():
        if directory.agent_webhook(conn, name, scope, channel_id) is None:
            key = f"comms/{scope}/{name}/{room.lstrip('#')}"
            cred = gateway.create_webhook(channel_id, f"{name}@{scope}", key)
            _keep(store, tokens.owner_for_standing(name, scope), cred.secret_id or key, cred)
            conn.execute(
                "INSERT INTO webhooks(webhook_id, slot_id, agent_name, agent_scope, "
                "channel_id, bws_secret_id, created_at) VALUES (?,NULL,?,?,?,?,?)",
                (cred.webhook_id, name, scope, channel_id,
                 cred.secret_id or key, directory.now()),
            )
    conn.commit()


# --------------------------------------------------------------------------- send-only

def send_only_main(argv=None, *, gateway=None, store=None, env=None, out=None,
                   daemon_client=None) -> int:
    """`moxi-comms-provision send-only NAME@SCOPE` -- one send-only standing identity, alone.

    A send-only identity is a standing row whose directory kind is `send-only`, with
    its own webhook bundle: it posts from that bundle when its holder (a module's own
    process) is bound, and nothing is ever delivered to it. This verb adds the row and
    the bundle to a directory that is already provisioned, and does nothing else: it
    does not re-run the slots or the operator's lane, so it can be run
    against a live box without touching anything it was not asked about. It migrates
    the directory's `agents` table to carry the `kind` column first, if it has none.

    Re-running converges: a row that is already send-only with its bundle mints nothing.
    A row of the same name and scope whose kind is `agent` is refused, not converted.
    """
    import argparse
    import os

    from . import tokens
    from .client import DaemonClient, DaemonOffline
    from .gateway import BACKENDS, LOCAL, GatewayError, backend_name, open_gateway
    from .paths import daemon_socket, directory_db
    from .scoping import NAME_RE

    ap = argparse.ArgumentParser(
        prog="moxi-comms-provision send-only",
        description="add a send-only standing identity (a row of kind send-only and its "
                    "own webhook bundle) to an already provisioned directory")
    ap.add_argument("identity", metavar="NAME@SCOPE",
                    help="the identity, e.g. console@acme (no @SCOPE means global)")
    ap.add_argument("--gateway", choices=BACKENDS,
                    help="local or mock; default: MOXI_COMMS_GATEWAY, then the "
                         "config file, then local")
    ap.add_argument("--gateway-store", help="JSON store, mock backend only")
    ap.add_argument("--db", help="directory.db path (default: DATA/comms/directory.db)")
    ap.add_argument("--store", help=f"token store directory, local backend only (default: "
                                    f"${tokens.STORE_ENV_VAR} or the STATE path)")
    args = ap.parse_args(argv)

    env = env if env is not None else os.environ
    say = out or print
    name, sep, scope = args.identity.partition("@")
    scope = scope if sep else GLOBAL
    if not NAME_RE.match(name) or not NAME_RE.match(scope):
        raise SystemExit(f"moxi-comms-provision send-only: {args.identity!r} is not NAME@SCOPE. "
                         "Nothing was written.")
    db_path = Path(args.db or directory_db())
    if not db_path.exists():
        raise SystemExit(f"moxi-comms-provision send-only: {db_path} does not exist. Provision "
                         "the directory first (moxi-comms-provision); this verb adds one "
                         "identity to it. Nothing was written.")
    if args.gateway_store:
        env = dict(env, MOXI_COMMS_MOCK_GATEWAY=args.gateway_store)
    backend = backend_name(args.gateway, env=env)
    opened = None
    if gateway is None:
        try:
            opened = gateway = open_gateway(args.gateway, role="provisioner", env=env)
        except GatewayError as e:
            raise SystemExit(f"moxi-comms-provision send-only: {e}") from None
        # The local backend mints its tokens and they have no other home; the other two
        # keep their own (see `main`).
        if store is None and backend == LOCAL:
            store = tokens.TokenStore(args.store, env=env)
    asked = None
    if daemon_client is None:
        # The daemon, when it is up, says whether the name is live as a chosen binding;
        # a daemon that is down has no binding to collide with.
        try:
            asked = daemon_client = DaemonClient(daemon_socket()).connect()
        except DaemonOffline:
            daemon_client = None
    conn = directory.init(db_path)
    try:
        rooms = _names_only({r["name"]: r["channel_id"]
                             for r in conn.execute("SELECT * FROM rooms")})
        if not rooms:
            raise SystemExit(f"moxi-comms-provision send-only: the directory has no "
                             f"{NAMES} room. Provision the directory first "
                             "(moxi-comms-provision). Nothing was written.")
        found = conn.execute("SELECT status FROM agents WHERE name = ? AND scope = ?",
                             (name, scope)).fetchone()
        if found is not None and found["status"] != "active":
            # Reactivating would bring back an identity someone retired; that is a
            # decision about it, not something a provisioning re-run should make.
            raise SystemExit(f"moxi-comms-provision send-only: {name}@{scope} is provisioned "
                             f"but {found['status']}; nothing was changed. Reactivating "
                             "a retired identity is not this verb's to do.")
        add_standing(gateway, conn, name, scope, rooms=rooms, daemon_client=daemon_client,
                     store=store, kind=directory.KIND_SEND_ONLY)
        hooks = {room: directory.agent_webhook(conn, name, scope, channel_id)["webhook_id"]
                 for room, channel_id in rooms.items()}
    except (GatewayError, ProvisionError) as e:
        raise SystemExit(f"moxi-comms-provision send-only: {e}") from None
    finally:
        conn.close()
        if asked is not None:
            asked.close()
        if opened is not None:
            close = getattr(opened, "close", None)
            if close:
                close()
    qualified = name if scope == GLOBAL else f"{name}@{scope}"
    say(f"backend: {backend}")
    say(f"send-only {qualified}: standing row, kind send-only")
    for room, webhook_id in hooks.items():
        say(f"  webhook {room:<12} {webhook_id}")
    if store is not None:
        owner = tokens.owner_for_standing(name, scope)
        say(f"token store: {store.root}   holder: {owner.key} "
            + ("(has a file)" if owner.key in store.owners_present() else "(NO FILE)"))
    return 0


# --------------------------------------------------------------------------- operator CLI

def main(argv=None) -> int:  # pragma: no cover - exercised by the smoke runs
    """`moxi-comms-provision` -- OPERATOR machinery, deliberately not a `moxi comms` verb.

    The fence is the provisioner CREDENTIAL, the crown jewels: nothing on the
    agent-facing surface reaches it, so provisioning gets its own binary like
    `moxi-comms-daemon` and `moxi-comms-shim`. The `moxi comms` verbs that only read reach
    no credential, so they don't breach it, and the verbs that ACT stay four.

    Re-running against an already-provisioned directory CONVERGES: rooms are found by name,
    webhooks by directory row or by name in the channel, and only what is missing is
    created.

    One SUBCOMMAND, dispatched before argparse sees anything: `moxi-comms-provision
    send-only NAME@SCOPE` (one send-only standing identity, added to a provisioned
    directory). A subparser would have been the tidier shape and would have broken every
    `moxi-comms-provision --slots 6` invocation; this adds a verb without moving the old
    one.

    With no flags it makes `[provision]`'s slots and operator (`configured()`).
    """
    import argparse
    import logging
    import os
    import sys

    from . import tokens
    from .gateway import BACKENDS, LOCAL, GatewayError, backend_name, open_gateway
    from .paths import directory_db
    from .scoping import GLOBAL

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "send-only":
        return send_only_main(argv[1:])

    ap = argparse.ArgumentParser(
        prog="moxi-comms-provision",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="subcommand (it has its own --help):\n"
               "  moxi-comms-provision send-only NAME@SCOPE add one send-only standing identity")
    ap.add_argument("--gateway", choices=BACKENDS,
                    help="local or mock; default: MOXI_COMMS_GATEWAY, then the "
                         "config file, then local")
    ap.add_argument("--gateway-store", help="JSON store, mock backend only")
    ap.add_argument("--slots", type=int, default=None,
                    help="slot webhook bundles to have (default: [provision] slots)")
    ap.add_argument("--standing", action="append", default=[], metavar="NAME[@SCOPE]",
                    help="declare a standing identity (repeatable)")
    ap.add_argument("--operator-id", help="the operator's user id")
    ap.add_argument("--operator-name", default=None,
                    help="the operator's identity name (default: [provision] operator)")
    ap.add_argument("--writer", default="provisioner", help="the declared directory writer")
    ap.add_argument("--db", help="directory.db path (default: DATA/comms/directory.db)")
    ap.add_argument("--store", help=f"token store directory, local backend only (default: "
                                    f"${tokens.STORE_ENV_VAR} or the STATE path)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.gateway_store:
        os.environ["MOXI_COMMS_MOCK_GATEWAY"] = args.gateway_store

    backend = backend_name(args.gateway)
    want_slots, (_, want_operator) = configured()
    standing = []
    for entry in args.standing:
        name, sep, scope = entry.partition("@")
        standing.append((name, scope if sep else GLOBAL))
    try:
        gateway = open_gateway(args.gateway, role="provisioner")
    except GatewayError as e:
        raise SystemExit(str(e)) from None
    # The local backend mints its own tokens and they have no other home, so provision
    # is their writer. Built HERE because this is the caller that knows which backend it
    # opened, and skipped on the mock, which keeps its tokens in its own record.
    store = tokens.TokenStore(args.store, env=os.environ) if backend == LOCAL else None
    try:
        result = provision(gateway, args.db or directory_db(),
                           slots=args.slots if args.slots is not None else want_slots,
                           standing=standing, writer=args.writer,
                           operator=(args.operator_id or "operator",
                                     args.operator_name or want_operator),
                           store=store)
    except (GatewayError, ProvisionError) as e:
        # A refusal mid-converge is one line, not a traceback; what converged before it stands, the run stopped AT the refusal.
        raise SystemExit(f"moxi-comms-provision: {e}") from None
    finally:
        close = getattr(gateway, "close", None)
        if close:
            close()
    print(f"backend: {backend}")
    for room, channel_id in result.rooms.items():
        print(f"room {room:<12} channel {channel_id}")
    print(f"slots: {', '.join(str(s) for s in result.slots)}")
    for name, scope in result.standing:
        print(f"standing {name}@{scope}")
    print(f"operator: {result.operator_user_id}")
    if store is not None:
        # No token value and no per-holder detail: the holders are named, the values are
        # not.
        print(f"token store: {store.root}")
        print(f"holders with a file: {', '.join(store.owners_present()) or 'none'}")
        print("operator credential: "
              + ("minted" if result.operator_token_written else "already provisioned"))
    return 0
