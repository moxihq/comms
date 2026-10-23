"""The directory's secret column: `secret_id` in a directory this release creates, and
the older name kept in a directory created before it.

A directory keeps the column names it was created with. Every reader and writer takes the
column's name from the table it opens, so a directory made by the earlier release sends,
provisions and re-provisions here, and none of that changes its columns."""

import sqlite3

import pytest

from moxi_comms import directory, tokens
from moxi_comms.local_gateway import META_OPERATOR_TOKEN_HASH, LocalGateway
from moxi_comms.paths import directory_db, tokens_dir
from moxi_comms.provision import COMPLETE, add_standing, provision, readiness
from moxi_comms.sender import SenderIdentity, credential_for, post

#: The directory schema of comms 0.1.0, as that release wrote it. Only this test holds it.
SCHEMA_0_1_0 = """
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


def _webhook_columns(path) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return [tuple(row) for row in conn.execute("PRAGMA table_info(webhooks)")]
    finally:
        conn.close()


def _old_directory():
    """An empty directory as 0.1.0 created it."""
    path = directory_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA_0_1_0)
        conn.commit()
    finally:
        conn.close()
    path.chmod(0o600)
    return path


@pytest.fixture
def rig():
    gateway = LocalGateway(poll_interval=0.02)
    try:
        yield gateway, tokens.TokenStore(tokens_dir())
    finally:
        gateway.close()


def _send(gateway, sender: SenderIdentity, body: str) -> None:
    """Resolve the sender's credential through the directory and post with it."""
    db = directory.connect(directory_db(), read_only=True)
    try:
        cred, channel_id = credential_for(gateway, db, sender)
    finally:
        db.close()
    post(gateway, cred, body, username=sender.qualified)
    assert body in [m.content for m in gateway.history(channel_id)]


def _ready(gateway, store) -> str:
    return readiness(directory_db(), store=store,
                     operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH)).state


def test_a_fresh_directory_names_the_column_secret_id_and_sends(rig):
    gateway, store = rig
    provision(gateway, directory_db(), slots=2, standing=[("planner", "proj-a")],
              store=store)
    names = [column[1] for column in _webhook_columns(directory_db())]
    assert "secret_id" in names and directory.OLD_SECRET_COLUMN not in names
    conn = directory.connect(directory_db(), read_only=True)
    try:
        assert directory.secret_column(conn) == "secret_id"
        assert all(directory.secret_of(row).startswith("comms/")
                   for row in conn.execute("SELECT * FROM webhooks"))
    finally:
        conn.close()
    assert _ready(gateway, store) == COMPLETE
    _send(gateway, SenderIdentity.for_agent("planner", "proj-a"), "from a standing row")
    _send(gateway, SenderIdentity.for_slot(2, "agent-1@proj-a", "proj-a"), "from a slot")


def test_a_directory_from_0_1_0_keeps_its_columns_through_every_writer(rig):
    gateway, store = rig
    path = _old_directory()
    before = _webhook_columns(path)
    assert directory.OLD_SECRET_COLUMN in [column[1] for column in before]
    assert "secret_id" not in [column[1] for column in before]

    # First provisioning, then a re-run that adds a slot and a standing identity.
    provision(gateway, path, slots=2, standing=[("planner", "proj-a")], store=store)
    assert _webhook_columns(path) == before
    provision(gateway, path, slots=3, standing=[("planner", "proj-a"), ("archivist", "global")],
              store=store)
    assert _webhook_columns(path) == before
    conn = directory.init(path)
    try:
        add_standing(gateway, conn, "reviewer", "proj-b", store=store)
        assert directory.secret_column(conn) == directory.OLD_SECRET_COLUMN
    finally:
        conn.close()
    assert _webhook_columns(path) == before

    conn = directory.connect(path, read_only=True)
    try:
        rows = conn.execute("SELECT * FROM webhooks").fetchall()
    finally:
        conn.close()
    assert len(rows) == 3 + 3
    assert all(directory.secret_of(row).startswith("comms/") for row in rows)

    assert _ready(gateway, store) == COMPLETE
    _send(gateway, SenderIdentity.for_agent("planner", "proj-a"), "old directory, standing")
    _send(gateway, SenderIdentity.for_agent("archivist", "global"), "old directory, global")
    _send(gateway, SenderIdentity.for_agent("reviewer", "proj-b"), "old directory, added")
    _send(gateway, SenderIdentity.for_slot(3, "agent-2@proj-a", "proj-a"), "old directory, slot")
    assert _webhook_columns(path) == before


def test_readiness_finds_a_missing_slot_token_in_an_old_directory(rig):
    """The SQL reader names the old column too: a slot whose token is gone is found."""
    gateway, store = rig
    path = _old_directory()
    provision(gateway, path, slots=2, store=store)
    store.path_for(tokens.owner_for_slot(2)).unlink()
    found = readiness(path, store=store,
                      operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH))
    assert "slot-2" in found.detail and "slot-1" not in found.detail
    assert _webhook_columns(path)[5][1] == directory.OLD_SECRET_COLUMN


def test_the_schema_declares_secret_id():
    assert directory.SECRET_COLUMN == "secret_id"
    assert "secret_id " in directory.SCHEMA
    assert directory.OLD_SECRET_COLUMN not in directory.SCHEMA
