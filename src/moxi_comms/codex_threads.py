"""Thread-id <-> identity, one file per identity (STATE/comms/codex/<qualified>.json).

The file holds the thread of the identity's CURRENT launch. A launch is one
`moxi-comms-codex` process: its first start always starts a new thread with `thread/start`
and records the id here, and the adapter never resumes a thread an earlier launch
recorded (operator ruling, 2026-10-08: a Codex relaunch starts a fresh conversation, as
a Claude relaunch does). Inside one launch the adapter resumes
only the thread that launch started, which it holds in memory, and never replaces it:
a resume that fails is loud and retried. No launch reads a thread id from this file;
it is read only for the keys kept beside the launch's own id (`rollout`, `layer_one`),
and only when the file names that id. An earlier launch's thread stays in Codex's own
session store.

The ADAPTER writes this file and the daemon never does -- the same category as the
cursor, and it lives beside the cursors for that reason. It is not routing (routing is
who is connected right now, and stays in daemon memory).

Written the same way cursors are: whole file, atomic replace, never appended to.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .paths import codex_threads_dir


def path(qualified: str) -> Path:
    return codex_threads_dir() / f"{qualified}.json"


def read(qualified: str) -> str | None:
    """The thread id the record names, or None. For readers outside a launch (the
    operator, the smoke script); no launch calls it."""
    record = read_meta(qualified)
    return record.get("thread_id") if record else None


def read_meta(qualified: str) -> dict | None:
    p = path(qualified)
    if not p.exists():
        return None
    try:
        record = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return record if isinstance(record, dict) else None


#: What one read of the record found: no record, a thread id, or a file that exists but
#: could not be read as one. Only `NONE` is evidence that a thread the launcher then
#: starts is new.
NONE, RECORDED, UNREADABLE = "none", "recorded", "unreadable"


def read_outcome(qualified: str) -> tuple:
    """(NONE | RECORDED | UNREADABLE, the thread id or None).

    NONE: the file does not exist, or it parsed as a JSON object with no `thread_id`.
    RECORDED: it parsed and holds an id. UNREADABLE: it exists but the read failed, it
    did not parse as a JSON object, or its `thread_id` is not a string. `read_meta`
    folds the last into "none"; this read keeps them apart."""
    p = path(qualified)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return NONE, None
    except OSError:
        return UNREADABLE, None
    try:
        record = json.loads(text)
    except ValueError:
        return UNREADABLE, None
    if not isinstance(record, dict):
        return UNREADABLE, None
    thread_id = record.get("thread_id")
    if thread_id is None or thread_id == "":
        return NONE, None
    if not isinstance(thread_id, str):
        return UNREADABLE, None
    return RECORDED, thread_id


def write(qualified: str, thread_id: str, **extra) -> str:
    """Record the thread id. Returns it, so callers read as `tid = write(q, tid)`."""
    record = {"thread_id": str(thread_id),
              "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              **extra}
    target = path(qualified)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{qualified}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return record["thread_id"]


def update(qualified: str, thread_id: str, **fields) -> dict | None:
    """Rewrite the record with `fields` merged over what it holds, keeping the thread
    id and every other key, but only when the record names `thread_id`, the caller's
    own thread. Returns the new record, or None, writing nothing, when there is no
    record or it names another thread (another launch's)."""
    record = read_meta(qualified)
    if not record or not thread_id or record.get("thread_id") != thread_id:
        return None
    extra = {k: v for k, v in record.items() if k not in ("thread_id", "updated_at")}
    extra.update(fields)
    write(qualified, thread_id, **extra)
    return read_meta(qualified)


__all__ = ["path", "read", "read_meta", "read_outcome", "write", "update",
           "NONE", "RECORDED", "UNREADABLE"]
