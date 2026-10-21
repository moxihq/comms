"""The adapter kinds: what a binding may register as, and how comms reads its cache.

A kind is declared by an entry point in the group `moxi.comms.kinds`, named by the kind,
whose target is a plain table (stdlib data, so a declaration imports nothing of comms):

    [project.entry-points."moxi.comms.kinds"]
    driver = "some_package.kinds:DRIVER"

    # some_package/kinds.py
    DRIVER = {"cache": "harness", "harness": "claude",
              "evidence": "some_package.kinds:driver_evidence"}
    SPEECH = {"cache": "person"}

`cache` is the kind's cache treatment:

* `person`: a person at the other end; its cache reads `no-cache` and its mail is never
  held for one.
* `harness`: a session of `harness` (`claude`, `codex` or `opencode`), with `evidence`, a
  `"package.module:function"` that says which session to read and whether the next
  delivery starts a new one (or that it cannot be read); comms' cache seam calls it and
  hands what it returns to cache's read, so the declaring module decides no verdict.

The evidence function is called as `evidence(qualified, session_id)`, with the binding's
qualified name and the session id its adapter reported (None when it reported none), at
every read of that binding's cache. It answers one of:

* `(session_id, new_session)`, a `str` and a `bool`: the session to read, and whether the
  next delivery starts a new session. The id names a session someone has run, so cache
  reads it with no root of its own and `turned=True`: a missing record is `unknown`. An
  empty id with `new_session=True` reads `fresh` (nothing cached to lose); an empty id
  without it reads `unknown`;
* `UNREADABLE` (`"unreadable"`): the kind's record exists but cannot be read, so the read
  is `unknown` (code `evidence-unreadable`).

Anything else, or an exception, reads `unknown` (code `evidence-failed`). The function
runs in the daemon's process at each read, so it is quick and reads only its own record.

comms' own kinds are declared the same way in its `pyproject.toml`: `claude-shim`,
`codex` and `opencode` (harness kinds whose evidence is their own register report) and
`send-only` (a name that receives nothing; comms' own treatment, which no module may
declare). They are also compiled in here (`BUILT_IN`), so a daemon in an environment
whose metadata predates the group still knows them, and a test holds the two copies
equal.

The daemon reads the group at its start (`load`) and refuses a register of any kind not
in it, as it refused an unknown kind before. A declaration that does not hold is left out
with its reason (`Registry.broken`), logged once, and the others are kept.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

log = logging.getLogger("comms.kinds")

#: The entry-point group.
GROUP = "moxi.comms.kinds"

#: The cache treatments. `send-only` is comms' own and is never declared by a module.
CACHE_HARNESS, CACHE_PERSON, CACHE_SEND_ONLY = "harness", "person", "send-only"
#: The harnesses a harness kind may name: the ones cache has a clock for.
HARNESSES = ("claude", "codex", "opencode")

#: A kind's name: lower-case letters, digits and dashes, starting with a letter.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_TARGET = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")

# comms' own declarations, which its pyproject.toml names under the group.
CLAUDE_SHIM = {"cache": CACHE_HARNESS, "harness": "claude"}
CODEX = {"cache": CACHE_HARNESS, "harness": "codex"}
OPENCODE = {"cache": CACHE_HARNESS, "harness": "opencode"}
SEND_ONLY = {"cache": CACHE_SEND_ONLY}

#: The built-in kinds, by name, in the order the daemon has always listed them.
BUILT_IN = {"claude-shim": CLAUDE_SHIM, "codex": CODEX, "opencode": OPENCODE,
            "send-only": SEND_ONLY}

#: What an evidence function answers when its record exists but cannot be read.
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Kind:
    """One accepted kind. `evidence` is None for the built-ins."""

    name: str
    cache: str
    harness: str | None = None
    evidence: str | None = None
    built_in: bool = False


@dataclass(frozen=True)
class Broken:
    """A declaration left out, and why."""

    name: str
    reason: str


@dataclass(frozen=True)
class Registry:
    """The kinds the daemon accepts, by name, and the declarations it left out."""

    kinds: tuple = ()
    broken: tuple = ()

    def names(self) -> tuple:
        return tuple(k.name for k in self.kinds)

    def get(self, name) -> Kind | None:
        return next((k for k in self.kinds if k.name == name), None)

    def __contains__(self, name) -> bool:
        return self.get(name) is not None


def _built_in() -> list:
    return [Kind(name, d["cache"], d.get("harness"), None, True)
            for name, d in BUILT_IN.items()]


def built_in() -> Registry:
    """comms' own kinds alone, read from nothing: what a read with no daemon knows."""
    return Registry(tuple(_built_in()))


def check(name: str, declared) -> Kind | str:
    """One module's declaration as a `Kind`, or the reason it does not hold."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return f"{name!r} is not a kind name (lower-case letters, digits and dashes)"
    if not isinstance(declared, Mapping):
        return f"its declaration is a {type(declared).__name__}, not a table"
    unknown = set(declared) - {"cache", "harness", "evidence"}
    if unknown:
        return f"its declaration has keys comms does not read: {', '.join(sorted(unknown))}"
    cache = declared.get("cache")
    if cache == CACHE_PERSON:
        if "harness" in declared or "evidence" in declared:
            return "a person kind names no harness and no evidence"
        return Kind(name, CACHE_PERSON)
    if cache == CACHE_HARNESS:
        harness, evidence = declared.get("harness"), declared.get("evidence")
        if harness not in HARNESSES:
            return f"its harness is {harness!r}; it must be one of {', '.join(HARNESSES)}"
        if not isinstance(evidence, str) or not _TARGET.match(evidence):
            return ("a harness kind names its evidence function as "
                    "\"package.module:function\"")
        return Kind(name, CACHE_HARNESS, harness, evidence)
    return f"its cache treatment is {cache!r}; it must be {CACHE_PERSON} or {CACHE_HARNESS}"


def load(entries: Iterable | None = None) -> Registry:
    """The built-in kinds, then each kind the group declares (or `entries`, each with
    `.name` and `.load()`, for tests), each checked. Never raises.

    A module entry named like a built-in kind is comms' own declaration when it equals
    the built-in table, and is left out otherwise; two entries of one name are both left
    out."""
    if entries is None:
        try:
            from importlib.metadata import entry_points

            entries = list(entry_points(group=GROUP))
        except Exception as exc:  # noqa: BLE001 - a broken environment costs module kinds
            log.warning("reading the %s group failed: %s", GROUP, exc)
            entries = []
    kinds, broken = _built_in(), []
    by_name: dict[str, list] = {}
    for ep in entries:
        by_name.setdefault(ep.name, []).append(ep)
    for name in sorted(by_name):
        claims = by_name[name]
        if len(claims) > 1 and name not in BUILT_IN:
            broken.append(Broken(name, f"{len(claims)} entry points declare it; keep one"))
            continue
        for ep in claims:
            try:
                declared = ep.load()
            except Exception as exc:  # noqa: BLE001
                broken.append(Broken(name, f"its declaration does not load: "
                                           f"{type(exc).__name__}: {exc}"))
                continue
            if name in BUILT_IN:
                same = isinstance(declared, Mapping) and dict(declared) == BUILT_IN[name]
                if not same:
                    broken.append(Broken(name, "it is a built-in kind of comms, which no "
                                               "module may declare"))
                continue
            got = check(name, declared)
            if isinstance(got, Kind):
                kinds.append(got)
            else:
                broken.append(Broken(name, got))
    for b in broken:
        log.warning("adapter kind %s is left out: %s", b.name, b.reason)
    return Registry(tuple(kinds), tuple(broken))
