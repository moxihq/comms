"""The token store's role holders: comms' own four, and the ones a box names in
`[secrets] roles` (design §7.3, [R72]).

comms keeps its own roles in code: `reader` and `daemon` (one holder, the daemon
process), `operator` (the local backend's operator lane) and `provisioner` (which exists
only to be refused). A role the box adds is a name in `[secrets] roles`; it keys under
its own name, in the store and as a holder string, and a role the config does not list
is unknown, with no store path, like any name the store has never heard of. The names
below (`calendar`, `voice-loop`) are placeholders for whatever modules a box keeps
credentials for.
"""

from __future__ import annotations

import stat

import pytest
from conftest import write_config

from moxi_comms import config, secrets, tokens

ADDED = ("calendar", "voice-loop")


@pytest.fixture
def listed():
    write_config(None, '[secrets]\nroles = ["calendar", "voice-loop"]\n')


# ------------------------------------------------------------------- unlisted: unknown

@pytest.mark.parametrize("role", ADDED)
def test_an_unlisted_role_is_unknown(role, tmp_path):
    """What any unknown role gets: an owner with no parts, no holder string, no path."""
    assert config.load().secrets.roles == ()
    owner = tokens.owner_for_role(role)
    assert owner == tokens.TokenOwner((), role=role)
    assert not owner.refused
    assert tokens.owner_of_holder(f"comms/{role}") is None
    store = tokens.TokenStore(root=tmp_path / "store")
    with pytest.raises(tokens.TokenStoreError, match="no owner"):
        store.path_for(owner)
    with pytest.raises(tokens.TokenStoreError):
        store.write_secret(owner, f"{role}/key", "value")
    assert not (tmp_path / "store").exists()


def test_listing_one_role_leaves_the_others_unknown(tmp_path):
    write_config(None, '[secrets]\nroles = ["calendar"]\n')
    assert tokens.owner_for_role("calendar").parts == ("calendar",)
    assert tokens.owner_for_role("voice-loop").parts == ()
    assert tokens.owner_of_holder("comms/voice-loop") is None


# --------------------------------------------------------------------- listed: a holder

@pytest.mark.parametrize("role", ADDED)
def test_a_listed_role_keys_under_its_own_name(listed, role, tmp_path):
    owner = tokens.owner_for_role(role)
    assert owner == tokens.TokenOwner((role,), role=role)
    assert not owner.refused
    assert tokens.owner_of_holder(f"comms/{role}") == owner
    assert tokens.project_name(owner) == f"comms/{role}"
    assert tokens.holder_of_key(f"comms/{role}/bot-token") == f"comms/{role}"
    assert tokens.TokenStore(root=tmp_path).path_for(owner) == tmp_path / role


def test_role_holders_share_a_class_never_a_file(listed, tmp_path):
    """`reader` and `daemon` collapse to one file because they are one process; an added
    role is a different process holding different credentials, so it gets its own."""
    store = tokens.TokenStore(root=tmp_path)
    paths = {store.path_for(tokens.owner_for_role(r))
             for r in ("daemon", "operator", *ADDED)}
    assert len(paths) == 4
    assert store.path_for(tokens.owner_for_role("reader")) == tmp_path / "daemon"


def test_a_listed_roles_keys_round_trip_0600_and_a_second_leaves_the_first(listed,
                                                                          tmp_path):
    owner = tokens.owner_for_role("calendar")
    store = tokens.TokenStore(root=tmp_path)
    path = store.write_secret(owner, "calendar/refresh-token", "refresh-1")
    assert store.read_secret(owner, "calendar/refresh-token") == "refresh-1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    store.write_secret(owner, "calendar/client-id", "client-1")
    assert store.read_secret(owner, "calendar/refresh-token") == "refresh-1"
    assert store.read_secret(owner, "calendar/client-id") == "client-1"
    assert store.secret_ids(owner) == ["calendar/client-id", "calendar/refresh-token"]


def test_the_chain_resolves_a_listed_roles_credential(listed, tmp_path):
    """How a module reads its credential: the env step under its key, else the store
    keyed by the role's owner."""
    owner = tokens.owner_for_role("voice-loop")
    store = tokens.TokenStore(root=tmp_path)
    store.write_secret(owner, "voice-loop/bot-token", "FROM-THE-STORE")
    chain = secrets.build(["env", "store"], env={}, store=store)
    assert chain.get("voice-loop/bot-token", owner=owner)["value"] == "FROM-THE-STORE"
    env = {secrets.env_key("voice-loop/bot-token"): "FROM-THE-ENVIRONMENT"}
    chain = secrets.build(["env", "store"], env=env, store=store)
    assert chain.get("voice-loop/bot-token", owner=owner)["value"] == \
        "FROM-THE-ENVIRONMENT"


def test_the_roles_are_read_from_the_environment_a_caller_passes(tmp_path):
    """`owner_for_role(role, env=...)` reads the config file under the roots that env
    names, as every other reader of the config does."""
    other = {"MOXI_COMMS_CONFIG_HOME": str(tmp_path / "cfg"), "HOME": str(tmp_path)}
    write_config(other, '[secrets]\nroles = ["calendar"]\n')
    assert tokens.owner_for_role("calendar", env=other).parts == ("calendar",)
    assert tokens.owner_of_holder("comms/calendar", env=other).parts == ("calendar",)
    assert tokens.box_roles(other) == ("calendar",)
    assert tokens.owner_for_role("calendar").parts == ()
    assert tokens.box_roles() == ()


# ------------------------------------------------------------------- comms' own roles

def test_comms_own_roles_need_no_listing(tmp_path):
    assert tokens.owner_for_role("reader") == tokens.TokenOwner(("daemon",), role="reader")
    assert tokens.owner_for_role("daemon") == tokens.TokenOwner(("daemon",), role="daemon")
    assert tokens.owner_for_role("operator") == tokens.TokenOwner(("operator",),
                                                                  role="operator")
    assert tokens.owner_of_holder("comms/daemon") == tokens.owner_for_role("daemon")
    assert config.BUILTIN_ROLES == ("reader", "daemon", "provisioner", "operator")


@pytest.mark.parametrize("role", config.BUILTIN_ROLES)
def test_the_config_may_not_list_one_of_comms_own_roles(role):
    write_config(None, f'[secrets]\nroles = ["{role}"]\n')
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert caught.value.key == "secrets.roles"
    assert "comms' own" in str(caught.value)


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "with space", ""])
def test_a_listed_role_must_be_a_token_store_name(bad):
    write_config(None, f'[secrets]\nroles = ["{bad}"]\n')
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert caught.value.key == "secrets.roles"


def test_the_provisioner_stays_fenced_whatever_the_config_says(listed, tmp_path):
    owner = tokens.owner_for_role("provisioner")
    assert owner.refused and owner.parts == ()
    with pytest.raises(tokens.TokenStoreError, match="crown-jewel"):
        tokens.TokenStore(root=tmp_path / "store").write_secret(owner, "comms/x/y",
                                                                "MASTER-ISH")
    assert not (tmp_path / "store").exists()


def test_no_fleet_role_is_named_in_code():
    """The roles a box adds live in its config, not in comms (design §7.3, RULE D2)."""
    for gone in ("FLEET_ROLES", "ROLE_VOICE_LOOP", "ROLE_REMOTE_EAR", "ROLE_CALENDAR"):
        assert not hasattr(tokens, gone), gone
