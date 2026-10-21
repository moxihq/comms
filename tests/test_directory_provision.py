"""directory.db + provisioning."""

import os

import pytest

from moxi_comms import directory
from moxi_comms.mock_gateway import MockGateway
from moxi_comms.paths import directory_db
from moxi_comms.provision import (NAMES, ROOM_KINDS, ProvisionError, add_standing,
                                  provision)


def test_schema_and_file_posture():
    gw = MockGateway()
    provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")])
    assert oct(os.stat(directory_db()).st_mode & 0o777) == "0o600"
    conn = directory.connect(directory_db())
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"agents", "slots", "rooms", "webhooks", "meta", "operators"} <= tables
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_foreign_keys_are_live_not_inert():
    gw = MockGateway()
    provision(gw, directory_db(), slots=1)
    conn = directory.connect(directory_db())
    try:
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO webhooks(webhook_id, slot_id, agent_name, agent_scope, "
                "channel_id, bws_secret_id, created_at) VALUES ('x',1,NULL,NULL,'nope','s','t')")
            conn.commit()
    finally:
        conn.close()


def test_exactly_one_owner_kind():
    gw = MockGateway()
    p = provision(gw, directory_db(), slots=1)
    conn = directory.connect(directory_db())
    try:
        with pytest.raises(Exception):   # neither owner
            conn.execute(
                "INSERT INTO webhooks(webhook_id, slot_id, agent_name, agent_scope, "
                "channel_id, bws_secret_id, created_at) VALUES ('y',NULL,NULL,NULL,?,'s','t')",
                (p.rooms[NAMES],))
    finally:
        conn.close()


def test_one_room_and_a_slot_bundle_is_one_names_webhook():
    gw = MockGateway()
    p = provision(gw, directory_db(), slots=3)
    assert list(p.rooms) == [NAMES]
    conn = directory.connect(directory_db(), read_only=True)
    try:
        assert {r["kind"] for r in conn.execute("SELECT kind FROM rooms")} == {"names"}
        assert directory.meta_get(conn, "reader_principal") is None, "no reader principal"
        for slot_id in (1, 2, 3):
            rows = conn.execute("SELECT channel_id FROM webhooks WHERE slot_id=?",
                                (slot_id,)).fetchall()
            assert [r["channel_id"] for r in rows] == [p.rooms[NAMES]], \
                "a slot bundle is exactly one #names webhook"
    finally:
        conn.close()


@pytest.mark.parametrize("room, kind", [("#conference", "conference"),
                                        ("#notices", "notices")])
def test_a_new_directory_refuses_a_freed_room_row(room, kind):
    """The schema's CHECK names the one room provision makes, for a new file."""
    import sqlite3

    conn = directory.init(directory_db())
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rooms(channel_id, name, kind, created_at) "
                         "VALUES ('c', ?, ?, 't')", (room, kind))
    finally:
        conn.close()


def _old_directory(gw, *, slots=1):
    """A directory as an install from before one room left it: the old rooms CHECK, and
    `#conference` and `#notices` rows beside `#names`. `CREATE TABLE IF NOT EXISTS`
    leaves the old table and its CHECK as they are."""
    directory_db().parent.mkdir(parents=True, exist_ok=True)
    conn = directory.connect(directory_db())
    try:
        conn.execute("CREATE TABLE rooms (channel_id TEXT PRIMARY KEY, name TEXT NOT NULL "
                     "UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('names','conference',"
                     "'notices')), created_at TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    os.chmod(directory_db(), 0o600)
    p = provision(gw, directory_db(), slots=slots)
    conn = directory.init(directory_db())
    try:
        for room, kind in (("#conference", "conference"), ("#notices", "notices")):
            channel_id = gw.create_channel(room)
            conn.execute("INSERT OR IGNORE INTO rooms(channel_id, name, kind, created_at) "
                         "VALUES (?,?,?,?)", (channel_id, room, kind, directory.now()))
        conn.commit()
    finally:
        conn.close()
    return p


def test_an_old_directory_with_three_rooms_loads_and_mints_names_webhooks_only():
    """[R28]: a standing row added later, with `rooms` read from the directory, and a slot
    added by a later run each get one `#names` webhook, never one in every room row."""
    gw = MockGateway()
    p = _old_directory(gw, slots=1)
    conn = directory.init(directory_db())
    try:
        assert {r["name"] for r in conn.execute("SELECT name FROM rooms")} >= {
            NAMES, "#conference", "#notices"}
        add_standing(gw, conn, "main", "proj-a")
    finally:
        conn.close()
    provision(gw, directory_db(), slots=2)
    conn = directory.connect(directory_db(), read_only=True)
    try:
        standing = conn.execute("SELECT channel_id FROM webhooks WHERE agent_name='main'"
                                ).fetchall()
        assert [r["channel_id"] for r in standing] == [p.rooms[NAMES]]
        slot = conn.execute("SELECT channel_id FROM webhooks WHERE slot_id=2").fetchall()
        assert [r["channel_id"] for r in slot] == [p.rooms[NAMES]]
    finally:
        conn.close()


def test_send_only_on_an_old_directory_mints_one_names_webhook(tmp_path):
    """[R28], the send-only verb: it reads the `#names` row only and prints one webhook."""
    from moxi_comms.provision import send_only_main

    gw = MockGateway()
    p = _old_directory(gw)
    said: list[str] = []
    assert send_only_main(["console@acme", "--db", str(directory_db())], gateway=gw,
                          env={}, out=said.append) == 0
    conn = directory.connect(directory_db(), read_only=True)
    try:
        rows = conn.execute("SELECT channel_id FROM webhooks WHERE agent_name='console'"
                            ).fetchall()
    finally:
        conn.close()
    assert [r["channel_id"] for r in rows] == [p.rooms[NAMES]]
    hooks = [line for line in said if line.lstrip().startswith("webhook")]
    assert len(hooks) == 1 and NAMES in hooks[0]


def test_standing_identity_gets_its_own_verifiable_bundle():
    gw = MockGateway()
    p = provision(gw, directory_db(), slots=1, standing=[("main", "proj-a")])
    conn = directory.connect(directory_db(), read_only=True)
    try:
        row = directory.agent_webhook(conn, "main", "proj-a", p.rooms[NAMES])
        assert row is not None and row["slot_id"] is None
    finally:
        conn.close()


def test_provisioning_is_idempotent():
    gw = MockGateway()
    provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")])
    provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")])
    conn = directory.connect(directory_db(), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] == 2 + 1
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == len(ROOM_KINDS)
    finally:
        conn.close()


def test_agent_n_namespace_is_refused_as_a_standing_row():
    gw = MockGateway()
    provision(gw, directory_db(), slots=1)
    conn = directory.connect(directory_db())
    try:
        with pytest.raises(ProvisionError, match="reserved"):
            add_standing(gw, conn, "agent-4", "proj-a")
    finally:
        conn.close()


def test_standing_row_refused_while_the_name_is_live_as_chosen(rig):
    from moxi_comms.client import DaemonClient
    from moxi_comms.paths import daemon_socket

    rig.shim(slot=1, name="planner", scope="proj-a")
    client = DaemonClient(daemon_socket()).connect()
    conn = directory.connect(directory_db())
    try:
        with pytest.raises(ProvisionError, match="release it first"):
            add_standing(rig.gateway, conn, "planner", "proj-a", daemon_client=client)
    finally:
        conn.close()
        client.close()


# ------------------------------------- provision as the token WRITER (the local backend)
#
# One token per ROW, written into the holder's store file under the row's own
# `secret_id` in the same call that created the row -- which is what makes a first run
# against `local` need no portal, no pasting and no operator beyond running the verb.

def local_rig(tmp_path):
    """A local record and its store, the pair `moxi-comms-provision --gateway local` builds."""
    from moxi_comms.local_gateway import LocalGateway
    from moxi_comms.tokens import TokenStore

    return LocalGateway(tmp_path / "record.db"), TokenStore(tmp_path / "store")


def test_every_minted_token_lands_in_its_own_holders_file(tmp_path):
    """Slot bundles to `owner_for_slot`, standing rows to their owner, the operator's to
    the `operator` holder. The daemon holds no webhook ([F22])."""
    from moxi_comms import tokens

    gw, store = local_rig(tmp_path)
    try:
        provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")],
                  operator=("900001", "operator"), store=store)
    finally:
        gw.close()

    assert set(store.owners_present()) == {
        "slot-1", "slot-2", "proj-a/main", "operator"}
    for slot_id in (1, 2):
        owner = tokens.owner_for_slot(slot_id)
        assert store.secret_ids(owner) == [f"comms/slot-{slot_id}/names"]
    assert store.secret_ids(tokens.owner_for_role(tokens.ROLE_OPERATOR)) == \
        [tokens.OPERATOR_SECRET_ID]
    assert store.secret_ids(tokens.owner_for_standing("main", "proj-a")) == \
        ["comms/proj-a/main/names"]


def test_the_record_keeps_hashes_and_the_directory_keeps_pointers(tmp_path):
    """Not one minted token is anywhere but the holder's 0600 file. The record's webhook
    rows keep a SHA-256, `meta` keeps the operator's, and `directory.db`'s
    `bws_secret_id` keeps the pointer it always was."""
    from moxi_comms import tokens

    gw, store = local_rig(tmp_path)
    try:
        provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")],
                  operator=("900001", "operator"), store=store)
    finally:
        gw.close()

    minted = [store.read_secret(owner, secret_id)
              for owner in (tokens.owner_for_slot(1), tokens.owner_for_slot(2),
                            tokens.owner_for_standing("main", "proj-a"),
                            tokens.owner_for_role(tokens.ROLE_OPERATOR))
              for secret_id in store.secret_ids(owner)]
    assert len(minted) == 4 and all(minted)

    for db in ((tmp_path / "record.db"), directory_db()):
        for path in db.parent.glob(db.name + "*"):        # the WAL and the shm too
            blob = path.read_bytes()
            for token in minted:
                assert token.encode() not in blob, f"{token[:4]}... is in the clear in {path.name}"

    conn = directory.connect(directory_db(), read_only=True)
    try:
        pointers = [r["bws_secret_id"] for r in conn.execute("SELECT * FROM webhooks")]
    finally:
        conn.close()
    assert all(p.startswith("comms/") for p in pointers)


def test_a_second_run_converges_and_rotates_nothing(tmp_path):
    """Re-provisioning is the same promise the directory rows have always made: what is
    there is found, only what is missing is made, and no holder loses the credential it
    is already spending."""
    from moxi_comms import tokens

    gw, store = local_rig(tmp_path)
    try:
        first = provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")],
                          operator=("900001", "operator"), store=store)
        before = {(owner.key, secret_id): store.read_secret(owner, secret_id)
                  for owner in (tokens.owner_for_slot(1), tokens.owner_for_slot(2),
                                tokens.owner_for_standing("main", "proj-a"),
                                    tokens.owner_for_role(tokens.ROLE_OPERATOR))
                  for secret_id in store.secret_ids(owner)}

        second = provision(gw, directory_db(), slots=2, standing=[("main", "proj-a")],
                           operator=("900001", "operator"), store=store)
        after = {(owner.key, secret_id): store.read_secret(owner, secret_id)
                 for owner in (tokens.owner_for_slot(1), tokens.owner_for_slot(2),
                               tokens.owner_for_standing("main", "proj-a"),
                                  tokens.owner_for_role(tokens.ROLE_OPERATOR))
                 for secret_id in store.secret_ids(owner)}
    finally:
        gw.close()

    assert after == before
    assert first.operator_token_written is True
    assert second.operator_token_written is False, "the second run rotated the operator"


def test_half_an_operator_pair_is_replaced_rather_than_left_unusable(tmp_path):
    """A stored token whose hash is not in the record is a lane nobody can use, so the
    converging run mints -- and the two halves agree again afterwards."""
    from moxi_comms import tokens
    from moxi_comms.local_gateway import META_OPERATOR_TOKEN_HASH, token_hash

    gw, store = local_rig(tmp_path)
    owner = tokens.owner_for_role(tokens.ROLE_OPERATOR)
    try:
        provision(gw, directory_db(), slots=1, operator=("900001", "operator"), store=store)
        store.write_secret(owner, tokens.OPERATOR_SECRET_ID, "stale-and-unmatched")

        again = provision(gw, directory_db(), slots=1, operator=("900001", "operator"),
                          store=store)
        token = store.read_secret(owner, tokens.OPERATOR_SECRET_ID)
        stored = gw.meta_get(META_OPERATOR_TOKEN_HASH)
    finally:
        gw.close()

    assert again.operator_token_written is True
    assert token != "stale-and-unmatched"
    assert stored == token_hash(token)


def test_the_mock_is_not_given_a_store(tmp_path):
    """The mock keeps the token in its own record, so there is nothing here to keep and
    provision writes nothing."""
    from moxi_comms.tokens import TokenStore

    store = TokenStore(tmp_path / "store")
    provision(MockGateway(), directory_db(), slots=1, standing=[("main", "proj-a")])

    assert store.owners_present() == []


# ------------------------------------------------- the first start and `[provision]`

def test_the_first_start_makes_what_provision_in_config_says(tmp_path):
    """`[provision] slots` and `operator`, read when the step runs; with no file it is
    the settings' defaults (6, `operator`)."""
    from conftest import write_config
    from moxi_comms import provision as prov
    from moxi_comms.local_gateway import LocalGateway
    from moxi_comms.tokens import TokenStore

    assert prov.configured() == (prov.DEFAULT_SLOTS, prov.DEFAULT_OPERATOR) == \
        (6, ("operator", "operator"))
    write_config(None, '[provision]\nslots = 2\noperator = "ethel"\n')
    assert prov.configured() == (2, ("operator", "ethel"))

    gw = LocalGateway(tmp_path / "record.db")
    try:
        prov.first_start(TokenStore(tmp_path / "store"))(gw, directory_db())
        conn = directory.connect(directory_db(), read_only=True)
        try:
            assert directory.slot_ids(conn) == [1, 2]
            assert [r["name"] for r in conn.execute("SELECT name FROM operators")] == \
                ["ethel"]
        finally:
            conn.close()
        # A provisioned directory is left alone, whatever the config says now.
        write_config(None, "[provision]\nslots = 4\n")
        prov.first_start(TokenStore(tmp_path / "store"))(gw, directory_db())
        conn = directory.connect(directory_db(), read_only=True)
        try:
            assert directory.slot_ids(conn) == [1, 2]
        finally:
            conn.close()
    finally:
        gw.close()


def test_the_paste_verb_is_gone():
    """The bws paste path went with bws: no `tokens` subcommand, no holder list."""
    from moxi_comms import provision as prov

    assert not hasattr(prov, "tokens_main") and not hasattr(prov, "holders")
