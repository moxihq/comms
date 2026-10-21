"""The resolver chain: one door to a credential, two places to look behind it.

What this file pins is the chain's whole contract: the declared order, a miss falling
through and a break staying loud, the KEY the env step looks under, the holder the store
step is keyed by, the one sentence an agent reads when nothing holds the secret, and the
config that declares all of it.

Moxi's comms has two steps, `env` and `store` (design §7.2). The Bitwarden step is the
fleet add-on's, and no part of this package names it: a chain that asks for it is
refused at config load, with the steps there are.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest
from conftest import write_config

from moxi_comms import config, secrets
from moxi_comms.gateway import GatewayError
from moxi_comms.local_gateway import LocalGateway
from moxi_comms.tokens import TokenStore, owner_for_slot

SECRET_ID = "comms/slot-1/names"
OWNER = owner_for_slot(1)


# --------------------------------------------------------------------------- the order

def test_env_beats_store_and_a_miss_falls_through_to_the_next(tmp_path):
    """The declared order, proved by taking one step away. A step that does not hold the
    secret is a MISS and the next one answers; nothing is retried and nothing is
    reordered."""
    store = TokenStore(tmp_path / "tokens")
    store.write_secret(OWNER, SECRET_ID, "FROM-THE-STORE")
    env = {secrets.env_key(SECRET_ID): "FROM-THE-ENVIRONMENT"}

    chain = secrets.build(secrets.DEFAULT_CHAIN, env=env, store=store)
    assert chain.names == ("env", "store")
    assert chain.get(SECRET_ID, owner=OWNER)["value"] == "FROM-THE-ENVIRONMENT"

    fell = secrets.build(secrets.DEFAULT_CHAIN, env={}, store=store)
    assert fell.get(SECRET_ID, owner=OWNER)["value"] == "FROM-THE-STORE"

    reordered = secrets.build(["store", "env"], env=env, store=store)
    assert reordered.get(SECRET_ID, owner=OWNER)["value"] == "FROM-THE-STORE"


def test_the_env_step_looks_under_the_key_the_design_states(tmp_path):
    """The env step: `MOXI_COMMS_SECRET_<KEY>`, the id upper-cased with every run of
    non-alphanumerics folded to one underscore. Stated in `env_key` and pinned here, so a
    box's config and its shell agree about the name."""
    assert secrets.env_key(SECRET_ID) == "MOXI_COMMS_SECRET_COMMS_SLOT_1_NAMES"
    assert secrets.env_key("ANTHROPIC_API_KEY") == "MOXI_COMMS_SECRET_ANTHROPIC_API_KEY"
    assert secrets.env_key("9bd448ec-0d1b") == "MOXI_COMMS_SECRET_9BD448EC_0D1B"

    chain = secrets.build(["env"], env={"MOXI_COMMS_SECRET_ANTHROPIC_API_KEY": "sk-x"})
    got = chain.get("ANTHROPIC_API_KEY")
    assert got == {"value": "sk-x", "note": ""}


def test_a_store_hit_is_keyed_by_the_holder_and_carries_no_note(tmp_path):
    """The store step is the custody store's DECLARED READ and the store is a flat map,
    so a hit has an empty note. A lookup with no holder cannot be keyed at all and is a
    miss, never another holder's file."""
    store = TokenStore(tmp_path / "tokens")
    store.write_secret(OWNER, SECRET_ID, "STORED-TOKEN")
    chain = secrets.build(["store"], env={}, store=store)

    assert chain.get(SECRET_ID, owner=OWNER) == {"value": "STORED-TOKEN", "note": ""}
    with pytest.raises(secrets.SecretNotFound):
        chain.get(SECRET_ID)                         # no owner: nothing to key on
    with pytest.raises(secrets.SecretNotFound):
        chain.get(SECRET_ID, owner=owner_for_slot(2))


# --------------------------------------------------------------- a break is not a miss

def test_a_store_that_cannot_be_read_is_loud_and_names_the_step(tmp_path):
    """"the credential is not here" and "I could not tell whether it is here" are
    different sentences, and only one of them may be answered by the next step. A holder
    file the rest of the box can read is the store's own refusal (0600 is the whole
    mechanism), so the chain stops on it instead of shrugging and asking the next step."""
    store = TokenStore(tmp_path / "tokens")
    path = store.write_secret(OWNER, SECRET_ID, "STORED-TOKEN")
    os.chmod(path, 0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    env = {secrets.env_key(SECRET_ID): "FROM-THE-ENVIRONMENT"}

    chain = secrets.build(["store", "env"], env=env, store=store)
    with pytest.raises(secrets.SecretStepFailed) as e:
        chain.get(SECRET_ID, owner=OWNER)
    assert "token store step failed" in str(e.value)
    assert "FROM-THE-ENVIRONMENT" not in str(e.value)


# --------------------------------------------------------------------------- the miss

def test_the_all_miss_error_names_the_steps_tried_and_the_holder(tmp_path):
    """One sentence, in the caller's language (the send verb prints it): which steps were
    tried, in order, and WHOSE credential was being looked for. No path, no key, no
    value."""
    chain = secrets.build(["env", "store"], env={}, store=tmp_path / "tokens")
    with pytest.raises(GatewayError) as e:            # SecretNotFound IS a GatewayError
        chain.get(SECRET_ID, owner=OWNER)
    said = str(e.value)
    assert f"cannot resolve the posting credential {SECRET_ID}" in said
    assert "the environment (MOXI_COMMS_SECRET_COMMS_SLOT_1_NAMES)" in said
    assert "the holder's token store" in said
    assert "slot-1" in said                               # the holder
    assert "ask the operator" in said


def test_an_env_only_chain_says_nothing_beyond_its_steps_was_tried():
    chain = secrets.build(["env"], env={})
    with pytest.raises(secrets.SecretNotFound) as e:
        chain.get(SECRET_ID, owner=OWNER)
    assert "Nothing was attempted beyond those steps." in str(e.value)
    assert "token store" not in str(e.value)


# ------------------------------------------------------------- no Bitwarden step here

def test_the_package_has_no_bitwarden_step():
    """The bws step is the fleet add-on's (§7.3): no module, no step name, no constant."""
    assert secrets.STEPS == ("env", "store")
    for gone in ("STEP_BWS", "BwsResolver", "for_bws", "STANDALONE_CHAIN"):
        assert not hasattr(secrets, gone), gone
    with pytest.raises(ImportError):
        __import__("moxi_comms.bws")
    with pytest.raises(secrets.SecretError, match="unknown secret resolver 'bws'"):
        secrets.build(["env", "store", "bws"])


CHILD_DEFAULT = """
import sys

from moxi_comms import config, secrets, tokens

store_root, secret_id = sys.argv[1:3]
cfg = config.load()
assert list(cfg.secrets.chain) == ["env", "store"], cfg.secrets.chain
got = secrets.from_config(cfg, env={}, store=store_root).get(
    secret_id, owner=tokens.owner_for_slot(1))
assert got == {"value": "STORED-TOKEN", "note": ""}, got
assert sorted(m for m in sys.modules if m.endswith("bws")) == []
print("ok")
"""


def test_a_box_with_no_table_resolves_env_then_store_in_a_fresh_process(tmp_path):
    """In a fresh interpreter under the test's tmp roots: a box with no config file
    loads its defaults, builds `env, store` and resolves a real credential, and no
    module named for Bitwarden enters the process."""
    TokenStore(tmp_path / "tokens").write_secret(OWNER, SECRET_ID, "STORED-TOKEN")
    out = subprocess.run([sys.executable, "-c", CHILD_DEFAULT, str(tmp_path / "tokens"),
                          SECRET_ID], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# --------------------------------------------------------------------------- the config

def test_the_chain_is_declared_in_the_config_and_defaults_to_env_then_store():
    """Absent means `env, store`; a box that wants one step writes the line."""
    assert secrets.default_chain() == ("env", "store")
    assert config.load().secrets.chain == ("env", "store")
    assert config.SecretsConfig().chain == ("env", "store")
    assert secrets.build().names == ("env", "store")

    write_config(None, '[secrets]\nchain = ["store"]\n')
    assert config.load().secrets.chain == ("store",)
    assert secrets.from_config(config.load()).names == ("store",)


def test_config_refuses_a_step_name_that_is_not_a_resolver():
    """A typo fails the box's first read of its own config, not the first send of the
    day; so does a chain that still names the fleet's Bitwarden step."""
    for step in ("vault", "bws"):
        write_config(None, f'[secrets]\nchain = ["env", "{step}"]\n')
        with pytest.raises(config.ConfigError) as e:
            config.load()
        assert f"'{step}'" in str(e.value)
        assert "env, store" in str(e.value)

    write_config(None, '[secrets]\nchain = []\n')
    with pytest.raises(config.ConfigError, match="empty"):
        config.load()

    write_config(None, '[secrets]\nchain = "env"\n')
    with pytest.raises(config.ConfigError):
        config.load()

    with pytest.raises(secrets.SecretError, match="unknown secret resolver"):
        secrets.build(["env", "vault"])


def test_the_secrets_table_may_not_hold_a_token_like_every_other_table():
    """`refuse_secrets` walks `[secrets]` with the rest of the file: the table declares
    WHERE credentials come from and never what they are."""
    write_config(None, '[secrets]\nchain = ["store"]\nwebhook_url = "https://example/x/y"\n')
    with pytest.raises(config.ConfigError, match="looks like a secret"):
        config.load()

    write_config(None, '[secrets]\naccess_token = "T"\n')
    with pytest.raises(config.ConfigError, match="looks like a secret"):
        config.load()


# ------------------------------------------------------------------- through the backends

def test_the_local_gateway_resolves_through_the_chain_end_to_end(tmp_path):
    """The other half: `LocalGateway._read_secret` is the chain now, and its `store`
    step is the declared read. Mint a row, write the token where provision writes it, and
    post with what the chain hands back."""
    from moxi_comms.gateway import NO_MENTIONS

    store = TokenStore(tmp_path / "tokens")
    chain = secrets.build(["env", "store"], env={}, store=store)
    gateway = LocalGateway(tmp_path / "record.db", poll_interval=60, resolver=chain)
    try:
        channel = gateway.create_channel("#names")
        cred = gateway.create_webhook(channel, "slot-1", SECRET_ID)

        with pytest.raises(GatewayError) as e:            # nothing written yet
            gateway.resolve_secret(SECRET_ID, owner=OWNER)
        assert "the holder's token store" in str(e.value)

        store.write_secret(OWNER, SECRET_ID, cred.token)
        resolved = gateway.resolve_secret(SECRET_ID, owner=OWNER)
        assert (resolved.webhook_id, resolved.token) == (cred.webhook_id, cred.token)
        posted = gateway.execute_webhook(resolved, "through the chain", username="slot-1",
                                         allowed_mentions=NO_MENTIONS)
        assert posted.content == "through the chain"
    finally:
        gateway.close()


def test_the_local_gateway_takes_the_env_step_too(tmp_path):
    """Same seam, first step: a role credential exported into the process resolves with
    no store file at all, which is what a client box's first run looks like before
    provision has written anything."""
    store = TokenStore(tmp_path / "tokens")
    env = {}
    chain = secrets.build(["env", "store"], env=env, store=store)
    gateway = LocalGateway(tmp_path / "record.db", poll_interval=60, resolver=chain)
    try:
        channel = gateway.create_channel("#names")
        cred = gateway.create_webhook(channel, "slot-1", SECRET_ID)
        env[secrets.env_key(SECRET_ID)] = cred.token
        resolved = gateway.resolve_secret(SECRET_ID, owner=OWNER)
        assert resolved.token == cred.token
    finally:
        gateway.close()


def test_open_gateway_hands_the_declared_chain_to_the_backend(tmp_path):
    """`open_gateway` builds the chain once, from config, and the gateway holds it: no
    backend constructs a resolver of its own."""
    from moxi_comms.gateway import open_gateway

    write_config(None, f'backend = "local"\n\n[local]\nrecord = "{tmp_path / "record.db"}"'
                       '\n\n[secrets]\nchain = ["store"]\n')
    gateway = open_gateway()
    try:
        assert gateway.resolver.names == ("store",)
    finally:
        gateway.close()


def test_every_step_returns_one_json_shape(tmp_path):
    """One shape for every step: `{"value", "note"}`. Pinned so a later step (a keyring,
    a client's own store) cannot quietly return something else and break the poster's
    rebuild."""
    store = TokenStore(tmp_path / "tokens")
    store.write_secret(OWNER, SECRET_ID, "STORED-TOKEN")
    for chain in (secrets.build(["env"], env={secrets.env_key(SECRET_ID): "E"}),
                  secrets.build(["store"], env={}, store=store)):
        got = chain.get(SECRET_ID, owner=OWNER)
        assert sorted(got) == ["note", "value"], got
        assert isinstance(json.dumps(got), str)
