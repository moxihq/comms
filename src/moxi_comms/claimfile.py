"""Claim files: where each adapter writes what it holds, before it answers.

One file per claim holder, 0600 in the 0700 runtime dir, written whole and atomically on
every change. The file is found again by whatever outlives the adapter's process inside
one session, and by nothing else:

  * the Claude shim: `comms-claim-<session>.json`, keyed by the session id
    `moxi-comms-claude` launched with (the key of its control socket too), so a respawned
    shim inside the same Claude session comes back in the recorded state;
  * a harness adapter (moxi-comms-codex, moxi-comms-opencode):
    `comms-claim-<pid>-<start>.json`, keyed by its own pid and that pid's start time,
    since the adapter is its session's top process and a respawn is a new launch;
  * a `ClaimServer` binding: `comms-claim-srv-<pid>-<start>-<qualified>.json`.

A file is LIVE when its socket answers, or failing that when one of its `tree` roots is
alive (the recorded pid with the recorded start time). A clean shutdown deletes the
file; the runtime dir is cleared at boot.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .membership import DEFAULT_PROC, TREE, ProcView, Root
from .paths import runtime_root
from .protocol import PROBE_LIVE, probe

PREFIX, SUFFIX = "comms-claim-", ".json"

#: The adapter states. A process speaks as a claim only in `bound` and `offline`.
#: A file in any other state (an old file's `passing`) loads as `standby`.
BOUND, OFFLINE, WAITING, STANDBY = "bound", "offline", "waiting", "standby"
STATES = (BOUND, OFFLINE, WAITING, STANDBY)
HOLDS = (BOUND, OFFLINE)


def claim_dir() -> Path:
    return runtime_root()


def file_for(key: str) -> Path:
    return claim_dir() / f"{PREFIX}{key}{SUFFIX}"


def key_of(path) -> str:
    name = Path(path).name
    return name[len(PREFIX):-len(SUFFIX)]


def write(key: str, record: dict) -> Path:
    """Whole file, atomic replace, 0600 in a 0700 dir."""
    target = file_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    tmp = target.parent / f".claim.{os.getpid()}.{threading.get_ident()}.{key}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return target


def read(key: str) -> dict | None:
    return read_path(file_for(key))


def read_path(path) -> dict | None:
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def remove(key: str) -> None:
    try:
        file_for(key).unlink()
    except FileNotFoundError:
        pass


def all_records() -> list:
    """Every claim file readable in the runtime dir, as (key, record)."""
    out = []
    try:
        entries = sorted(claim_dir().glob(f"{PREFIX}*{SUFFIX}"))
    except OSError:
        return out
    for p in entries:
        record = read_path(p)
        if record is not None:
            out.append((key_of(p), record))
    return out


def roots_of(record: dict) -> list:
    return [r for r in (Root.from_dict(x) for x in record.get("roots") or ()) if r]


def socket_answers(record: dict) -> bool:
    sock = record.get("socket")
    if not sock:
        return False
    try:
        verdict, _ = probe(Path(sock))
    except Exception:                                           # noqa: BLE001
        return False
    return verdict == PROBE_LIVE


def tree_root_alive(record: dict, proc: ProcView | None = None) -> bool:
    proc = proc or DEFAULT_PROC
    return any(r.reach == TREE and proc.alive(r.pid, r.start) for r in roots_of(record))


def is_live(record: dict | None, proc: ProcView | None = None) -> bool:
    if not record:
        return False
    return socket_answers(record) or tree_root_alive(record, proc)


def live_records(proc: ProcView | None = None) -> list:
    return [(k, r) for k, r in all_records() if is_live(r, proc)]


def membership_table(records) -> list:
    """`(key, roots)` for `membership.sessions_of`."""
    return [(k, roots_of(r)) for k, r in records]


def held_identity(record: dict | None) -> str | None:
    """The comms identity a record holds (a claim file, or a `claim` op's answer): its
    claim's qualified name while its state is one that holds, and only when the claim
    names an identity, a qualified name and a bare name both. A claim that names none
    (an auto-named launch before its first bind, or through a daemon outage when it
    never bound) holds no comms identity in any state. None when it holds none."""
    if not record or record.get("state") not in HOLDS:
        return None
    claim = record.get("claim") or {}
    if not claim.get("qualified") or not claim.get("name"):
        return None
    return claim["qualified"]


def names_identity(record: dict, qualified: str) -> bool:
    """Does this file's claim name `qualified`, in any state (a waiting launch's
    included)? The interim stamp refusal counts it, not only a claim that may bind."""
    return (record.get("claim") or {}).get("qualified") == qualified


def sweep_dead_harness_files(proc: ProcView | None = None) -> list:
    """Remove harness files whose pid and start time no longer match a live process
    (a start ignores and removes them). Returns the keys removed."""
    proc = proc or DEFAULT_PROC
    gone = []
    for key, record in all_records():
        if record.get("keying") != "pid":
            continue
        roots = roots_of(record)
        if roots and not any(proc.alive(r.pid, r.start) for r in roots):
            remove(key)
            gone.append(key)
    return gone


__all__ = ["BOUND", "HOLDS", "OFFLINE", "STANDBY", "STATES", "WAITING", "all_records",
           "claim_dir", "file_for", "held_identity", "is_live", "key_of",
           "live_records", "membership_table", "names_identity", "read", "read_path",
           "remove", "roots_of", "socket_answers", "sweep_dead_harness_files",
           "tree_root_alive", "write"]
