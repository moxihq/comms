"""The launch context comms gives a session: the roster snapshot and the roster deltas.

comms' listener (`moxi_comms.listener`) answers turns with what this module renders, at
a session's start and at each prompt; the Codex launcher puts the same layer one into a
thread's developer instructions (`codex.layer_one_text`). comms writes no hook of its own:
turns carries the text into the session.

The launch roster snapshot: a labeled verbatim `moxi comms agents` snapshot, given at
session start, recipient-side, after the context block.

Roster deltas: after birth, roster changes ride turns that happen anyway. `delta` diffs
the live roster against the session's roster memory -- `roster.json` in the session's
RUNTIME dir (`listener.memory_path`) -- and gives only the joins and leaves, one
parenthetical line, self excluded, every tier included, ephemerals too. Nothing changed
-> None, not an empty line: most turns get nothing. Presence never wakes anyone; this
rides turns, it never causes one.

LAYER ONE: what a session is given at start is EXACTLY the agent-facing context block
(`context.layer_one_block`: the one canonical constant, with the director sentence for a
session that declared a director), the line naming which comms to run
(`context.MOXI_COMMS_PATH_LINE`, as a Codex or OpenCode session gets it), and then the
current roster. Nothing else. Everything an agent meets only conditionally -- the tiers,
the marks, what an unmarked frame means, the catch-up digest -- is layer two, pulled
through `moxi comms help <term>`. The block renders on the offline branch too: how to
send and reply is true whether or not the daemon answered.

The snapshot IS the verb's output: both call `roster_view.render_agents`, so "verbatim"
holds by construction rather than by a second rendering kept in sync. The delta diffs by
qualified name alone -- the name is the identity, so slot/adapter churn under an
unchanged name is not news -- and shows a join's tier the way the verb shows it.
Both show the names an agent can message: a send-only binding is in neither, on either
side of the diff. Waiting mail does not ride deltas: joins and leaves are the signal.

The state file is the qualified-name->{tier, adapter} map as this session last saw it
(a memory written before reply-enforcement holds the bare tier): advisory
session-lifetime memory, not STATE-contract data -- losing it costs at most one quiet
re-seed, never mail. It is written atomically (temp + rename in the same directory). The
snapshot SEEDS it on a successful query -- and only then: the offline envelope seeds
nothing, because "the roster is empty" and "comms was down" must not be confused in
memory any more than in rendering.

`delta` failure modes:
  * daemon unreachable -> nothing, and the file does NOT advance (silent skip -- the
    session-start line and the adapter's own offline surfacing carry the honesty; a
    per-prompt nag would duplicate them). The next successful diff carries the
    accumulated truth: at-least-once, replayed against a roster instead of a record.
  * state file missing or unreadable -> seed from the current roster, say nothing.
    An unknown baseline must not announce the whole world as newly joined.
  * self (the qualified identity from MOXI_COMMS_NAME / MOXI_COMMS_SCOPE / MOXI_COMMS_GLOBAL) is
    filtered from BOTH sides -- the own-binding race would otherwise make the first
    prompt announce "you joined".

The daemon being unreachable degrades honestly and never raises: a listener that fails
costs its own context, and comms being down must never stop an agent from working.

Which comms to run: the line names the command the session's launcher stamped,
`MOXI_COMMS_COMMAND`. A session an older launcher started has no stamp, and the line
names this process's own install instead (`context.own_comms`); the listener runs in
turns' endpoint process, whose install is the launching one where one Moxi environment
serves every session. With neither, the line names the bare `moxi comms`, as the Codex and
OpenCode launchers' do.

The own-binding race: the session's adapter binds before `claude` starts, so the snapshot
normally lists the session's own binding. When the session carries an identity stamp and
the roster comes back with no name it can message, one honest line says its own
registration may not have completed, rather than reading as partners being unreachable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .. import directors as directors_mod
from ..context import (BARE_COMMS, COMMAND_ENV, CONTEXT_BLOCK, MOXI_COMMS_PATH_LINE,
                       layer_one_block, own_comms)
from ..roster_view import SEND_ONLY_ADAPTER, messageable, render_agents
from ..client import DaemonClient
from ..scoping import GLOBAL, qualify

#: Layer one leads with the context block -- imported, never copied, so "from the one
#: canonical constant" holds by construction.
BLOCK = CONTEXT_BLOCK
LABEL = ("Comms roster as of session start (snapshot — `moxi comms agents` is always "
         "current):")
OFFLINE = ("comms was offline at session start; `moxi comms agents` shows the live roster "
           "once comms is back.")
#: appended ONLY when the session is identity-stamped and the roster came back empty:
#: the one case where the own-binding race could read as an outage.
OWN_BINDING_RACE = ("(this session's own binding as {name} may not have completed "
                    "registration yet; `moxi comms agents` will show it.)")
DELTA_OPEN = "(comms roster update: "
DELTA_CLOSE = " — `moxi comms agents` is always current.)"


def fetch(socket_path=None, *, timeout: float = 2.0) -> dict | None:
    """The one daemon round-trip both modes share: the `agents` query, or None.

    Any failure -- socket gone, timeout, a refusal -- is None. The alternative is a
    hook that raises, and a raising hook is a blocked session.
    """
    try:
        client = DaemonClient(socket_path, timeout=timeout).connect()
        try:
            return client.query("agents")
        finally:
            client.close()
    except Exception as e:                                       # noqa: BLE001
        print(f"comms roster: {e}", file=sys.stderr)
        return None


def usable(info) -> bool:
    """Is this a roster response both modes can render: a dict whose `bindings` is a
    list?

    The default view, the only one the snapshot and the delta render, reads `bindings`
    alone, so that is all this checks; `pending` is no longer read anywhere. (The
    daemon's `agents` answer still carries an always-empty `pending`, for the `usable`
    of an adapter started before it, which required one.) `fetch` already absorbs
    transport failures, but a version-skewed daemon can answer with a dict this build
    does not know how to index -- and `render_agents` reaching for a missing `bindings`
    would raise a KeyError out of `main()`, which is a FAILED SessionStart hook: exactly
    the blocked launch this module promises never to cause. A response we cannot render
    is a roster we did not get, so it takes the offline branch and the honest line that
    goes with it. The block ships either way.
    """
    return isinstance(info, dict) and isinstance(info.get("bindings"), list)


def roster_of(info: dict) -> dict[str, dict]:
    """The session's roster memory: qualified name -> its tier and its adapter, for
    the names an agent can message (send-only bindings dropped, as the roster drops
    them).

    The diff is keyed by qualified name alone, so adapter churn under an unchanged name
    is still not news; the adapter is kept so a remembered send-only row can be told
    apart. Slots and waiting mail earn no place in memory.
    """
    return {b["qualified"]: {"tier": b["tier"], "adapter": b.get("adapter")}
            for b in messageable(info["bindings"])}


def entry_of(value) -> dict:
    """One remembered name, whatever build wrote it: a memory written before holds
    the bare tier and no adapter, and renders exactly as it did."""
    if isinstance(value, dict):
        return {"tier": value.get("tier"), "adapter": value.get("adapter")}
    return {"tier": value, "adapter": None}


def self_identity(env=None) -> str | None:
    """The session's own qualified identity: what its claim holds when the session has
    a claim socket, else the launch stamp, or None."""
    from ..claimhold import claim_overlay
    env = claim_overlay(env if env is not None else os.environ)
    name = env.get("MOXI_COMMS_NAME")
    if not name:
        return None
    scope = GLOBAL if env.get("MOXI_COMMS_GLOBAL") else (env.get("MOXI_COMMS_SCOPE") or GLOBAL)
    return qualify(name, scope)


# ---------------------------------------------------------------------- the snapshot

def snapshot(socket_path=None, *, env=None, timeout: float = 2.0) -> tuple[str, bool]:
    """The envelope text, and whether the daemon answered."""
    info = fetch(socket_path, timeout=timeout)
    return render_snapshot(info, env=env)


def render_snapshot(info: dict | None, *, env=None) -> tuple[str, bool]:
    """The context block and the labeled snapshot envelope for a fetched roster (None --
    or anything `usable` cannot render -- is the offline branch). The block leads both
    branches: how to send and reply does not depend on liveness."""
    env = env if env is not None else os.environ
    # The session's declared directors, as its `moxi-comms-claude` stamped them: a session
    # with one gets the director sentence after the block.
    directors = directors_mod.from_env(env)
    comms = comms_command(env)
    if not usable(info):
        return with_block(OFFLINE, directors=directors, comms=comms), False
    lines = [LABEL, render_agents(info)]
    name = env.get("MOXI_COMMS_NAME")
    # The list the roster shows: one of only send-only rows is empty to the session too.
    if name and not messageable(info["bindings"]):
        lines.append(OWN_BINDING_RACE.format(name=name))
    return with_block("\n".join(lines), directors=directors, comms=comms), True


def comms_command(env) -> str:
    """The comms the session is told to run: its launcher's stamp, else this process's
    own install, else the bare `moxi comms`, as the Codex and OpenCode launchers say."""
    return env.get(COMMAND_ENV) or own_comms() or BARE_COMMS


def with_block(body: str, *, directors=(), comms=None) -> str:
    """Layer one, in order: the block, one blank line, the line naming `comms` (when
    there is one) and one blank line, then the roster (or the offline line), as the
    Codex and OpenCode launchers lay it out. The block is first because it is what the
    roster under it is FOR. A session with a declared director gets the director
    sentence at the block's end."""
    if comms:
        return (f"{layer_one_block(directors)}\n\n{MOXI_COMMS_PATH_LINE.format(comms=comms)}"
                f"\n\n{body}")
    return f"{layer_one_block(directors)}\n\n{body}"


# ---------------------------------------------------------------------- roster memory

def read_state(path: Path) -> dict[str, str] | None:
    """The remembered roster, or None when there is no usable baseline.

    Unreadable and missing are the same answer on purpose: a corrupt memory is an
    unknown baseline, and an unknown baseline seeds quietly rather than announcing
    the world.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(path: Path, roster: dict[str, str]) -> bool:
    """Atomic advance: temp + rename in the state file's own directory.

    Same-event hook entries overlap, so a reader
    must only ever see a complete file. Failure is diagnosed to stderr and absorbed
    -- the state file is advisory memory and a hook must never block a turn over it.
    """
    path = Path(path)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        with os.fdopen(fd, "w") as f:
            json.dump(roster, f, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"comms roster: state write failed: {e}", file=sys.stderr)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


# ---------------------------------------------------------------------- the delta

def diff(before: dict[str, str], now: dict[str, str],
         self_q: str | None) -> tuple[list[str], list[str]]:
    """(joins, leaves), keyed by qualified name, self filtered from both sides."""
    joins = sorted(q for q in now if q not in before and q != self_q)
    leaves = sorted(q for q in before if q not in now and q != self_q)
    return joins, leaves


def render_delta(joins: list[str], leaves: list[str], now: dict) -> str:
    """One parenthetical line. A join shows its tier the way the verb shows it;
    a leave is just the name -- the tier of someone gone is not news. The operator's
    join and leave read like any name's."""
    parts = [f"{q} [{entry_of(now[q])['tier']}] is now reachable" for q in joins]
    parts += [f"{q} left" for q in leaves]
    return DELTA_OPEN + "; ".join(parts) + DELTA_CLOSE


#: The key the delta keeps the ignored reminders it has already shown under, in the same
#: memory file. No qualified name can start with `#`.
SHOWN_KEY = "#unanswered-shown"

#: What the author's session is told, once per ignored reminder naming it.
UNANSWERED_LINE = "(comms: {owing} ended a turn without answering your msg {message_id}.)"


def unanswered_for(info: dict, self_q: str | None, shown: list) -> tuple[list[str], list]:
    """(the lines to show, the shown-memory to keep) for the records naming `self_q`.

    Each record is shown once: its key joins the memory when it is shown, and leaves it
    when the daemon stops listing it (paid, expired, or the owing binding ended). A
    session with no identity stamp is shown nothing.
    """
    rows = [u for u in (info.get("unanswered") or ())
            if isinstance(u, dict) and self_q and u.get("author") == self_q]
    keys = [f"{u.get('owing')}:{u.get('message_id')}" for u in rows]
    new = [(k, u) for k, u in zip(keys, rows) if k not in shown]
    lines = [UNANSWERED_LINE.format(owing=u.get("owing"), message_id=u.get("message_id"))
             for _, u in new]
    return lines, [k for k in shown if k in keys] + [k for k, _ in new]


def delta(socket_path=None, state_path: Path | str = "", *, env=None,
          timeout: float = 2.0) -> tuple[str | None, bool]:
    """The delta envelope text (None = nothing to say), and whether the daemon
    answered.

    The advance-then-emit order is deliberate: the file advances BEFORE the line is
    emitted, and a failed advance emits nothing. Emitting first would repeat the same
    announcement every prompt until a write succeeded; skipping keeps the honest
    shape -- no advance, so the next successful diff carries the accumulated change.

    Beside the joins and leaves it carries, once each, the ignored reminders that name
    this session as the author: one line per record, on a turn that happens anyway. An
    unknown roster baseline still announces no roster change; the records are shown,
    since none has been shown to this memory yet.
    """
    env = env if env is not None else os.environ
    state_path = Path(state_path)
    info = fetch(socket_path, timeout=timeout)
    if not usable(info):
        return None, False                     # silent skip: the file stays put
    now = roster_of(info)
    raw = read_state(state_path)
    # A memory written before this build may hold send-only rows; filtered like the
    # roster, or the first delta would announce one leaving.
    before = None if raw is None else {
        q: v for q, v in raw.items()
        if q != SHOWN_KEY and entry_of(v)["adapter"] != SEND_ONLY_ADAPTER}
    shown_before = list((raw or {}).get(SHOWN_KEY) or [])
    self_q = self_identity(env)
    owed_lines, shown = unanswered_for(info, self_q, shown_before)
    memory = {**now, SHOWN_KEY: shown} if shown else dict(now)
    if before is None:
        # unknown baseline: seed, and announce no roster change
        if not write_state(state_path, memory):
            return None, True
        return ("\n".join(owed_lines) or None), True
    joins, leaves = diff(before, now, self_q)
    if (before != now or shown != shown_before) and not write_state(state_path, memory):
        return None, True                      # could not advance -> nothing announced
    parts = [render_delta(joins, leaves, now)] if joins or leaves else []
    parts += owed_lines
    return ("\n".join(parts) or None), True
