"""directory.db -- the provenance anchor.

Two requirements this module honours:

  * every connection sets ``PRAGMA foreign_keys=ON`` (SQLite FKs are inert otherwise);
  * the file is mode 0600, owned by the daemon/provisioner user context. Every other
    consumer opens read-only.

The daemon consults this per claim -- never a boot-time cache.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (   -- standing identities, and retired ones
    name        TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'global',
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent','send-only')),
    PRIMARY KEY (name, scope)
);
CREATE TABLE IF NOT EXISTS slots (    -- claim containers (webhook bundles); not addresses
    slot_id     INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (    -- no 'inbox' kind; rooms have no owner
    channel_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('names')),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhooks ( -- posting credentials; THE provenance anchor
    webhook_id    TEXT PRIMARY KEY,
    slot_id       INTEGER REFERENCES slots(slot_id),
    agent_name    TEXT,
    agent_scope   TEXT,
    channel_id    TEXT NOT NULL REFERENCES rooms(channel_id),
    bws_secret_id TEXT NOT NULL,      -- pointer only; URLs never touch disk
    created_at    TEXT NOT NULL,
    FOREIGN KEY (agent_name, agent_scope) REFERENCES agents(name, scope),
    CHECK ((slot_id IS NULL) <> (agent_name IS NULL)),
    CHECK ((agent_name IS NULL) = (agent_scope IS NULL)),
    UNIQUE (slot_id, channel_id),
    UNIQUE (agent_name, agent_scope, channel_id)
);
CREATE TABLE IF NOT EXISTS meta (     -- e.g. 'writer' -> the one named directory writer
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operators (
    user_id     TEXT PRIMARY KEY,     -- the operator's user id
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

# meta keys
META_WRITER = "writer"

#: A standing row's `kind`. `agent` is every row before send-only identities existed and
#: every row since that is not one. A `send-only` row is a name that sends and is never a
#: recipient: nothing is delivered to it, deferred for it or held for it, bound or not.
KIND_AGENT = "agent"
KIND_SEND_ONLY = "send-only"
ROW_KINDS = (KIND_AGENT, KIND_SEND_ONLY)

_ADD_KIND = ("ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent' "
             "CHECK (kind IN ('agent','send-only'))")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open directory.db. ALWAYS sets PRAGMA foreign_keys=ON (build requirement)."""
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | Path) -> sqlite3.Connection:
    """Create the schema; the file is mode 0600 (integrity is file ownership, not creds)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = connect(path)
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    if fresh or (path.stat().st_mode & 0o777) != 0o600:
        os.chmod(path, 0o600)
    return conn


def migrate(conn) -> bool:
    """Bring a directory written before the `kind` column up to the schema. Answers
    whether it changed anything. Every existing row reads `agent`, which is what it was.

    Only a writer runs this (`init`, the provisioner's open). The daemon and the verbs
    open the file read-only and read a missing column as `agent` (`kind_of`), so a box
    whose directory has not been migrated yet routes exactly as before."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
    if "kind" in columns:
        return False
    conn.execute(_ADD_KIND)
    return True


# --------------------------------------------------------------------------- reads

def room_by_name(conn, name: str):
    return conn.execute("SELECT * FROM rooms WHERE name = ?", (name,)).fetchone()



def standing_row(conn, name: str, scope: str):
    return conn.execute(
        "SELECT * FROM agents WHERE name = ? AND scope = ? AND status = 'active'",
        (name, scope),
    ).fetchone()


def kind_of(row) -> str:
    """A standing row's kind; `agent` for a row read from a directory with no column."""
    if row is None:
        return KIND_AGENT
    return row["kind"] if "kind" in row.keys() else KIND_AGENT


def is_send_only(conn, name: str, scope: str) -> bool:
    """Is there an active standing row at exactly (name, scope) whose kind is send-only?
    Read per call, like every directory fact the daemon uses."""
    return kind_of(standing_row(conn, name, scope)) == KIND_SEND_ONLY


def retired(conn, name: str, scope: str) -> bool:
    """Is (name, scope) retired: an `agents` row at exactly (name, scope) whose status is
    `retired`? A retired identity is one comms no longer binds, routes to or queues for;
    its journal rows, its messages in the record, its cursor file and its card stay. Read
    per call, like every directory fact the daemon uses."""
    return conn.execute(
        "SELECT 1 FROM agents WHERE name = ? AND scope = ? AND status = 'retired'",
        (name, scope),
    ).fetchone() is not None


#: The refusals for a retired identity, word for word: the register's, and resolution's
#: (send, reply, --check, explain, cache). `{q}` is the retired identity as `qualify`
#: spells it, `{name}` the bare name of the global identity to use instead.
RETIRED_BIND = ("{q} is retired: comms keeps a project suffix only to tell apart two agents "
                "of one name in different projects, and {q} had no such twin. Bind {name} "
                "instead, with no --scope.")
RETIRED_SEND = ("{q} is retired: comms keeps a project suffix only to tell apart two agents "
                "of one name in different projects, and {q} had no such twin. Send to "
                "{name} instead. Nothing was sent.")


def standing_rows(conn):
    return conn.execute("SELECT * FROM agents WHERE status = 'active'").fetchall()


def slot_ids(conn) -> list[int]:
    return [r["slot_id"] for r in conn.execute("SELECT slot_id FROM slots ORDER BY slot_id")]


def webhook(conn, webhook_id: str):
    return conn.execute("SELECT * FROM webhooks WHERE webhook_id = ?", (webhook_id,)).fetchone()


def slot_webhook(conn, slot_id: int, channel_id: str):
    return conn.execute(
        "SELECT * FROM webhooks WHERE slot_id = ? AND channel_id = ?", (slot_id, channel_id)
    ).fetchone()


def agent_webhook(conn, name: str, scope: str, channel_id: str):
    return conn.execute(
        "SELECT * FROM webhooks WHERE agent_name = ? AND agent_scope = ? AND channel_id = ?",
        (name, scope, channel_id),
    ).fetchone()


def operator(conn, user_id: str):
    return conn.execute("SELECT * FROM operators WHERE user_id = ?", (user_id,)).fetchone()


def meta_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now()),
    )
