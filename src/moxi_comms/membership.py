"""Which session a process is in: the membership rule.

A binding records its session's ROOTS in its claim file (`claimfile`), each a pid, that
pid's start time (field 22 of `/proc/<pid>/stat`) and a reach: `tree` (the root and
everything descended from it) or `self` (that one process only). A process is inside
the session of the first root met walking from the process itself up its ancestry,
among the roots of every live claim file, where a `self` root counts only for the
process itself. A process that meets no root is inside no session.

So descent alone never decides: sessions nest, and a socket somewhere in a process's
ancestry does not admit it. A pid whose start time differs from the recorded one is not
that root, so a reused pid neither admits a stranger nor keeps a dead session's file
live.

Everything here reads `/proc` and the claim files in the runtime dir, nothing on the
daemon. `ProcView` is the one reader of `/proc`; a test hands in its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

TREE, SELF = "tree", "self"
REACHES = (TREE, SELF)


@dataclass(frozen=True)
class Root:
    pid: int
    start: str
    reach: str = TREE

    def as_dict(self) -> dict:
        return {"pid": self.pid, "start": self.start, "reach": self.reach}

    @classmethod
    def from_dict(cls, raw) -> "Root | None":
        if not isinstance(raw, dict):
            return None
        pid, start, reach = raw.get("pid"), raw.get("start"), raw.get("reach", TREE)
        if (isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(start, str)
                or reach not in REACHES):
            return None
        return cls(pid, start, reach)


class ProcView:
    """Reads a process's parent and start time from `/proc`. `stat(pid)` answers
    `(ppid, start)` or None when there is no such process (or it cannot be read)."""

    def __init__(self, root: str = "/proc"):
        self.root = root

    def stat(self, pid: int):
        try:
            with open(f"{self.root}/{int(pid)}/stat", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return None
        # The command name sits in parentheses and may itself hold spaces or parens;
        # everything after the LAST `)` is space-separated, starting at field 3.
        tail = raw[raw.rfind(")") + 1:].split()
        try:
            return int(tail[1]), tail[19]
        except (IndexError, ValueError):
            return None

    def start_time(self, pid: int) -> str | None:
        st = self.stat(pid)
        return None if st is None else st[1]

    def parent(self, pid: int) -> int | None:
        st = self.stat(pid)
        return None if st is None else st[0]

    def alive(self, pid: int, start: str) -> bool:
        return self.start_time(pid) == start

    def ancestry(self, pid: int, limit: int = 4096):
        """The process itself, then each ancestor up to (not including) pid 0."""
        seen = set()
        while pid and pid > 0 and pid not in seen and len(seen) < limit:
            seen.add(pid)
            st = self.stat(pid)
            if st is None:
                return
            yield pid, st[1]
            pid = st[0]


DEFAULT_PROC = ProcView()


def root_of(pid: int, reach: str = TREE, proc: ProcView | None = None) -> Root | None:
    """A root for a running process, with its start time read now."""
    start = (proc or DEFAULT_PROC).start_time(pid)
    return None if start is None else Root(int(pid), start, reach)


def sessions_of(pid: int, files, proc: ProcView | None = None) -> list:
    """The claim-file keys whose session `pid` is inside: the files holding a root at
    the nearest level of `pid`'s ancestry that holds any. More than one key only for a
    holder that is a `self` root of several bindings (a module's own loop holding
    several names, a `ClaimServer` per binding). `files` is an iterable of `(key, roots)`, roots as `Root`s.
    An empty list: inside no session."""
    proc = proc or DEFAULT_PROC
    table = [(key, [r for r in roots if r is not None]) for key, roots in files]
    for depth, (at, start) in enumerate(proc.ancestry(pid)):
        hits = []
        for key, roots in table:
            for r in roots:
                if r.pid != at or r.start != start:
                    continue
                if r.reach == SELF and depth != 0:
                    continue                  # a self root admits only itself
                hits.append(key)
                break
        if hits:
            return hits
    return []


def inside(pid: int, key: str, files, proc: ProcView | None = None) -> bool:
    """Is `pid` inside the session whose claim file is `key`?"""
    return key in sessions_of(pid, files, proc)


__all__ = ["DEFAULT_PROC", "REACHES", "SELF", "TREE", "ProcView", "Root", "inside",
           "root_of", "sessions_of"]
