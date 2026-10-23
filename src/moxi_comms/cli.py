"""`moxi comms` -- the canonical agent-facing surface: four verbs that ACT, three that read.

    moxi comms send <name[,name...]> "text"   --posture none|optional|once|open
                                         [--cap N | --no-cap] [--standing]
                                         [--reply-to ID] [--audience NAME] [--held]
                                         [--new] [--new-name] [--check]
    moxi comms reply <message-id> "text"      [--posture none|optional|once|open]
                                         [--cap N | --no-cap] [--standing]
                                         [--audience NAME] [--held] [--check]
    moxi comms agents                         [--seats]
    moxi comms history <identity>             [--limit N]
    moxi comms help [<term>]
    moxi comms whois <name>
    moxi comms explain <identity> [<message-id>]
    moxi comms cache [<name> | <session-id>] ...   (the policy's cache callers only)
    moxi comms doctor

The fence the verbs keep: nothing agent-facing reaches the provisioner credential, and
the verbs that ACT stay four (`send`, `reply`, `agents`, `history`).

`help` is local, read-only and static: no daemon round-trip, no credential, no network.
It prints from a table compiled into the package, so it works with comms entirely
offline -- it teaches READING, not liveness, the same posture as the start injection.

`whois` is a reader on the same terms: it reads at most one holder-authored card file and
touches nothing else -- strictly less than `help`, which at least reads a compiled-in
table. A card is written with an ordinary file write and never with a comms verb, which is
exactly how the card surface avoids becoming a fifth ACTING verb.

`explain` is a diagnostic for whoever is at the terminal when an agent has gone quiet:
it asks the daemon every live fact behind one identity's delivery and reads one cursor
file, and it acts on nothing. It touches no credential. It differs from the other two
readers in reaching the daemon at all, so it is the first reading verb that can be told
"comms offline" -- and when it is, it prints the cursor anyway, because the cursor is a
file and the one durable fact.

`cache` reads any agent's prompt-cache state. It answers only a caller the config file's
`[policy] cache_callers` names (the operator by default), found the way `send` finds a
sender -- a fence for agents that follow the rules, not a lock. It posts nothing and
changes nothing.

`doctor` reads what this box's comms is and where it lives. It prints the device and
comms' five directories under Moxi's roots (`moxi.paths`), the config file, the backend
and where it came from, the provisioned slots, the secrets chain, the adapter binaries
on PATH and whether the daemon answers. It writes nothing, posts nothing, and exits 0
unless the daemon is configured (a config file or `MOXI_COMMS_GATEWAY`) and down.

Send boundary, enforced here:
  * an unbound EPHEMERAL name is an honest error at the verb -- a retired agent-N is
    nobody and the number is never reused;
  * a CHOSEN name accepts, deferring when unbound, with an explicit notice -- declared
    latency, never a silent success. A `moxi comms send` to an identity the custody journal
    has never seen bound (a bare name is the global identity; `x@S` is x@S), with no
    standing row, is refused unless the sender passes `--new-name`;
  * an UNDECLARED STANDING name is an honest error too -- but nothing in a bare name
    distinguishes it from a chosen one, so the sender declares the expectation with
    `--standing` and the verb checks it against the directory. Without the flag,
    behaviour is unchanged: no row means chosen, which means accept and defer. The
    declaration is verb-level only; it never appears in the machine line or the record.
  * `reply` obeys the same rules, applied to the target it derives from the referent's
    author, and writes the referent's id into the machine line as `re:<message_id>` --
    the record's own correlation, because a post carries no reply field.
  * `--audience` on either verb writes `[audience:<name>]` on line zero. A reply
    carries the ask's audience: the verb copies it when the replier passes none, accepts
    one where the ask had none, and REFUSES a different one -- a message for someone
    else is a new send, not a reply. `send --reply-to` is a reply with the referent
    explicit and gets the same table.
  * every new `moxi comms send` (no `--reply-to`) states `--posture`: `none` writes
    `[no reply]`, `optional` writes `[reply-optional]` (a reply only if the recipient has
    something to add; nothing is owed), `once` writes `[reply-once]`, and `open` writes
    `[open]`, which makes the message the root of an open thread with limits. Without it
    the send is refused, exit 2, posting nothing. A reply needs none: it takes its
    posture from what it answers. A reply to a verified `[no reply]` is refused. A reply
    to `[reply-once]` or `[reply-optional]` goes out `[no reply]`, closing the exchange,
    unless it asks back with `--posture once`; `--posture open` there is refused. A
    reply in an open thread is open. Any other reply is open, with nothing in the slot.
  * open threads: a thread is derived from the record at each reply, by walking `re:`
    up to its root. Only a verified root carrying `[open]` makes one. A reply in it is
    refused once the thread has had its cap of verified replies (`--cap N` or `--no-cap`
    from its opener, on the send or a later reply of the opener's in it, else
    `[threads] reply_cap`, default 8), once its newest message has been readable by the
    replier for an hour (the daemon's `readable` query says when it was handed over),
    or after a `--posture none` reply in it. An answer to a verified `[reply-once]` in it
    that someone else wrote still posts. The verb writes `[1 reply left]` and
    `[last reply]` on the replies that leave one and that close it. A read that fails
    or may be cut short lets the reply through with a NOTE.
  * the exempt sender: the operator (any lane whose sender is `operator`). No posture
    refusal applies to him: a new send of his with no `--posture` is open, with no
    `[open]`, and what he passes is written as passed. The `--cap` refusals still apply.
  * the single-reply refusal: a plain `moxi comms send` whose
    recipients include someone holding a verified, unexpired `[reply-once]` ask to this
    sender, still unanswered in the record, posts nothing and names the cure --
    `moxi comms reply <id>`, or `--new` for a message on another subject. The daemon only
    nominates candidate asks; the verb decides on its own read of the record, and every
    read that fails lets the send through with a NOTE. This refusal never applies to the
    exempt sender; the cold-cache hold below holds his message like anyone's.
Resolution is tier/route resolution, never a liveness check: nothing about delivery
timing is inferred from it.

The cold-cache hold is a separate step after resolution. THE RULE: a message reaches a
bound agent live only when comms has read its cache as warm, or as fresh (nothing cached
to lose). The verdict is the daemon's own read, carried in its `session` answer for each
bound recipient, so it is the read the daemon delivers by. A recipient whose cache reads
cold, on any adapter, is HELD: the message is posted as an ordinary one, exit 0, and
waits for that session's next start, /clear, compaction or turn (an `agent-N` has only
its current session, and its held mail ends with its binding). The `sent` line comes
first, one `held:` line per held recipient with when its cache went cold, and the last
line says when each held recipient reads it.
Recipients that pass get the message now. An agent comms has no clock for, a model with
no lifetime row included, reads `no-cache` and gets its mail live. A read comms cannot
make refuses the whole post -- nothing posted, exit 6 (`EXIT_COLD`) -- and the refusal
opens with "Nothing was sent:" and each refused recipient's reason, and closes with
"Nothing was sent.": a bound recipient whose read is `unknown` (no session id yet, a
transcript comms cannot read), a failed `session` query, and a name that unbound while
comms checked it. There is no size exception and no sender exemption: the operator's
lane and every send-only identity are held like any agent. There is no
override. On every exit path the outcome line is last and every NOTE comes before it,
so the last line tells the truth. The verb's check decides what it says; the daemon
checks again at every delivery, and that check is the enforcement.

`--held` writes `[held]` on line zero. The daemon holds the message until the
recipient's next turn, or until its session starts, is cleared or is compacted; nothing
wakes it for this message alone. It holds a warm recipient too. It reads the recipients'
caches like any send and is accepted for every recipient; to the operator it is ignored,
with a NOTE.

Length is the verb's business too. The gateway seam declares `max_content_length`, the
one cap on one message; the verb checks it before any credential is resolved, and a
message over it is refused with nothing posted. Comms does not split a message.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
import time

from . import (attribution, cache, cards, claimfile, claimhold, config, cursors,
               directory, glossary, machine_line, policy, sending)
from .client import DaemonClient, DaemonError, DaemonOffline, DaemonTimeout
from .gateway import GatewayError
from .provision import NAMES
from .roster_view import render_agents
from .scoping import identity_of, is_auto_name, project_key
from .sender import MalformedStamp, OperatorRefused, operator_from_env, sender_from_env
# The send path lives in `sending`, the library under this verb and under
# `moxi_comms.api.send`. These names stay readable as `cli.<name>`: callers and rigs
# read the exit codes, the verb's words and `_bodies` here.
from .sending import (  # noqa: F401
    ASK_CHECK_NO_ANSWER, CLOSED_BY_CAP, CLOSED_BY_IDLE, CLOSED_BY_NONE, EXIT_BLOCKED,
    EXIT_COLD, EXIT_FAILED, EXIT_OFFLINE, EXIT_OK, EXIT_REFUSED, HELD_IGNORED_OPERATOR,
    HELD_NOTE, HELD_NOTE_SESSION_ONLY, NO_REPLY_REFUSED, NOT_BOUND_NOTE, ONCE_ASK,
    ONCE_NOT_OPEN, ONE_LEFT_SENT, OPERATOR, POSTURE_REQUIRED, THREAD_NOTE_MISSING,
    TIER_EPHEMERAL, TIER_STANDING, VERB_PROTOCOL_MISMATCH, WOULD_SEND, Context, SendPlan,
    VerbProtocolMismatch, _ago, _bodies, _claim_sender, _cure_flags, _hold_text,
    _nudge_daemon, _open_asks, _refusal_reason, _refused_tag, _reply_checks,
    _reply_targets, _say_notes, _say_would_send, _send, _send_checks, _send_resolved,
    _tag_matches, open_for, send_only_refusal)

log = logging.getLogger("comms.cli")

#: What the command is called when typed: the umbrella's `moxi` and the module's name.
#: `moxi` passes it in as `prog`; the bare `comms` is an opt-in shortcut that runs it.
PROG = "moxi comms"


# --------------------------------------------------------------------------- verbs

def cmd_send(args, ctx: Context) -> int:
    """`moxi comms send`: the library's send, rendered. It decides nothing of its own."""
    tags = [t.strip() for t in args.to.split(",") if t.strip()]
    return _render(ctx, sending.run(ctx, args, tags=tags))


def cmd_reply(args, ctx: Context) -> int:
    """`moxi comms reply`: the library's reply, rendered."""
    return _render(ctx, sending.run(ctx, args))


def _render(ctx: Context, result: sending.SendResult) -> int:
    """Print what a send said, line by line, and answer its exit code."""
    for line in result.lines():
        ctx.say(line)
    return result.exit_code


def cmd_agents(args, ctx: Context) -> int:
    try:
        client = ctx.daemon()
    except DaemonOffline as e:
        ctx.say(f"{e}\nNothing is addressable while the daemon is down.")
        return EXIT_OFFLINE
    try:
        info = client.query("agents")
    finally:
        client.close()
    ctx.say(render_agents(info, seats=getattr(args, "seats", False), now=ctx.clock()))
    return EXIT_OK


def cmd_history(args, ctx: Context) -> int:
    # Spelled once by the one rule: `x@global` reads as `x`.
    identity = identity_of(args.identity)
    window = None
    if is_auto_name(identity):
        # agent-N history is time-scoped to the binding, never the concatenation of
        # every process that ever held the name.
        try:
            client = ctx.daemon()
            try:
                window = client.query("binding_window", name=identity)["window"]
            finally:
                client.close()
        except (DaemonOffline, DaemonError):
            ctx.say("comms offline: agent-N history is time-scoped to its binding and "
                    "the daemon holds that window. Showing nothing rather than guessing.")
            return EXIT_OFFLINE
        if window is None:
            ctx.say(f"{identity}: no binding this daemon has seen. agent-N names are "
                    "never reused, so there is no history to show.")
            return EXIT_OK
    with ctx.db() as db:
        room = directory.room_by_name(db, NAMES)
        msgs = ctx.gateway.history(room["channel_id"], limit=args.limit)
        rows = []
        # The effective audience of every message IN THIS PAGE, oldest first (the seam's
        # `history` is ascending), so a hand-typed reply can show the audience it
        # inherited. A referent outside the page is not fetched:
        # history reads the record it holds, not the daemon's index.
        effective: dict[str, str | None] = {}
        for m in msgs:
            ml = machine_line.parse(m.content)
            if ml is None:
                continue
            effective[m.id] = ml.audience or (effective.get(ml.reply_to)
                                              if ml.reply_to else None)
            author = attribution.from_record(db, m)
            if window is not None and not _in_window(m, window):
                continue
            addressed = any(_tag_matches(t, identity) for t in ml.recipients)
            if author.qualified != identity and not addressed:
                continue
            rows.append((m, ml, author, "from" if author.qualified == identity else "to"))
    if not rows:
        ctx.say(f"no {NAMES} history for {identity}.")
    for m, ml, author, direction in rows:
        # `re:` is read back out of the machine line -- the record says what it is a
        # reply to, and nothing else is consulted for it.
        # the operator's audit surface, so both words are always spelled out -- the
        # frame line's silent defaults are for a reader mid-turn, not for a review of
        # the record, where "no mark" and "not rendered" must not be confusable.
        ctx.say(f"{m.id}  {m.timestamp}  {direction:<4} {author.qualified} "
                f"({author.tier}, {attribution.rendered(author.attribution)})"
                f"  -> {', '.join(ml.recipients)}"
                + (f" re:{ml.reply_to}" if ml.reply_to else "")
                + _history_audience(ml, effective)
                + _history_marks(ml))
        for line in ml.text.splitlines():
            ctx.say(f"      {line}")
    if window is not None:
        ctx.say(f"(time-scoped to the {identity} binding: {window[0]} .. "
                f"{window[1] if window[1] is not None else 'now'})")
    return EXIT_OK


def _history_marks(ml) -> str:
    """The posture token, when present, spelled as on line zero: `[no reply]`,
    `[reply-once]` or `[open]`."""
    bits = []
    if ml.open_mark:
        bits.append(machine_line.OPEN_TOKEN)
    elif ml.posture != machine_line.POSTURE_OPEN:
        bits.append(machine_line.POSTURE_TOKENS[ml.posture])
    return "".join(f" {bit}" for bit in bits)


def _history_audience(ml, effective: dict) -> str:
    """The audience bit of one history row, spelled out: the audience line zero carries,
    or the one it inherited from a referent in the same page, said as inherited. Nothing
    when neither is known -- history does not claim what it did not read."""
    if ml.audience:
        return f" audience: {ml.audience}"
    inherited = effective.get(ml.reply_to) if ml.reply_to else None
    return f" audience: {inherited} (inherited)" if inherited else ""


def cmd_help(args, ctx: Context) -> int:
    """The vocabulary lookup. Reads `glossary.TERMS` and nothing else.

    Note what this function does NOT touch: `ctx.daemon()`, `ctx.gateway`, `ctx.db()`.
    That is the whole point -- an agent that met `(unverified)` in a frame needs the
    word explained, and whether the daemon is up has no bearing on what the word means.
    Every other verb has a reason to be offline-fragile; this one must never acquire
    one, and `test_help.py` pins it by running the verb with a socket path that does
    not exist.
    """
    term = getattr(args, "term", None)
    if term is None:
        ctx.say(glossary.render_terms())
        return EXIT_OK
    entry = glossary.lookup(term)
    if entry is None:
        # EXIT_REFUSED, not a failure code: the verb worked, the word did not exist.
        ctx.say(glossary.render_unknown(term))
        return EXIT_REFUSED
    ctx.say(glossary.render_entry(entry))
    return EXIT_OK


def cmd_whois(args, ctx: Context) -> int:
    """The card lookup. Reads `cards` and nothing else.

    Same posture as `cmd_help`, and for a sharper reason: an agent runs this at the
    moment it is about to message someone, which is also the moment the context is worth
    spending -- and whether the daemon is up has no bearing on who someone is. So this
    function touches `ctx.daemon()`, `ctx.gateway` and `ctx.db()` exactly as much as
    `cmd_help` does, which is not at all, and `EXIT_OFFLINE` is unreachable from here.

    Absence answers EXIT_OK, deliberately diverging from `help`'s exit-2 miss: a glossary
    miss is a typo against a closed table, while a missing card is the DEFAULT
    state of the world for most identities and a permanent one for parked holders.
    EXIT_REFUSED is kept for a request comms will not answer at all (a malformed name, an
    ephemeral); EXIT_FAILED for a card that is there and cannot be read.
    """
    result = cards.answer(args.name, claimhold.claim_overlay(ctx.env))
    ctx.say(result.text)
    return {cards.OK: EXIT_OK, cards.MISS: EXIT_OK,
            cards.REFUSED: EXIT_REFUSED, cards.UNREADABLE: EXIT_FAILED}[result.status]


def cmd_explain(args, ctx: Context) -> int:
    """Why one message did or did not land, in live facts.

    Five questions decide whether mail landed, each with one place its ground truth
    lives, and the daemon's classifier turns those into four cases. This verb asks the
    daemon the whole table at once and prints the answer, plus the named reason the
    cursor is where it is.

    A READING verb. It posts nothing, advances nothing, and records nothing: asking does
    not change a single fact it reports, and comms keeps no receipts ledger for it to
    write to. What it does touch, and `help` and `whois` do not, is the daemon and one
    cursor file. It reaches no credential, no network and no provisioner secret.

    With the daemon down it still prints the cursor, which is the one durable fact,
    and says plainly that the rest is unknowable rather than showing a blank where a
    binding would be.
    """
    identity = args.identity
    try:
        client = ctx.daemon()
    except DaemonOffline as e:
        ctx.say(str(e))
        ctx.say("\n".join(_wrap(
            "The binding, the adapter kind and the last delivery are daemon memory, so "
            "with the daemon down there is nothing to read them out of. The cursor is a "
            "file and survives, so here is that much.", 76)))
        ctx.say("")
        ctx.say(_render_cursor(cursors.explain(identity_of(identity))))
        return EXIT_OFFLINE
    try:
        try:
            qualified = client.query("resolve", name=identity)["qualified"]
        except DaemonError as e:
            # The daemon refuses a retired identity (an agent-N whose binding ended, or
            # an `x@S` retired in the directory) by name, and that refusal is already the
            # whole answer to "why did my message not land". Print it as it came.
            ctx.say(_refused_tag(identity, e))
            return EXIT_REFUSED
        info = client.query("explain", identity=qualified, message_id=args.message_id)
    finally:
        client.close()
    ctx.say(_render_explain(info))
    for line in _launcher_status(qualified, ctx.env):
        ctx.say(line)
    return EXIT_OK


def cmd_owner(args, ctx: Context) -> int:
    """Which comms identity owns one session id now, and whom it awaits.

    A READING verb, for a program that nudges idle sessions, which asks it whether a
    session it may nudge is a director with an ask out: one `owner` query to the daemon, which answers from
    `session_owner` and computes `awaiting` exactly as the Stop hook's `owed` query does.
    It posts nothing, writes nothing, reaches no credential, and changes no hold, pend,
    delivery or ask.

    Exit 0 is an answer, with or without an owner: a session no live binding holds is
    an answer (`owner` null), not a failure. Every other exit is a read that failed:
    2 an empty session id, refused before the daemon is asked; 4 the daemon is down or
    closed the connection, or speaks another verb protocol; 5 the daemon timed out or
    answered an error. With `--json` each of these prints one JSON
    object on stdout, `ok` true with the answer or false with `error` and `detail`.
    """
    sid = args.session_id.strip()

    def fail(code: int, error: str, detail: str) -> int:
        if args.json:
            ctx.say(json.dumps({"ok": False, "session_id": sid, "error": error,
                                "detail": detail}))
        else:
            ctx.say(f"{sid or '(empty)'}: {detail}")
        return code

    if not sid:
        return fail(EXIT_REFUSED, "refused", "a session id is required")
    try:
        client = ctx.daemon()
    except DaemonOffline as e:
        return fail(EXIT_OFFLINE, "offline", str(e))
    try:
        answer = client.query("owner", session_id=sid)
    except DaemonOffline as e:
        return fail(EXIT_OFFLINE, "offline", str(e))
    except DaemonTimeout as e:
        return fail(EXIT_FAILED, "timeout", str(e))
    except DaemonError as e:
        return fail(EXIT_FAILED, "failed", str(e))
    finally:
        client.close()
    out = {"ok": True, "session_id": answer.get("session_id", sid),
           "owner": answer.get("owner"), "adapter": answer.get("adapter"),
           "awaiting": list(answer.get("awaiting") or [])}
    if args.json:
        ctx.say(json.dumps(out))
    elif out["owner"] is None:
        ctx.say(f"{sid}: no live binding owns this session.")
    else:
        waits = ", ".join(out["awaiting"]) or "nobody"
        ctx.say(f"{sid}: owned by {out['owner']} ({out['adapter']}); awaiting {waits}.")
    return EXIT_OK


#: How many of a launcher's newest status lines `moxi comms explain` prints.
LAUNCHER_STATUS_SHOWN = 5


def _launcher_status(qualified: str, env) -> list:
    """The newest lines of an `opencode` launcher's status line for this name: what it
    said while its window covered its own terminal. Read from the session file the
    launcher writes; nothing when there is none."""
    from .adapters import opencode, opencode_roster
    from .config import ConfigError

    path = opencode_roster.session_path(qualified)
    try:
        agent = opencode_roster.load(env=env).agents.get(qualified)
        if agent is not None:
            path = agent.session_file
    except ConfigError:
        pass
    lines = opencode.status_lines(path)[-LAUNCHER_STATUS_SHOWN:]
    if not lines:
        return []
    return ["", "moxi-comms-opencode status (newest last):"] + [
        f"  {row.get('at', '?')}  {row.get('text', '')}" for row in lines
        if isinstance(row, dict)]


#: Who `moxi comms cache` answers: the config file's `[policy] cache_callers`, the
#: operator by default. Exit 2.
CACHE_REFUSED = "moxi comms cache answers {callers} only; nothing was read."


def _cache_refused(ctx: Context) -> str:
    callers = policy.for_env(ctx.env).cache_callers
    names = " and ".join(("the operator" if c == OPERATOR else c) for c in callers)
    return CACHE_REFUSED.format(callers=names)


def _cache_caller(ctx: Context) -> str | None:
    """The caller's name if it may run `moxi comms cache`, else None. It resolves names in no
    scope: a name is read by the one rule.

    Identified exactly as `send` identifies a sender: the operator's terminal is
    `MOXI_COMMS_OPERATOR` with no claim beside it, and any other caller is a claim whose
    qualified name is one of `[policy] cache_callers` (a global identity's
    process carries `MOXI_COMMS_NAME` and `MOXI_COMMS_GLOBAL=1`). Both stamps at once, a
    malformed stamp, or no identity: refused.
    """
    env = claimhold.claim_overlay(ctx.env)
    project = project_key(env=env)
    try:
        operator_id = operator_from_env(env)
    except OperatorRefused:
        return None
    if operator_id is not None:
        return OPERATOR
    try:
        sender = sender_from_env(env, project)
    except MalformedStamp:
        return None
    if sender is not None and sender.qualified in policy.for_env(ctx.env).cache_callers:
        return sender.qualified
    return None


def cmd_cache(args, ctx: Context) -> int:
    """Whether each agent's prompt cache has expired.

    A READING verb. The kind and age of a name are the daemon's own read, from its
    `session` answer: the read the daemon delivers by, so what the operator hears and
    what a sender is told cannot disagree. A raw session id is read here, through
    comms' cache seam (`cache.clock_state`), with no evidence. A caller the policy does
    not name reads nothing.
    """
    caller = _cache_caller(ctx)
    if caller is None:
        ctx.say(_cache_refused(ctx))
        return EXIT_REFUSED
    try:
        client = ctx.daemon()
    except DaemonOffline as e:
        ctx.say(f"{e}\nWhich session sits behind a name is daemon memory; with the daemon "
                "down, only a raw session id can be read.")
        ids = [a for a in args.targets if cache.SESSION_ID_RE.match(a)]
        if not ids:
            return EXIT_OFFLINE
        for i in ids:
            ctx.say(cache.spoken_line(cache.kind_of(cache.target_for_session_id(i))))
        return EXIT_OFFLINE
    targets = []
    refused = False         # a name resolution refused (retired): exit 2
    try:
        wanted = list(args.targets)
        if not wanted:
            wanted = [b["qualified"] for b in client.query("agents").get("bindings", [])]
            if not wanted:
                ctx.say("no live bindings; there is no agent to read.")
                return EXIT_OK
        for arg in wanted:
            if cache.SESSION_ID_RE.match(arg):
                targets.append(cache.target_for_session_id(arg))
                continue
            try:
                qualified = client.query("resolve", name=arg)["qualified"]
            except DaemonError as e:
                targets.append(cache.target_for_name(arg, error=str(e)))
                refused = True
                continue
            targets.append(cache.session_target(client, qualified))
    finally:
        client.close()
    for target in targets:
        ctx.say(cache.spoken_line(cache.kind_of(target)))
    return EXIT_REFUSED if refused else EXIT_OK



# --------------------------------------------------------------------------- doctor

#: The adapter binaries `moxi comms doctor` looks for.
DOCTOR_BINARIES = ("claude", "codex", "opencode")
#: How long one `<binary> --version` may take before doctor says so and moves on.
DOCTOR_VERSION_TIMEOUT = 5.0


def _binary_version(path: str) -> str:
    import subprocess

    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=DOCTOR_VERSION_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return f"version not read ({e})"
    first = (proc.stdout or proc.stderr or "").strip().splitlines()
    return first[0] if first else f"version not read (exit {proc.returncode})"


def _slots_row(backend: str, cfg, ctx: Context) -> str:
    """The provisioned slots, by `provision.readiness`, the definition the daemon's
    first start uses. Read-only: nothing is opened for writing and nothing is created.
    A daemon on the local backend provisions an empty directory on its first start
    (`[provision] slots` of them); the mock keeps its own record."""
    from . import gateway as gw, provision, tokens
    from .paths import record_db

    local = backend == gw.LOCAL
    try:
        if local:
            record = cfg.local.record or record_db()

            found = provision.readiness(
                ctx.db_path, operator_hash=provision.operator_hash_in(record),
                store=tokens.TokenStore(None, env=ctx.env))
        else:
            found = provision.readiness(ctx.db_path, operator_hash=None, store=None)
    except Exception as e:  # an unreadable store or directory is reported, not raised
        return f"unknown ({e})"
    if found.state == provision.EMPTY:
        if local:
            return (f"none yet (the daemon provisions {cfg.provision.slots} on its "
                    "first start)")
        return "none (moxi-comms-provision makes them)"
    if found.state == provision.COMPLETE:
        return f"{found.slots} provisioned"
    if found.state == provision.UNFINISHED:
        then = ("the daemon finishes it on its next start" if local
                else "run moxi-comms-provision to finish it")
        return f"{found.slots}, unfinished: {found.detail}; {then}"
    then = ("the daemon will not start until the store is restored or the directory is "
            "provisioned afresh" if local else
            "sends that need those tokens fail until the store is restored")
    return f"{found.slots}, but {found.detail}; {then}"


#: The directories `moxi comms doctor` prints: comms' own under each of Moxi's roots.
DOCTOR_DIRS = ("data", "state", "cache", "config", "runtime")


def cmd_doctor(args, ctx: Context) -> int:
    """What this box's comms is and where it lives. Read-only."""
    import shutil

    from . import gateway as gw, secrets
    from .paths import LAYOUT, daemon_socket

    def row(label: str, value: str) -> None:
        ctx.say(f"{label:<10}  {value}")

    # Where this process keeps comms' files: its five directories under Moxi's roots
    # (`moxi.paths`), as this process resolves them.
    row("device", LAYOUT.device())
    for name in DOCTOR_DIRS:
        row(f"  {name.upper()}", str(getattr(LAYOUT, f"{name}_dir")()))

    configured = bool(ctx.env.get("MOXI_COMMS_GATEWAY"))
    try:
        cfg = config.load(env=ctx.env)
        configured = configured or cfg.source != "defaults"
        row("config", cfg.source if cfg.source != "defaults"
            else f"none ({config.config_path(ctx.env)} does not exist)")
        choice = gw.backend_choice(None, env=ctx.env)
        row("backend", f"{choice.name} ({choice.describe()})")
        row("slots", _slots_row(choice.name, cfg, ctx))
        row("secrets", ", ".join(cfg.secrets.chain)
            + ("" if tuple(cfg.secrets.chain) != tuple(secrets.default_chain())
               else " (the default)"))
        required = cfg.cache.required
    except (config.ConfigError, GatewayError) as e:
        row("config", f"ERROR: {e}")
        required = False

    # The cache module, which the cold-send hold reads through (an optional module).
    found = cache.module()
    row("cache", cache.describe_module(found))
    refusal = cache.required_refusal(required, found)
    if refusal:
        row("", "[cache] required is true, so the daemon refuses to start")

    # Other programs, found on PATH the way their users find them. comms' own entry
    # points are never looked up there (`moxi.paths.script` names them).
    for name in DOCTOR_BINARIES:
        path = shutil.which(name, path=ctx.env.get("PATH", os.environ.get("PATH")))
        row(name, f"{path}, {_binary_version(path)}" if path else "not on PATH")

    socket_path = ctx.socket_path or daemon_socket()
    try:
        client = DaemonClient(socket_path, timeout=2.0).connect()
        try:
            client.request("ping")
        finally:
            client.close()
        daemon_up, detail = True, f"answers at {socket_path}"
    except (DaemonOffline, DaemonError, OSError) as e:
        daemon_up, detail = False, f"not answering at {socket_path} ({e})"
    row("daemon", detail)
    if daemon_up:
        for line in _waiting_rows(socket_path):
            row("waiting", line)
    # Can comms reach a session? turns answers: its config, the listeners registered with
    # it (comms' among them), the live endpoints, and its Claude plugins.
    for found in _turns_rows():
        row("turns", found)
    if configured and not daemon_up:
        return EXIT_OFFLINE
    return EXIT_OK


def _waiting_rows(socket_path) -> list[str]:
    """One line per bound session that has no turns endpoint, with how long it has had
    none, or one line saying there is none. A session that never finds one (turns'
    Claude plugin not loaded) waits here until it ends."""
    try:
        client = DaemonClient(socket_path, timeout=2.0).connect()
        try:
            bindings = client.query("agents").get("bindings") or []
        finally:
            client.close()
    except (DaemonOffline, DaemonError, OSError) as e:
        return [f"could not be read ({e})"]
    now = time.time()
    lines = []
    for b in bindings:
        if b.get("endpoint") is not False:
            continue
        since = b.get("endpoint_since")
        age = (cache.spoken_span(now - since) if isinstance(since, (int, float))
               else "a while")
        lines.append(f"{b.get('qualified')}: bound, no turns endpoint for {age}; its "
                     "mail waits in its adapter")
    return lines or ["none (every bound session that reports one has its endpoint)"]


def _turns_rows() -> list[str]:
    """turns' own rows (`moxi_turns.check()`), each `ok` or `NOT OK`, name, detail."""
    try:
        import moxi_turns
        found = moxi_turns.check()
    except Exception as e:                                     # noqa: BLE001
        return [f"NOT OK  turns could not be checked: {type(e).__name__}: {e}"]
    return [f"{'ok' if r.ok else 'NOT OK'}  {r.name}: {r.detail}" for r in found]




def _field(label: str, first: str, *rest: str) -> list[str]:
    """One labelled field, continuation lines aligned under the first."""
    pad = " " * 17
    return [f"  {label:<15}{first}"] + [f"{pad}{line}" for line in rest]


def _wrap(text: str, width: int = 60, indent: str = "") -> list[str]:
    """Wrapped to a terminal, and never returning nothing where a line was expected."""
    return [f"{indent}{line}" for line in textwrap.wrap(text, width)] or [indent]


def _render_cursor(cursor: dict) -> str:
    """The cursor block on its own, so the offline path can print it alone."""
    lines = []
    where = f"backend {cursor['backend']}" if cursor["backend"] else "no backend resolved"
    if cursor["cursor"] is None:
        lines += _field("cursor", f"none on this box ({where})", cursor["read_path"])
    else:
        written = cursor["updated_at"] or "no timestamp in the file"
        lines += _field("cursor", f"{cursor['cursor']}, written {written}",
                        f"{where}, {cursor['read_path']}")
    if cursor["path"] != cursor["read_path"]:
        lines += _field("", f"the next advance writes {cursor['path']}")
    if cursor["reason"]:
        lines += _field("reason", cursor["reason"], *_wrap(cursor["sentence"]))
    if cursor["backend_reason"]:
        lines += _field("also", cursor["backend_reason"],
                        *_wrap(cursor["backend_sentence"]))
    return "\n".join(lines)


def _render_explain(info: dict) -> str:
    """The whole page: the verdict and what it means, then the facts under it, then the
    text the agent was actually shown.

    The verdict leads because it is the question the person came with. The facts are
    under it because the verdict is the one line they might not believe, and every fact
    it was made from is on the page. The injected text goes last because it is the only
    part with no fixed length.
    """
    from .daemon import CASE_REASONS

    d = info["delivery"]
    about = "" if d is None else f"   message {d['message_id']}"
    lines = [f"{info['identity']}   "
             f"{info['case'] or 'no delivery to classify'}{about}", ""]
    if info["case"]:
        # the classifier's four cases, or the waiting one, whose sentence is the named reason
        # the cursor has not reached it
        reason = CASE_REASONS.get(info["case"]) or cursors.REASONS.get(info["case"], "")
        lines += _wrap(reason, 74, "  ") + [""]
    if info["case_error"]:
        lines += _wrap(f"comms could not classify this message: "
                       f"{info['case_error']}. The cursor's own reason, below, is what "
                       "there is to go on.", 74, "  ") + [""]

    b = info["binding"]
    if b is None:
        lines += _field("binding", "none. nothing holds this name right now",
                        "mail sent to it waits in the record until something binds")
    else:
        lines += _field("binding", f"{b['tier']}, adapter {b['adapter']}, "
                                   f"bound {_ago(b['bound_for'])}")

    if d is None:
        lines += _field("delivered", "nothing this daemon still knows about",
                        "a binding drops what it was handed when it ends, and the",
                        "daily trim drops a hand-over older than an hour")
    else:
        when = "age unknown" if d["delivered_for"] is None else _ago(d["delivered_for"])
        of = ("" if d["asked_for"] or info["deliveries"] < 2
              else f" (the newest of {info['deliveries']})")
        lines += _field("delivered", f"{when}{of}")

    through = info["injected_through"]
    if through is None:
        lines += _field("injected", "nothing reported"
                        + ("" if b is None else " under this binding"))
    else:
        size = ("" if info["digest"] is None
                else f", {len(info['digest'])} characters shown")
        lines += _field("injected", f"through {through}{size}")

    if info["activity_for"] is None:
        lines += _field("activity", "none reported")
    else:
        stale = "" if b is not None else ", under a binding that has since ended"
        lines += _field("activity", f"{_ago(info['activity_for'])}{stale}")
    waiting = info.get("waiting") or []
    lines += _field("waiting", "none" if not waiting
                    else f"{len(waiting)} in the queue for its next release point: "
                         f"{', '.join(waiting[:5])}")
    lines += _render_cursor(info["cursor"]).splitlines()

    # Only where a write could plausibly have been the thing that failed. In the handled
    # case the cursor moved, so nothing declined to move it and this would be noise.
    if d is not None and info["cursor"]["reason"] != cursors.ALREADY_PAST:
        lines += ["", "  Two more reasons never reach this command. They happen inside "
                      "the session",
                  "  that writes the cursor and leave nothing behind to read. Grep that "
                  "session's",
                  "  log for them by name:"]
        lines += [f"    {name}" for name in cursors.WRITE_SIDE_REASONS]

    if info["digest"]:
        lines += ["", "  the text this identity was shown, verbatim:"]
        lines += _digest_lines(info["digest"])
    return "\n".join(lines)


def _digest_lines(digest: str, max_lines: int = 14, max_chars: int = 900) -> list[str]:
    """The injected text, indented, cut where it gets long and never cut quietly.

    The daemon hands this back verbatim so the explainer can show the input and not only the
    verdict. A catch-up digest can be fifty messages long, which is not a page anyone
    reads at 3am, so it is cut, and the cut says exactly how much was left out and where
    the rest lives.
    """
    body = digest[:max_chars]
    kept = body.splitlines()[:max_lines]
    shown = sum(len(line) + 1 for line in kept)
    lines = [f"    | {line}" for line in kept]
    if shown < len(digest):
        lines.append(f"    | [{len(digest) - shown} more characters, cut here. The "
                     "record has all of it: moxi comms history <name>]")
    return lines


def _in_window(msg, window) -> bool:
    from .daemon import _epoch
    start, end = window
    ts = _epoch(msg.timestamp)
    return ts >= start - 1 and (end is None or ts <= end + 1)


# --------------------------------------------------------------------------- parser

#: The help both verbs share for `--cap` and `--no-cap`.
CAP_HELP = ("on an open thread you opened: close it after N replies. On the send, or on "
            "any reply of yours in the thread")
NO_CAP_HELP = ("on an open thread you opened: no reply limit (it still closes after an "
               "hour with no message). On the send, or on any reply of yours in the thread")

CHECK_HELP = ("run every check this send makes before posting and then stop: posts, nudges "
              "and records nothing. Prints \"would send N message(s) to NAMES\", any "
              "held: lines and every NOTE the post would print, exit 0; or the send's own "
              "refusal, with its exit code")

def build_parser(prog: str = PROG) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description="agent-to-agent comms")
    ap.add_argument("--gateway", choices=("mock",),
                    help="which backend to talk to: mock (default: "
                         "MOXI_COMMS_GATEWAY, then the config file, then local)")
    sub = ap.add_subparsers(dest="verb", required=True)

    # `claude` is the launcher (`adapters/session.py`), with a parser of its own; `main`
    # hands it every argument after the verb, `--` and what follows included. Listed here
    # so `--help` names it.
    sub.add_parser("claude", add_help=False,
                   help="start a Claude Code session with a comms identity "
                        "(`claude --help` for its options). The options of "
                        "`moxi comms` itself, such as --gateway, do not apply to it")

    s = sub.add_parser("send", help="message an identity by name")
    s.add_argument("to", help="name, or a comma-separated list (one post, one thread)")
    s.add_argument("text")
    s.add_argument("--reply-to", dest="reply_to",
                   help="correlate with this message id (rides the machine line as "
                        "re:<id>; digits only)")
    s.add_argument("--standing", action="store_true",
                   help="declare that every recipient is a STANDING identity; refuse "
                        "(sending nothing) if any of them has no standing row")
    s.add_argument("--audience", metavar="NAME",
                   help="who the answer is for (rides the machine line as "
                        "[audience:NAME]; `operator` is the usual one)")
    s.add_argument("--held", action="store_true",
                   help="hold the message until the recipient's next turn, or until its "
                        "session starts, is cleared or is compacted; nothing wakes it for "
                        "this message")
    s.add_argument("--posture", choices=machine_line.POSTURES,
                   help="required on a new message: what you want back. none: no reply "
                        "(a reply to it is refused). optional: a reply is welcome but not "
                        "expected; answer only if you have something worth adding. once: "
                        "a single answer. open: a back-and-forth that closes after a set "
                        "number of replies or an hour with no message (moxi comms help "
                        "posture)")
    s.add_argument("--cap", metavar="N", help=CAP_HELP)
    s.add_argument("--no-cap", dest="no_cap", action="store_true", help=NO_CAP_HELP)
    s.add_argument("--new", action="store_true",
                   help="this message is not the reply to a single-reply ask you hold "
                        "open; send it as a new message")
    s.add_argument("--new-name", dest="new_name", action="store_true",
                   help="the recipient has not started yet: send to a name comms has never "
                        "seen bound, and the message waits for its first session. Without "
                        "it such a send is refused")
    s.add_argument("--check", action="store_true", help=CHECK_HELP)
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("reply", help="answer a message")
    r.add_argument("message_id")
    r.add_argument("text")
    r.add_argument("--standing", action="store_true",
                   help="declare that every recipient is a STANDING identity (the "
                        "message's author, or for your own message its recipients); "
                        "refuse (sending nothing) if any has no standing row")
    r.add_argument("--audience", metavar="NAME",
                   help="who the answer is for. The reply already carries the audience "
                        "of the message it answers; this adds one where it had none, "
                        "and a different one is refused")
    r.add_argument("--held", action="store_true",
                   help="hold the message until the recipient's next turn, or until its "
                        "session starts, is cleared or is compacted; nothing wakes it for "
                        "this message")
    r.add_argument("--posture", choices=machine_line.POSTURES,
                   help="leave it out: a reply takes its posture from the message it "
                        "answers. once asks something back; none closes an open thread")
    r.add_argument("--cap", metavar="N", help=CAP_HELP)
    r.add_argument("--no-cap", dest="no_cap", action="store_true", help=NO_CAP_HELP)
    r.add_argument("--check", action="store_true", help=CHECK_HELP)
    r.set_defaults(func=cmd_reply, reply_to=None)

    a = sub.add_parser("agents", help="who you can message")
    a.add_argument("--seats", action="store_true",
                   help="operator view: also show send-only identities, each binding's "
                        "slot, and the mail waiting for each name")
    a.set_defaults(func=cmd_agents)

    h = sub.add_parser("history", help="an identity's record, with honest attribution")
    h.add_argument("identity")
    h.add_argument("--limit", type=int, default=200)
    h.set_defaults(func=cmd_history)

    g = sub.add_parser("help", help="explain a comms word (tiers, marks, catch-up)")
    g.add_argument("term", nargs="?",
                   help="the word, as you saw it — marks resolve with or without "
                        "their punctuation ((unverified), [no reply], re:)")
    g.set_defaults(func=cmd_help)

    w = sub.add_parser("whois", help="read the card an identity wrote about itself")
    w.add_argument("name",
                   help="a comms identity — `name`, or `name@scope`. A bare name is "
                        "the global identity.")
    w.set_defaults(func=cmd_whois)

    x = sub.add_parser("explain",
                       help="why one message to an identity did or did not land")
    x.add_argument("identity",
                   help="a comms identity — `name`, or `name@scope`")
    x.add_argument("message_id", nargs="?",
                   help="which message to explain. The default is the newest delivery "
                        "the daemon can still see for that identity.")
    x.set_defaults(func=cmd_explain)

    # No help=: argparse prints a subcommand's row only when it has one, and the braces
    # below leave it out (a query for a program that nudges sessions, not a verb an
    # agent reads about).
    o = sub.add_parser("owner")
    o.add_argument("session_id", metavar="session-id",
                   help="a session id, as the session's harness names it")
    o.add_argument("--json", action="store_true",
                   help="one JSON object on stdout: ok, session_id, owner, adapter, "
                        "awaiting; or ok false with error and detail")
    o.set_defaults(func=cmd_owner)

    c = sub.add_parser("cache",
                       help="an agent's prompt-cache state (the policy's cache "
                            "callers only)")
    c.add_argument("targets", nargs="*", metavar="name-or-session-id",
                   help="a comms name (a bare name is the global identity) or "
                        "a Claude session id. With none, every bound name.")
    c.set_defaults(func=cmd_cache)

    d = sub.add_parser("doctor",
                       help="this box's comms: directories, config, backend, slots, "
                            "daemon (read-only)")
    d.set_defaults(func=cmd_doctor)

    wh = sub.add_parser("whoami", help="what comms identity this session holds")
    wh.set_defaults(func=cmd_whoami)
    v = sub.add_parser("service",
                       help="run the daemon as a systemd user service: install, "
                            "uninstall, or status")
    v.add_argument("action", choices=("install", "uninstall", "status"))
    v.set_defaults(func=cmd_service)
    sub.metavar = ("{" + ",".join(verb for verb in sub.choices if verb not in UNLISTED_VERBS)
                   + "}")
    return ap


def cmd_whoami(args, ctx: Context) -> int:
    """What comms identity this session holds, read from its claim, or why it holds
    none. Reads the claim socket and nothing else."""
    return claimhold.whoami(ctx.env, say=ctx.say)


def cmd_service(args, ctx: Context) -> int:
    """`moxi comms service`: the box's service manager, not
    the comms record. See `service.py`."""
    from . import service

    return service.run(args.action, env=ctx.env, say=ctx.say)


#: Verbs that parse and run but no help list names: `moxi comms --help` leaves them out of
#: its braces and its rows. `owner` is a query for a program that nudges sessions.
UNLISTED_VERBS = ("owner",)


def main(argv=None, ctx: Context | None = None, *, prog: str = PROG) -> int:
    """`moxi comms <verb>`: the umbrella calls this in process as `main(argv,
    prog="moxi comms")`, so the usage and help lines name the command as typed."""
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv[:1] == ["claude"]:
        from .adapters import session
        return session.main(argv[1:], prog=f"{prog} claude")
    args = build_parser(prog).parse_args(argv)
    ctx = ctx or Context(backend=getattr(args, "gateway", None))
    try:
        return args.func(args, ctx)
    except config.ConfigError as e:
        # A config file that does not load refuses the verb, naming the key and the fix.
        ctx.say(str(e))
        return EXIT_REFUSED



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
