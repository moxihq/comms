"""The operator's typed lane, and the credential that guards it.

The terminal is the lane. The lane's two halves live in two places and this file pins
the second: `local_gateway.py` owns the hash compare (`test_local_gateway.py`), and the
SENDER owns the trigger, the claimed-session refusal and the token it presents.

Why local only: Discord refuses `post_as_user` outright and the mock keeps no `meta` for
a hash to rest in, so the credentialled lane exists on exactly one backend. What the mock
and Discord do own is the KEYWORD -- accepted and ignored on both -- and the parametrised
suites prove that every time they post as the operator.
"""

from __future__ import annotations

import pytest
from conftest import wait_for_queue

from moxi_comms import tokens
from moxi_comms.local_gateway import META_OPERATOR_TOKEN_HASH, token_hash


@pytest.fixture
def backend():
    """This lane is the local backend's; see the module docstring."""
    return "local"


def operator_env(user_id: str = "900001", **extra) -> dict:
    """The stamp the operator types into their own terminal. The launcher never sets it."""
    return {"MOXI_COMMS_OPERATOR": user_id, **extra}


# ------------------------------------------------------------------ the holder's token

def test_provision_gives_the_operator_a_holder_and_the_record_only_its_hash(rig):
    """The lane needs a credential like every other lane, so provision mints one.

    The token rests in the `operator` holder's own 0600 file and the record keeps the
    SHA-256 and nothing else, exactly as a webhook row does.
    """
    owner = tokens.owner_for_role(tokens.ROLE_OPERATOR)
    token = rig.store.read_secret(owner, tokens.OPERATOR_SECRET_ID)

    path = rig.store.path_for(owner)
    assert token, "provision minted no operator credential"
    assert path.name == "operator", "the operator is a holder like any other"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert rig.store.secret_ids(owner) == [tokens.OPERATOR_SECRET_ID]
    assert rig.gateway.meta_get(META_OPERATOR_TOKEN_HASH) == token_hash(token)


# ------------------------------------------------------------------ the guard

@pytest.mark.parametrize("claim", [{"MOXI_COMMS_NAME": "planner"}, {"MOXI_COMMS_SLOT": "3"}])
def test_a_claimed_session_is_never_the_operator(rig, claim):
    """A claim must not spell its way into `post_as_user`, which is the
    fabricated-identity class. Either stamp
    beside the trigger and the verb refuses, rather than preferring one of the two."""
    code, out = rig.cli(["send", "reviewer@proj-a", "ship it"], env=operator_env(**claim))

    assert code == 2, out
    assert "nothing was sent" in out
    assert "claimed session is never the operator" in out
    # No seat vocabulary and no variable names on an agent-facing surface.
    assert "MOXI_COMMS_" not in out and "slot" not in out.lower()
    assert rig.gateway.history(rig.names_channel) == []


def test_a_bad_operator_token_is_refused_by_the_record(rig):
    """The store is the only place the token lives, so a wrong one there is a wrong one
    at the record: the gateway compares against `meta('operator_token_hash')` and the
    verb reports a send that did NOT happen."""
    rig.store.write_secret(tokens.owner_for_role(tokens.ROLE_OPERATOR),
                           tokens.OPERATOR_SECRET_ID, "not-the-minted-one")

    code, out = rig.cli(["send", "--new-name", "reviewer@proj-a", "ship it"],
                        env=operator_env())

    assert code == 5, out
    assert "bad operator token" in out
    assert rig.gateway.history(rig.names_channel) == []


def test_no_operator_credential_on_the_box_is_a_loud_refusal(rig):
    """A lane with no credential does not quietly become the unguarded lane: a
    single-user install may never post with no credential."""
    rig.store.path_for(tokens.owner_for_role(tokens.ROLE_OPERATOR)).unlink()

    code, out = rig.cli(["send", "--new-name", "reviewer@proj-a", "ship it"],
                        env=operator_env())

    assert code == 5, out
    assert "operator's posting credential is not on this box" in out
    assert "moxi-comms-provision" in out
    assert rig.gateway.history(rig.names_channel) == []


# ------------------------------------------------------------------ the lane itself

def test_the_operator_posts_with_the_provisioned_token_and_lands_as_the_operator(rig):
    """The whole lane end to end: the stamp, the store, the record, the delivered frame.

    What the recipient reads back is the operators row (`attribution.from_record`), so a
    message typed in the operator's terminal arrives the way one typed in Discord always
    did."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")

    code, out = rig.cli(["send", "reviewer@proj-a", "ship it"], env=operator_env())

    assert code == 0, out
    wait_for_queue(reviewer, 1)
    assert reviewer.queue[0]["text"] == "ship it"
    assert reviewer.queue[0]["author"] == {"name": "operator", "tier": "operator",
                                           "attribution": "verified"}
    posted = rig.gateway.history(rig.names_channel)
    assert [m.author_kind for m in posted] == ["user"]
    assert posted[0].author_id == rig.prov.operator_user_id
    assert f"sent {posted[0].id}" in out


def test_the_operator_obeys_the_same_cap(rig):
    """The lane shares `_send`'s composition: the cap is a property of a MESSAGE, not of
    the webhook route, so a long operator post is refused the same way an agent's is,
    with nothing posted."""
    long_text = "x" * (rig.gateway.max_content_length + 500)

    code, out = rig.cli(["send", "--new-name", "reviewer@proj-a", long_text], env=operator_env())

    assert code == 2, out
    assert "-character cap on one message. Nothing was sent." in out
    assert rig.gateway.history(rig.names_channel) == []


def test_an_unstamped_terminal_is_not_the_operator(rig):
    """The trigger is the whole trigger: with no `MOXI_COMMS_OPERATOR` this is a shell with no
    claim, and the no-identity refusal is unchanged."""
    code, out = rig.cli(["send", "reviewer@proj-a", "ship it"])

    assert code == 2, out
    assert "comms has no identity for this process" in out
    assert rig.gateway.history(rig.names_channel) == []
