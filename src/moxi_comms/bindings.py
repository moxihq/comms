"""bindings.db -- the binding custody journal.

The daemon's routing table is memory only and dies with the socket; that is the
contract and it is right. But the ONE fact the daemon alone witnesses -- *who held
seat N over which interval* -- is also the missing half of replay attribution, and
losing it meant every chosen-tier agent's backlog arrived blanket-unverified. Chosen
agents hand off across sessions as normal workflow, so "unverified" was the common
case rather than the exceptional one.

So custody is JOURNALLED, durably, and replay runs the SAME match-gate live delivery
runs, retroactively:

    a message posted by seat N's webhook at time T
  + the journal shows exactly one holder of seat N over an interval containing T
  + that holder's qualified name == the display the record kept
  --------------------------------------------------------------------------------
  = VERIFIED. Two independent facts agreeing, exactly as live.

Anything less -- no journal, no interval, more than one interval, a display that does
not match the holder -- is the display-derived unverified path. Ambiguity
degrades toward unverified and never toward a false verified.

What this file is NOT: it is never consulted for routing or addressability. A journal
row is history, not a live seat, and nothing here can make a dead agent addressable. It
is read for whether an identity has existed, by the send's never-bound check only
(`ever_bound`, and `known_scopes` for that refusal's hint); it makes nothing
addressable.

Posture, mirroring directory.db: DATA (device-agnostic -- the journal interprets the
record, and the record is not a device's), mode 0600, written by the daemon alone,
every other consumer opens read-only. Journal mode is WAL, which is the whole reason
a read-only consumer can read the file while the daemon holds it open: a rollback
journal would hand a concurrent reader a lock error at exactly the moment a bind is
being written, and an attribution read that fails is an attribution read that lies by
omission.

Best-effort by construction: every operation here swallows its own failures and says
so in the log. Custody history is EVIDENCE; delivery is the product. A journal that
cannot be opened costs verification and nothing else.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .paths import bindings_db

log = logging.getLogger("comms.bindings")

#: The record's timestamp format, the one the gateway normalizes timestamps into.
#: Containment is a string comparison because this format sorts chronologically.
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    id           INTEGER PRIMARY KEY,
    qualified    TEXT NOT NULL,     -- the match-gate compares THIS to the display
    name         TEXT NOT NULL,
    scope        TEXT NOT NULL,
    tier         TEXT NOT NULL,     -- the tier a verified match reports
    slot_id      INTEGER,           -- the seat; NULL for send-only (no slot), never matched
    adapter      TEXT NOT NULL,
    bound_at     TEXT NOT NULL,     -- %Y-%m-%dT%H:%M:%SZ, UTC
    released_at  TEXT,              -- NULL while the binding is LIVE
    fenced       INTEGER NOT NULL DEFAULT 0   -- closed by a boot fence, not by a release
);
CREATE INDEX IF NOT EXISTS bindings_by_slot ON bindings(slot_id, bound_at);
"""


def stamp(t: float) -> str:
    """Epoch seconds -> the record's own timestamp spelling."""
    return time.strftime(TIMESTAMP_FMT, time.gmtime(t))


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init(path: str | Path) -> sqlite3.Connection:
    """Create the journal, 0600, in WAL. The daemon's connection, and only its."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    # chmod BEFORE WAL: SQLite gives the -wal/-shm sidecars the main file's mode, so
    # getting the order right is what keeps the sidecars 0600 too.
    if (path.stat().st_mode & 0o777) != 0o600:
        os.chmod(path, 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ------------------------------------------------------------------------- the reader

def holder_at(slot_id, timestamp: str, path: str | Path | None = None):
    """Who held `slot_id` at `timestamp` -- or None, which means "no evidence".

    ONE covering interval or nothing. Two overlapping intervals cannot both be true and
    the journal cannot say which is, so the answer is no answer: the caller falls back
    to the display-derived label, which is what it would have had anyway.

    Containment is exact, with NO tolerance window: `bound_at <= T <= released_at`. A
    message a second outside the interval is unverified and says so. A fudge factor here
    would be a second, quieter, undeclared rule about who counts as the holder.

    Never raises. A missing or unreadable journal is not an error condition, it is the
    absence of evidence, and attribution already knows what to do with that.
    """
    if slot_id is None or not timestamp:
        return None
    try:
        conn = connect(path or bindings_db(), read_only=True)
    except (sqlite3.Error, OSError):
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM bindings WHERE slot_id = ? AND bound_at <= ? "
            "AND (released_at IS NULL OR released_at >= ?)",
            (int(slot_id), timestamp, timestamp)).fetchall()
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        conn.close()
    return rows[0] if len(rows) == 1 else None


# ------------------------------------------------------------------------- the writer

class Journal:
    """The daemon's custody writer: bind, release, and the boot fence. Nothing else.

    Every method is failure-swallowing on purpose (see the module docstring): a write
    that fails is logged and the binding proceeds. The alternative -- refusing to bind
    an agent because a history file would not open -- trades the product for its
    evidence.
    """

    def __init__(self, path: str | Path | None = None, *, clock=time.time):
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _resolved(self) -> Path:
        return self.path if self.path is not None else bindings_db()

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> bool:
        """Open (creating) the journal. False means custody goes unrecorded this run."""
        with self._lock:
            if self._conn is not None:
                return True
            try:
                self._conn = init(self._resolved())
            except (sqlite3.Error, OSError) as e:
                log.warning("custody journal unavailable (%s: %s); bindings will not be "
                            "journalled and replay attribution stays unverified",
                            type(e).__name__, e)
                self._conn = None
                return False
            return True

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # -- the boot fence -----------------------------------------------------
    def fence(self, at: float | None = None) -> int:
        """Close every interval left open by a daemon that did not shut down cleanly.

        This is SAFE rather than approximate. While the daemon is down no new binding
        can exist and no moxi comms send can resolve, so nothing competes for the seat in the
        gap: the holder at the moment of the crash is the last holder there was, and the
        fence records that the interval ENDED without witnessing the release (`fenced=1`)
        rather than pretending the binding ran to now.

        The declared residual: an out-of-band spend of the seat's webhook DURING the gap,
        wearing the fenced holder's display, verifies against the fenced tail. That is
        the same trust-local caveat live attribution already carries -- a credential
        holder who spends it under someone's name is indistinguishable from that someone,
        and the journal was never going to be the thing that fixed that.
        """
        with self._lock:
            if self._conn is None:
                return 0
            marker = stamp(self.clock() if at is None else at)
            try:
                cur = self._conn.execute(
                    "UPDATE bindings SET released_at = ?, fenced = 1 "
                    "WHERE released_at IS NULL", (marker,))
                self._conn.commit()
            except sqlite3.Error:
                log.exception("boot fence failed; dangling custody intervals remain open")
                return 0
            return cur.rowcount or 0

    # -- the two moments only the daemon witnesses --------------------------
    def bind(self, *, qualified: str, name: str, scope: str, tier: str, slot_id,
             adapter: str, at: float | None = None) -> int | None:
        """Open an interval. Returns the row id to close later, or None if unrecorded."""
        with self._lock:
            if self._conn is None:
                return None
            try:
                cur = self._conn.execute(
                    "INSERT INTO bindings(qualified, name, scope, tier, slot_id, adapter,"
                    " bound_at) VALUES (?,?,?,?,?,?,?)",
                    (qualified, name, scope, tier,
                     None if slot_id is None else int(slot_id), adapter,
                     stamp(self.clock() if at is None else at)))
                self._conn.commit()
            except sqlite3.Error:
                # logged, never fatal: the binding is real whether or not it is recorded
                log.exception("custody journal: failed to record the bind of %s", qualified)
                return None
            return cur.lastrowid

    def release(self, row_id: int | None, at: float | None = None) -> None:
        """Close an interval at the connection-close deregistration."""
        if row_id is None:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "UPDATE bindings SET released_at = ? WHERE id = ? "
                    "AND released_at IS NULL",
                    (stamp(self.clock() if at is None else at), int(row_id)))
                self._conn.commit()
            except sqlite3.Error:
                log.exception("custody journal: failed to record the release of row %s",
                              row_id)

    # -- read-back (the daemon's own view; consumers use `holder_at`) --------
    def ever_bound(self, name: str, scope: str) -> bool | None:
        """Has the identity (name, scope) ever been bound on this box? None when the
        journal is not open or the read fails: the caller then does not know, and the
        send's never-bound check does not run."""
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM bindings WHERE name = ? AND scope = ? LIMIT 1",
                    (name, scope)).fetchone()
            except sqlite3.Error:
                return None
            return row is not None

    def known_scopes(self, name: str) -> list:
        """Every scope this NAME has ever been bound in, most recent first. Read only for
        the never-bound refusal's hint (the identities of that name that did exist);
        never to decide which identity a name means."""
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT scope, MAX(id) AS last FROM bindings WHERE name = ? "
                    "GROUP BY scope ORDER BY last DESC", (name,)).fetchall()
            except sqlite3.Error:
                return []
            return [str(r["scope"]) for r in rows]

    def intervals(self) -> list:
        with self._lock:
            if self._conn is None:
                return []
            try:
                return self._conn.execute(
                    "SELECT * FROM bindings ORDER BY id").fetchall()
            except sqlite3.Error:
                return []


__all__ = ["Journal", "SCHEMA", "TIMESTAMP_FMT", "connect", "holder_at", "init", "stamp"]
