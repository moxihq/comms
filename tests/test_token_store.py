"""The token store: what may rest, where, at what mode, and who may spend it.

The store's door is the declared pair, `read_secret` and `write_secret`. The machine
token the fleet's Bitwarden step paid with, its reserved key and the paste verb that
wrote it are the fleet add-on's (design §7.2-§7.3), so this file has no bws fake. A file
that is not a flat table of strings holds no comms key: a read misses, a write refuses.
"""

from __future__ import annotations

import os
import stat
import tomllib

import pytest

from moxi_comms import tokens
from moxi_comms.mock_gateway import MockGateway
from moxi_comms.paths import tokens_dir
from moxi_comms.tokens import (NOT_A_MAP, STORE_ENV_VAR, TokenStore, TokenStoreError,
                               owner_for_role, owner_for_slot, owner_for_standing)

POSTING_KEY = "comms/slot-1/names"


# --------------------------------------------------------------------- the location

def test_the_default_location_is_the_device_keyed_state_root():
    """STATE, not DATA: a stored credential is a device credential and DATA is the sync
    surface."""
    assert TokenStore(env={}).root == tokens_dir()
    assert "state" in str(tokens_dir())


def test_the_explicit_override_beats_the_default_state_path(tmp_path):
    """`MOXI_COMMS_HOME` collapses every root, so an alternate-home recipe needs a
    deliberate way to still name the operator's real store."""
    store = TokenStore(env={STORE_ENV_VAR: str(tmp_path / "elsewhere")})
    assert store.root == tmp_path / "elsewhere"

    store.write_secret(owner_for_slot(2), "comms/slot-2/names", "SLOT-2-TOKEN")
    assert (tmp_path / "elsewhere" / "slot-2").exists()
    assert not tokens_dir().exists()


def test_the_holder_file_names(tmp_path):
    store = TokenStore(tmp_path)
    assert store.path_for(owner_for_slot(4)) == tmp_path / "slot-4"
    assert store.path_for(owner_for_standing("main", "proj-a")) == tmp_path / "proj-a/main"
    assert store.path_for(owner_for_role("daemon")) == tmp_path / "daemon"
    assert store.path_for(owner_for_role("reader")) == tmp_path / "daemon"   # one holder
    assert store.path_for(owner_for_role("operator")) == tmp_path / "operator"


def test_a_role_that_is_still_unknown_has_no_store_path_at_all(tmp_path):
    """The role set is closed by construction, not by a list somewhere: an unknown role
    gets an owner with no PARTS, and the refusal happens where the path is computed. The
    message is the generic no-owner one -- there is no role-specific error to extend, and
    a box adding a role names it in `[secrets] roles` (test_roles.py)."""
    store = TokenStore(tmp_path)
    unknown = owner_for_role("stenographer")
    assert unknown.parts == () and unknown.role == "stenographer"
    with pytest.raises(TokenStoreError) as e:
        store.path_for(unknown)
    assert "no owner" in str(e.value)
    assert "stenographer" not in str(e.value)


def test_the_fleet_names_are_gone():
    for gone in ("BWS_ACCESS_TOKEN_KEY", "FLEET_ROLES", "ROLE_VOICE_LOOP",
                 "ROLE_REMOTE_EAR", "ROLE_CALENDAR"):
        assert not hasattr(tokens, gone), gone
    assert not hasattr(TokenStore, "read") and not hasattr(TokenStore, "write")


# --------------------------------------------------------------------- permissions

def test_the_store_is_written_0600_in_a_0700_directory(tmp_path):
    store = TokenStore(tmp_path / "tokens")
    path = store.write_secret(owner_for_standing("main", "proj-a"),
                              "comms/proj-a/main/names", "STANDING-TOKEN")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_a_loose_file_is_REFUSED_not_warned_about(tmp_path):
    """Decided here: 0600 is the whole mechanism. A store that reads a
    world-readable credential anyway has a comment where its rule was."""
    store = TokenStore(tmp_path)
    path = store.write_secret(owner_for_slot(1), POSTING_KEY, "SLOT-1-TOKEN")
    os.chmod(path, 0o644)
    with pytest.raises(TokenStoreError) as e:
        store.read_secret(owner_for_slot(1), POSTING_KEY)
    assert "0600" in str(e.value)
    assert str(path) not in str(e.value)      # the path goes to the log, not to an agent


def test_a_loose_store_directory_is_refused_too(tmp_path):
    store = TokenStore(tmp_path / "tokens")
    store.write_secret(owner_for_slot(1), POSTING_KEY, "SLOT-1-TOKEN")
    os.chmod(store.root, 0o755)
    with pytest.raises(TokenStoreError):
        store.read_secret(owner_for_slot(1), POSTING_KEY)


# --------------------------------------------------------------------- the fences

def test_the_provisioner_role_cannot_even_have_a_file_written_for_it(tmp_path):
    with pytest.raises(TokenStoreError) as e:
        TokenStore(tmp_path).write_secret(owner_for_role("provisioner"), POSTING_KEY,
                                          "MASTER-ISH")
    assert "crown-jewel" in str(e.value)
    assert not any(tmp_path.rglob("*"))


def test_the_provisioner_is_refused_by_the_declared_read_too(tmp_path):
    with pytest.raises(TokenStoreError) as e:
        TokenStore(tmp_path).read_secret(owner_for_role("provisioner"), POSTING_KEY)
    assert "crown-jewel" in str(e.value)
    assert not any(tmp_path.rglob("*"))


@pytest.mark.parametrize("bad", ["../../etc", "..", ".", "a/b", "", "with space", "x\\y"])
def test_owner_components_cannot_walk_out_of_the_store(bad):
    """Scope and name are env-derived (`MOXI_COMMS_PROJECT` verbatim), so a crafted
    value must not be able to name a path outside the store."""
    with pytest.raises(TokenStoreError):
        owner_for_standing(bad, "proj-a")
    with pytest.raises(TokenStoreError):
        owner_for_standing("main", bad)


def test_a_traversal_attempt_never_reaches_the_filesystem(tmp_path):
    store = TokenStore(tmp_path / "tokens")
    (tmp_path / "outside").write_text("NOT-A-STORE-FILE")
    with pytest.raises(TokenStoreError):
        store.read_secret(owner_for_standing("outside", ".."), POSTING_KEY)


# --------------------------------------------------------------------- the seam

def test_the_mock_gateway_takes_the_owner_and_ignores_it(tmp_path):
    """No mock-backend regression. The mock holds no credential to scope."""
    gw = MockGateway(str(tmp_path / "store.json"))
    channel = gw.create_channel("#names")
    cred = gw.create_webhook(channel, "slot-1", POSTING_KEY)
    assert gw.resolve_secret(POSTING_KEY).token == cred.token
    assert gw.resolve_secret(POSTING_KEY, owner=owner_for_slot(1)).token == cred.token


def test_the_sender_threads_the_owner_of_the_row_it_actually_resolved(rig):
    """A chosen-tier sender carries BOTH a name and a slot and spends the SLOT
    bundle, so the owner has to follow the row, not the identity's shape."""
    from moxi_comms import directory
    from moxi_comms.paths import directory_db
    from moxi_comms.sender import SenderIdentity, credential_for

    seen = []

    class Recording:
        def resolve_secret(self, secret_id, *, owner=None):
            seen.append(owner)
            return rig.gateway.resolve_secret(secret_id, owner=owner)

    db = directory.connect(directory_db(), read_only=True)
    try:
        credential_for(Recording(), db, SenderIdentity.for_agent("main", "proj-a"))
        assert seen[-1].key == "proj-a/main"

        # chosen tier: name AND slot, and the slot bundle is what gets spent
        credential_for(Recording(), db,
                       SenderIdentity.for_slot(2, "pilot@proj-a", "proj-a", name="pilot"))
        assert seen[-1].key == "slot-2"
    finally:
        db.close()


# ------------------------------------------------------------ the map and the pair
#
# The holder file is a flat TOML map, and every value goes in and out through
# `read_secret` / `write_secret`.

def test_read_secret_and_write_secret_round_trip_at_0600_in_0700(tmp_path):
    """The declared pair, both halves, with the 0600/0700 modes."""
    store = TokenStore(tmp_path / "tokens")
    owner = owner_for_slot(1)
    path = store.write_secret(owner, POSTING_KEY, "POSTING-TOKEN-0001")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert store.read_secret(owner, POSTING_KEY) == "POSTING-TOKEN-0001"
    assert tomllib.loads(path.read_text()) == {POSTING_KEY: "POSTING-TOKEN-0001"}
    assert store.secret_ids(owner) == [POSTING_KEY]
    assert store.read_secret(owner, "comms/slot-1/other") is None


def test_one_key_is_written_without_disturbing_the_others(tmp_path):
    store = TokenStore(tmp_path)
    owner = owner_for_slot(1)
    store.write_secret(owner, "ANTHROPIC_API_KEY", "ROLE-CREDENTIAL")
    store.write_secret(owner, POSTING_KEY, "NAMES-TOKEN")
    store.write_secret(owner, POSTING_KEY, "NAMES-TOKEN-ROTATED")
    assert store.read_secret(owner, "ANTHROPIC_API_KEY") == "ROLE-CREDENTIAL"
    assert store.read_secret(owner, POSTING_KEY) == "NAMES-TOKEN-ROTATED"
    assert store.secret_ids(owner) == ["ANTHROPIC_API_KEY", POSTING_KEY]


def test_the_emitter_escapes_a_value_that_looks_like_toml(tmp_path):
    """The stdlib reads TOML and does not write it, so the emitter is ours: a value with
    a quote, a backslash, an `=` and a newline in it comes back byte for byte."""
    nasty = 'a"b\\c=d\ne\tf'
    store = TokenStore(tmp_path)
    owner = owner_for_slot(1)
    store.write_secret(owner, 'weird"key\\here', nasty)
    assert store.read_secret(owner, 'weird"key\\here') == nasty.strip()
    assert tomllib.loads(store.path_for(owner).read_text()) == {
        'weird"key\\here': nasty.strip()}


def test_the_declared_pair_refuses_an_empty_value_and_an_unprintable_key(tmp_path):
    store = TokenStore(tmp_path)
    with pytest.raises(TokenStoreError):
        store.write_secret(owner_for_slot(1), POSTING_KEY, "   ")
    with pytest.raises(TokenStoreError):
        store.write_secret(owner_for_slot(1), "two\nlines", "TOK")
    with pytest.raises(TokenStoreError):
        store.read_secret(owner_for_slot(1), "")
    assert not any(tmp_path.rglob("*"))


def test_a_loose_file_is_refused_by_every_half_of_the_boundary(tmp_path):
    """0600 is the whole mechanism, so the pair and the key-level question refuse
    alike."""
    store = TokenStore(tmp_path)
    owner = owner_for_slot(1)
    path = store.write_secret(owner, POSTING_KEY, "POSTING-TOKEN-0001")
    os.chmod(path, 0o644)
    for call in (lambda: store.read_secret(owner, POSTING_KEY),
                 lambda: store.secret_ids(owner),
                 lambda: store.write_secret(owner, POSTING_KEY, "NEXT")):
        with pytest.raises(TokenStoreError) as e:
            call()
        assert "0600" in str(e.value)
        assert str(path) not in str(e.value)


@pytest.mark.parametrize("bare", ["0.slot-one.machine:token", "0.abc=.def:ghi",
                                  "aGVsbG8=", "a=b", "x = 3"])
def test_a_file_that_is_not_a_flat_table_of_strings_holds_no_key_and_is_left_alone(
        tmp_path, bare):
    """A bare token (what an older install's paste verb wrote) or any other shape is not
    the map this store writes: it holds no comms key, so a read misses as it did before
    the port; a write refuses it with `NOT_A_MAP`, naming no path, and never overwrites
    it."""
    store = TokenStore(tmp_path)
    owner = owner_for_slot(1)
    path = tmp_path / "slot-1"
    path.write_text(bare + "\n")
    os.chmod(path, 0o600)
    os.chmod(tmp_path, 0o700)
    assert store.read_secret(owner, POSTING_KEY) is None
    assert store.secret_ids(owner) == []
    with pytest.raises(TokenStoreError) as e:
        store.write_secret(owner, POSTING_KEY, "NEXT")
    assert str(e.value) == NOT_A_MAP and str(path) not in str(e.value)
    assert path.read_text() == bare + "\n"


def test_the_comm_slot_question_is_asked_of_the_keys_not_of_a_credential(tmp_path):
    """`adapters/session.py` uses the store's key-level half: a holder whose file
    carries a posting token is PROVISIONED, and asking never reads the value."""
    from moxi_comms.adapters import session

    env = {STORE_ENV_VAR: str(tmp_path / "tokens")}
    store = TokenStore(env=env)
    store.write_secret(owner_for_slot(3), "comms/slot-3/names", "LOCAL-POSTING-TOKEN")
    store.write_secret(owner_for_slot(4), "comms/slot-4/names", "SLOT-4-TOKEN")

    assert session.unprovisioned(3, env=env) is None
    assert session.unprovisioned(4, env=env) is None
    assert "names no provisioned webhook bundle" in session.unprovisioned(9, env=env)
    assert session.provisioned_slots(env=env) == [3, 4]
    os.chmod(store.path_for(owner_for_slot(3)), 0o644)
    assert "could not answer" in session.unprovisioned(3, env=env)
