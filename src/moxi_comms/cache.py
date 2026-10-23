"""How cold is an agent's prompt cache: comms' one seam to the cache module.

**THE RULE (cold sends).** A message reaches a bound
agent live only when comms has read that agent's cache as warm, or as fresh (nothing
cached to lose). Every other live delivery is held, whoever sent it and by whatever path.
No sender, lane, flag or adapter is exempt. The daemon (`Daemon._gate`) and the send verb
act on the read made here.

**cache says what a recipient's cache is; comms decides what happens to the mail.** comms
reads no transcript, rollout or database itself. This file is the one place comms reaches
the cache module, and it reaches it only through `moxi.modules.load("cache",
interface=1).api`; no import statement in comms names cache's package. comms keeps its own
`Clock`, kinds and `MISSING`, so the daemon imports and runs with no cache installed.

**ONE function reads the clock**: `clock_state`. The daemon runs it at every delivery and
in its `session` answer. The send verb and `moxi comms cache` take their verdict from that
answer, so what the operator hears, what a sender is told and what the daemon does can
never disagree. Only `moxi comms cache <session-id>` runs it in a caller's process, for
display, and that run passes no evidence.

`clock_state` asks, in this order:

1. the binding's kind (`kinds`): a `person` kind reads `no-cache` and an adapter with no
   clock (`send-only`, a kind the registry does not know) reads `no-cache` too, without
   asking for the module;
2. `load("cache", interface=1)`: when cache is not `present`, every agent reads
   `no-cache`, code `module-absent`, so comms holds nothing for a cold cache and refuses
   nothing for an unreadable one;
3. a kind's evidence function, for a kind that declares one: it names the session to
   read and whether the next delivery starts a new one, or says the record is unreadable;
   its session id names a session someone has run, so it is read with no root of its own
   and `turned=True`;
4. cache's `read`, on the harness the kind names, with the evidence the binding's own
   adapter reported.

Every `no-cache` and `unknown` carries a `code`; comms branches on the code, never on the
reason's words. A read never raises: an exception anywhere in it is `unknown`, code
`read-failed`.

**`[cache] required`** (comms' config) makes the daemon refuse to start when cache is not
`present` (`required_refusal`), so a box whose cold-send rule matters cannot lose the hold
without a word.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import time
from dataclasses import dataclass, fields

from . import kinds as kinds_mod

log = logging.getLogger("comms.cache")

WARM, COLD, FRESH, NO_CACHE, UNKNOWN = "warm", "cold", "fresh", "no-cache", "unknown"

#: The kinds a read can give.
KINDS = (WARM, COLD, FRESH, NO_CACHE, UNKNOWN)


class _Missing:
    """An argument the caller did not pass, kept apart from None."""

    def __repr__(self):
        return "MISSING"


MISSING = _Missing()

#: The kinds a live delivery may go to (THE RULE). Everything else is held or refused.
PASSING = (WARM, FRESH, NO_CACHE)

#: A string shaped like a Claude session id. `moxi comms cache` reads one directly.
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

#: The module this file reaches, and the interface it speaks.
MODULE = "cache"
INTERFACE = 1
#: The one command that installs it, through comms' extra (`moxi install`).
INSTALL = "moxi install comms --with cache"

# The reasons a read is `unknown` that comms itself gives.
NOT_BOUND = "not bound right now, so there is no session to read"

# ------------------------------------------------------------------------------- codes
# The codes the seam gives itself. Every other code is cache's (`moxi_cache.api.CODES`);
# the ones comms branches on are named below.

#: `no-cache`: cache is not installed (or does not load), so nothing is read.
MODULE_ABSENT = "module-absent"
#: `no-cache`: an adapter kind comms has no clock for (`send-only`, an unknown kind).
NO_CLOCK = "no-clock"
#: `no-cache`: a `person` kind.
PERSON = "person"
#: `unknown`: a kind's evidence function said its record cannot be read.
EVIDENCE_UNREADABLE = "evidence-unreadable"
#: `unknown`: a kind's evidence function failed, or answered in a shape comms does not
#: read.
EVIDENCE_FAILED = "evidence-failed"
#: `unknown`: an exception inside the read (cache's code of the same name, or the seam's).
READ_FAILED = "read-failed"

#: cache's codes for a `no-cache` with no clock to count by: a model with no lifetime row
#: and a harness cache has no clock for.
NO_ROW = "no-row"
UNKNOWN_HARNESS = "unknown-harness"
#: cache's code for a free model.
FREE = "free"
#: cache's codes for a measured lifetime it cannot read from the drift record.
DRIFT_HELD = "drift-held"
NO_DRIFT_RECORD = "no-drift-record"

#: The `no-cache` codes that mean "no clock to count by", which `spoken_line` says so.
NO_CLOCK_CODES = (MODULE_ABSENT, NO_CLOCK, NO_ROW, UNKNOWN_HARNESS)


def no_clock(adapter) -> str:
    """The reason for an adapter comms has no clock for."""
    return f"comms has no cache clock for {adapter or 'unknown'} agents"


# --------------------------------------------------------------------------- the clock

@dataclass
class Clock:
    """What `clock_state` read: a kind, the cache's age when there is a clock, the
    lifetime the read used (`ttl_seconds`, None when it used none) and where it came from
    (`ttl_source`), what the lifetime was looked up by (`key`, in words), and why
    (`reason` in words, `code`) when the read is `unknown`, `no-cache` or a `cold` with no
    lifetime.

    A Codex read on a measured row may also carry `lowering`, a lowering of that model's
    lifetime the learning drift store wrote before the read returned; `notes`, journal
    lines about cold landings under the documented minimum; and `judged`, (thread id, the
    newest turn the drift scan judged)."""
    kind: str
    age_seconds: float | None = None
    reason: str | None = None
    ttl_seconds: float | None = None
    key: str | None = None
    basis: str | None = None             # "documented" or "measured", with a lifetime
    provider: str | None = None
    model: str | None = None
    lowered_from: float | None = None    # a measured row's shipped number, when lowered
    lowering: dict | None = None
    notes: list | None = None
    judged: tuple | None = None
    code: str | None = None
    ttl_source: str | None = None


_CLOCK_FIELDS = tuple(f.name for f in fields(Clock))


def _from_cache(theirs) -> Clock:
    """cache's `Clock` as comms' own, field by field; a field comms does not know is
    left behind, and a kind outside the five reads `unknown`."""
    got = {name: getattr(theirs, name, None) for name in _CLOCK_FIELDS}
    if got["kind"] not in KINDS:
        return Clock(UNKNOWN, reason=f"cache answered a kind comms does not know "
                                     f"({got['kind']!r})", code=READ_FAILED)
    return Clock(**got)


# --------------------------------------------------------------------------- the module

def module():
    """cache as `moxi.modules.load` finds it now: a `Found`, `present`, `absent` or
    `broken`. Never raises. Asked at the point of use; `load` keeps one record per
    process, so asking again costs a dictionary lookup."""
    from moxi import modules

    return modules.load(MODULE, interface=INTERFACE)


def present() -> bool:
    return module().state == "present"


def describe_module(found=None) -> str:
    """One line for `moxi comms doctor` and the required refusal."""
    found = module() if found is None else found
    if found.state == "present":
        return (f"present ({found.distribution} {found.version}, interface {INTERFACE}); "
                "the cold-send hold reads every agent's cache")
    if found.state == "absent":
        return (f"not installed; no cache is read and nothing is held for a cold one. "
                f"Install it: {INSTALL}")
    return f"broken ({found.reason}); no cache is read. Reinstall it: {INSTALL}"


def required_refusal(required: bool, found=None) -> str | None:
    """Why the daemon refuses to start, or None: `[cache] required` is true and cache is
    not `present`."""
    if not required:
        return None
    found = module() if found is None else found
    if found.state == "present":
        return None
    what = "is not installed" if found.state == "absent" else f"does not load ({found.reason})"
    return (f"comms' config sets [cache] required = true, but the cache module {what}, so "
            f"the daemon would deliver into cold caches with no hold. Install it "
            f"({INSTALL}), or set [cache] required = false in comms' config file if "
            "running without the hold is what you mean.")


def projects_root(environ) -> str | None:
    """The Claude projects root a session states as its evidence, by cache's rule over
    the session's environment (cache re-exports sessions' `projects_root`). None when
    cache is not present: nothing would read it."""
    found = module()
    if found.state != "present" or found.api is None:
        return None
    try:
        return str(found.api.projects_root(environ))
    except Exception as e:                                         # noqa: BLE001
        log.warning("the Claude projects root could not be worked out (%s: %s)",
                    type(e).__name__, e)
        return None


def open_store(*, learn: bool):
    """cache's drift store (`learn=True` asks to be the one learner), or None when cache
    is not present or the store does not open."""
    found = module()
    if found.state != "present" or found.api is None:
        return None
    try:
        return found.api.open_store(learn=learn)
    except Exception as e:                                         # noqa: BLE001
        log.warning("cache's drift store did not open (%s: %s); a measured lifetime "
                    "reads unknown", type(e).__name__, e)
        return None


def close_store(store) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        try:
            close()
        except Exception:                                          # noqa: BLE001
            log.exception("closing cache's drift store failed")


# --------------------------------------------------------------------------- the read

#: The built-in kinds alone: what a read with no registry (a raw id) knows.
_BUILT_IN = kinds_mod.built_in()

#: An evidence function's target -> the function, imported once per process.
_evidence_fns: dict = {}
_evidence_lock = threading.Lock()


def _evidence_fn(target: str):
    with _evidence_lock:
        fn = _evidence_fns.get(target)
    if fn is None:
        path, _, attr = target.partition(":")
        fn = getattr(importlib.import_module(path), attr)
        with _evidence_lock:
            _evidence_fns[target] = fn
    return fn


def clock_state(adapter, session_id, *, projects_root=None, turned=True, name=None,
                now=None, cache_source=None, new_session=False, agent_model=MISSING,
                bound_at=None, store=None, kinds=None) -> Clock:
    """THE function: one recipient's cache kind, its age, and why.

    `adapter` is the binding's kind, looked up in `kinds` (the daemon's registry; by
    default comms' built-in kinds). `session_id` is the id its adapter reported (a Claude
    session, a Codex thread, an OpenCode session). `name` is the binding's qualified
    name, which a kind's evidence function is given. `projects_root` is the root a Claude
    session itself reported and `turned` whether a prompt or a turn end has been seen in
    this session id: together they are the evidence for reading a missing transcript as
    `fresh`. `cache_source`, `new_session` and `agent_model` are a Codex or OpenCode
    launcher's report, `bound_at` when the binding registered, and `store` cache's drift
    store (`open_store`).

    `turned` defaults to True, `new_session` to False and `agent_model` to not reported,
    so a caller that passes no evidence gets `unknown` for a missing record. Never raises.
    """
    try:
        return _clock_state(adapter, session_id, projects_root=projects_root,
                            turned=turned, name=name,
                            now=time.time() if now is None else now,
                            cache_source=cache_source, new_session=new_session,
                            agent_model=agent_model, bound_at=bound_at, store=store,
                            kinds=kinds)
    except Exception as e:                                         # noqa: BLE001
        return Clock(UNKNOWN, reason=f"the cache read failed ({type(e).__name__}: {e})",
                     code=READ_FAILED)


def _clock_state(adapter, session_id, *, projects_root, turned, name, now, cache_source,
                 new_session, agent_model, bound_at, store, kinds) -> Clock:
    kind = (kinds if kinds is not None else _BUILT_IN).get(adapter)
    # 1. what answers without the module
    if kind is None or kind.cache == kinds_mod.CACHE_SEND_ONLY:
        # No clock, so nothing to count: mail goes live, never blocked for lack of one.
        return Clock(NO_CACHE, reason=no_clock(adapter), code=NO_CLOCK)
    if kind.cache == kinds_mod.CACHE_PERSON:
        return Clock(NO_CACHE, code=PERSON)
    # 2. the module
    found = module()
    if found.state != "present" or found.api is None:
        why = ("cache is not installed" if found.state == "absent"
               else f"cache does not load ({found.reason})")
        return Clock(NO_CACHE, reason=why, code=MODULE_ABSENT)
    api = found.api
    # 3. a kind's evidence
    if kind.evidence:
        try:
            answer = _evidence_fn(kind.evidence)(name, session_id)
        except Exception as e:                                     # noqa: BLE001
            return Clock(UNKNOWN, reason=f"the {adapter} kind's evidence failed "
                                         f"({type(e).__name__}: {e})",
                         code=EVIDENCE_FAILED)
        if answer == kinds_mod.UNREADABLE:
            who = name or f"the {adapter} agent"
            return Clock(UNKNOWN, reason=f"{who}'s session record exists but could not be "
                                         "read as one", code=EVIDENCE_UNREADABLE)
        if (not isinstance(answer, (tuple, list)) or len(answer) != 2
                or not isinstance(answer[0], str) or not isinstance(answer[1], bool)):
            return Clock(UNKNOWN, reason=f"the {adapter} kind's evidence answered "
                                         f"{answer!r}, not (session id, new session)",
                         code=EVIDENCE_FAILED)
        # A session someone has run: no root of its own, and a request was made.
        session_id, new_session = answer
        projects_root, turned = None, True
    # 4. cache's read
    theirs = api.read(kind.harness, session_id, now=now, projects_root=projects_root,
                      turned=turned, cache_source=cache_source, new_session=new_session,
                      agent_model=api.MISSING if agent_model is MISSING else agent_model,
                      bound_at=bound_at, store=store)
    return _from_cache(theirs)


# --------------------------------------------------------------------------- targets

@dataclass
class Target:
    """One thing to read: a bound name as the daemon described it, or a raw session id.

    `settled` is the kind already decided: the daemon's verdict for a name, or a failed
    lookup (`unknown`, with `reason`). `age_seconds` comes with it. A Target with no
    `settled` is a raw session id, read in this process.
    """
    label: str
    adapter: str | None = None
    session_id: str | None = None
    bound: bool = False
    bound_at: float | None = None
    bound_for: float | None = None
    settled: str | None = None
    reason: str | None = None
    age_seconds: float | None = None
    ttl_seconds: float | None = None
    key: str | None = None
    cache_source: str | None = None
    code: str | None = None
    #: Whether the bound session has a turns endpoint its adapter delivers through, and
    #: for how long it has had it or lacked it (the daemon's `endpoint`, `endpoint_for`).
    #: None when the daemon or the adapter reports no such state.
    endpoint: bool | None = None
    endpoint_for: float | None = None


@dataclass
class CacheState:
    target: str
    kind: str
    reason: str | None = None            # why `unknown`, a lifetime-less `cold`, a free model
    age_seconds: float | None = None
    ttl_seconds: float | None = None     # the lifetime the read used, when it used one
    key: str | None = None               # what the lifetime was looked up by, in words
    cache_source: str | None = None      # a codex binding's sessions root
    session_id: str | None = None
    adapter: str | None = None
    bound_at: float | None = None
    bound_for: float | None = None
    code: str | None = None              # the read's code (a `no-cache` or `unknown`)


def target_for_session_id(session_id: str) -> Target:
    """A raw session id: no daemon, no adapter, just the transcript."""
    return Target(label=session_id, adapter="claude-shim", session_id=session_id,
                  bound=True)


def target_for_name(qualified: str, reply: dict | None = None, *,
                    error: str | None = None) -> Target:
    """A name, from the daemon's answer to `query kind="session"` (or why it failed).

    The kind and age are the daemon's (`cache_kind`, `age_seconds`, `cache_reason`): the
    read it delivers by, with the lifetime it used (`cache_ttl`), what that was looked up
    by (`cache_key`) and a codex binding's root (`cache_source`), for every adapter: an
    adapter comms has no clock for reads as the daemon answers it. A reply whose
    `cache_kind` is not one of the five is `unknown`. `cache_code` is the read's code.
    The session id is the binding's.
    """
    if error is not None:
        return Target(label=qualified, settled=UNKNOWN, reason=error)
    reply = reply or {}
    if not reply.get("bound"):
        return Target(label=qualified, settled=UNKNOWN, reason=NOT_BOUND)
    adapter = reply.get("adapter")
    session_id = reply.get("session_id") or None
    endpoint = reply.get("endpoint")
    endpoint_for = reply.get("endpoint_for")
    base = dict(label=qualified, adapter=adapter, bound=True, session_id=session_id,
                bound_at=reply.get("bound_at"), bound_for=reply.get("bound_for"),
                endpoint=endpoint if isinstance(endpoint, bool) else None,
                endpoint_for=float(endpoint_for) if _number(endpoint_for) else None)
    kind = reply.get("cache_kind")
    if kind not in (WARM, COLD, FRESH, NO_CACHE, UNKNOWN):
        kind = UNKNOWN
    age = reply.get("age_seconds")
    ttl = reply.get("cache_ttl")
    source = reply.get("cache_source")
    reason = reply.get("cache_reason") or None
    code = reply.get("cache_code")
    return Target(**base, settled=kind, code=code if isinstance(code, str) and code else None,
                  reason=(reason or "no reason given") if kind == UNKNOWN else reason,
                  age_seconds=float(age) if _number(age) else None,
                  ttl_seconds=float(ttl) if _number(ttl) else None,
                  key=reply.get("cache_key") if isinstance(reply.get("cache_key"), str)
                  else None,
                  cache_source=source if isinstance(source, str) and source else None)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def session_target(client, qualified: str) -> Target:
    """Ask the daemon which session sits behind `qualified`, with its verdict.

    A failed query is `unknown`, with its reason; the send refuses it.
    """
    from .client import DaemonError, DaemonOffline

    try:
        reply = client.query("session", identity=qualified)
    except DaemonOffline as e:
        return target_for_name(qualified, error=f"the comms daemon is offline ({e})")
    except DaemonError as e:
        return target_for_name(qualified, error=f"the session query failed ({e})")
    return target_for_name(qualified, reply)


# --------------------------------------------------------------------------- composing

def cache_state(target, *, client=None, env=None) -> CacheState:
    """One name or raw session id: its kind (the daemon's for a name, `clock_state` in
    this process for a raw id). `target` is a Target, a raw session id, or a qualified
    name (which needs `client`, a connected DaemonClient)."""
    if isinstance(target, str):
        if SESSION_ID_RE.match(target):
            target = target_for_session_id(target)
        elif client is None:
            raise ValueError("a name needs a daemon client to find its session")
        else:
            target = session_target(client, target)
    return cache_states([target], env=env)[0]


def cache_states(targets, *, env=None) -> list[CacheState]:
    """Each target's kind (`kind_of`). `env` is accepted for the callers that pass it
    and read by nothing here."""
    return [kind_of(t) for t in targets]


def kind_of(t: Target) -> CacheState:
    """The kind alone: the settled one, or a raw id's `clock_state` read with no
    evidence (so a raw id with no transcript is `unknown`)."""
    state = CacheState(target=t.label, kind=UNKNOWN, session_id=t.session_id,
                       adapter=t.adapter, bound_at=t.bound_at, bound_for=t.bound_for)
    if t.settled is not None:
        state.kind, state.reason, state.age_seconds = t.settled, t.reason, t.age_seconds
        state.ttl_seconds, state.key, state.cache_source = (t.ttl_seconds, t.key,
                                                            t.cache_source)
        state.code = t.code
        return state
    clock = clock_state(t.adapter or "claude-shim", t.session_id)
    state.kind, state.reason, state.age_seconds = clock.kind, clock.reason, clock.age_seconds
    state.ttl_seconds, state.key, state.code = clock.ttl_seconds, clock.key, clock.code
    return state


# --------------------------------------------------------------------------- wording

def span(seconds: float) -> str:
    """Compact, for the hold: `58m`, `2h10m`, `2d3h`."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def spoken_span(seconds: float) -> str:
    """For speech: `4 minutes`, `1 hour 10 minutes`, `2 days 3 hours`."""
    def unit(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return unit(minutes, "minute")
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return unit(hours, "hour") + (f" {unit(minutes, 'minute')}" if minutes else "")
    days, hours = divmod(hours, 24)
    return unit(days, "day") + (f" {unit(hours, 'hour')}" if hours else "")


def last_turn_clock(age_seconds: float, now: float | None = None) -> str:
    """The wall-clock time of the last turn, local time: `19:22`, or with the date when
    it was not today."""
    now = time.time() if now is None else now
    then = time.localtime(now - age_seconds)
    if time.strftime("%Y-%m-%d", then) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", then)
    return time.strftime("%b %d %H:%M", then)


def _key_parts(key: str | None) -> tuple:
    """`codex openai/gpt-5.6-sol (measured on this box)` -> ("openai", "gpt-5.6-sol",
    "measured on this box"). Anything else -> (None, None, None)."""
    if not key or " " not in key:
        return None, None, None
    _harness, _, rest = key.partition(" ")
    pair, _, note = rest.partition(" (")
    provider, _, model = pair.partition("/")
    return (provider or None, model or None, note.rstrip(")") or None)


def _basis(state: CacheState) -> str:
    """Why comms counts the lifetime it does, for the hold."""
    provider, _model, note = _key_parts(state.key)
    if note and note.startswith("measured"):
        if "lowered from" in note:
            lowered = note.split("lowered from ", 1)[1].split(" on ", 1)[0]
            return (f"measured on this box and lowered from {lowered} when a cache went "
                    "cold sooner")
        return "measured on this box"
    names = {"openai": "the minimum OpenAI documents",
             "anthropic": "the lifetime Anthropic documents"}
    return names.get(provider, "the lifetime its provider documents")


def _harness_agent(state: CacheState) -> bool:
    return state.adapter in ("codex", "opencode")


def hold_line(state: CacheState) -> str:
    """One held (cold) recipient's line: why it is held. It ends after its cold
    sentence.

    A cold read with both an age and a lifetime says how long ago it went cold: the age
    less the lifetime. A cold read with no lifetime (a changed model, a compressed
    rollout) says why it is counted cold and, where the age is known, when its last
    request was; it never prints a cold duration. Only a cold read is held: a read comms
    could not make refuses the send."""
    age, ttl = state.age_seconds, state.ttl_seconds
    if state.kind == COLD and age is not None and ttl is not None:
        if _harness_agent(state) and state.key:
            _provider, model, _note = _key_parts(state.key)
            return (f"held: {state.target}'s cache went cold {span(age - ttl)} ago (last "
                    f"request {last_turn_clock(age)}; on {model} comms counts a cache warm "
                    f"for {spoken_span(ttl)} after its last request, {_basis(state)}).")
        return (f"held: {state.target}'s cache went cold {span(age - ttl)} ago "
                f"(last turn {last_turn_clock(age)}).")
    why = state.reason or "comms has no cache lifetime to count it by"
    when = f" (last request {span(age)} ago)" if age is not None else ""
    return f"held: {state.target}'s cache is counted as cold: {why}{when}."


def _lifetime_words(state: CacheState) -> str:
    """`gpt-5.6-terra, 30 minutes after a request`, with a measured row's basis."""
    _provider, model, note = _key_parts(state.key)
    words = f"{model}, {spoken_span(state.ttl_seconds)} after a request"
    if note and note.startswith("measured"):
        words += f", {note}"
    return words


def spoken_line(state: CacheState) -> str:
    """One `moxi comms cache` line, sized to be spoken. No tables."""
    name = state.target
    if state.kind == NO_CACHE:
        reason = state.reason or ""
        if state.code in NO_CLOCK_CODES or (reason and reason == no_clock(state.adapter)):
            # No clock to count by (cache not installed, an adapter comms does not read,
            # a model with no lifetime row): read as no cache, so mail goes live.
            return (f"{name}: {reason or 'no clock to count by'}, so comms does not count "
                    "its cache, and agents' messages reach it.")
        if state.code == PERSON:
            return f"{name}: a person, no cache."
        if _harness_agent(state):
            provider, model, _note = _key_parts(state.key)
            pair = f"{provider}/{model}" if provider and model else "a free model"
            return (f"{name}: on a free model ({pair}), so comms does not count its cache, "
                    "and agents' messages reach it.")
        return f"{name}: a person, no cache."
    if state.kind == FRESH:
        if _harness_agent(state):
            return f"{name}: new session, no request yet, nothing cached."
        return f"{name}: new session, no turn yet, nothing cached."
    if state.kind == UNKNOWN:
        return (f"{name}: cache state not known ({state.reason}). Agents' messages to it "
                "are held.")
    age, ttl = state.age_seconds, state.ttl_seconds
    where = "window" if state.adapter == "opencode" else "session"
    if state.kind == COLD and (age is None or ttl is None):
        why = state.reason or "comms has no cache lifetime to count it by"
        last = f" Last request {spoken_span(age)} ago." if age is not None else ""
        return (f"{name}: cache counted as cold: {why}.{last} No agent can reach "
                f"it now; only the operator, in its {where}.")
    if state.kind == WARM:
        if age is None:
            return f"{name}: cache warm."
        if _harness_agent(state) and ttl is not None and state.key:
            return (f"{name}: cache warm, last request {spoken_span(age)} ago, counted warm "
                    f"for {spoken_span(ttl - age)} more ({_lifetime_words(state)}).")
        expiry = "" if ttl is None else f", expires in {spoken_span(ttl - age)}"
        return f"{name}: cache warm, last turn {spoken_span(age)} ago{expiry}."
    if _harness_agent(state) and state.key:
        return (f"{name}: cache expired {spoken_span(age - ttl)} ago "
                f"({_lifetime_words(state)}). No agent can reach it now; only "
                f"the operator, in its {where}.")
    line = f"{name}: cache expired {spoken_span(age - ttl)} ago."
    return line + " No agent can reach it now; only the operator, in its session."


__all__ = ["WARM", "COLD", "FRESH", "NO_CACHE", "UNKNOWN", "KINDS", "PASSING", "Clock",
           "clock_state", "module", "present", "describe_module", "required_refusal",
           "projects_root", "open_store", "close_store", "Target", "CacheState",
           "cache_state", "cache_states", "kind_of", "session_target", "target_for_name",
           "target_for_session_id", "hold_line", "spoken_line", "span", "spoken_span",
           "no_clock", "MISSING", "MODULE_ABSENT", "NO_CLOCK", "PERSON",
           "EVIDENCE_UNREADABLE", "EVIDENCE_FAILED", "READ_FAILED", "NO_ROW", "FREE",
           "UNKNOWN_HARNESS", "DRIFT_HELD", "NO_DRIFT_RECORD", "NO_CLOCK_CODES",
           "SESSION_ID_RE", "NOT_BOUND", "INSTALL"]
