"""The resolver CHAIN: one way to turn a secret id into a value, two places to look.

Every backend resolves secrets through it rather than its own lookup. The chain is the
one door, declared in config.toml:

    [secrets]
    chain = ["env", "store"]

and the interface is one method:

    Resolver.get(secret_id, *, owner=None) -> {"value": ..., "note": ...}

THE STEPS, in the order the file declares them:

  1. `env` -- `MOXI_COMMS_SECRET_<KEY>`, for comms' own role credentials. The KEY is the
     secret id upper-cased with every run of non-alphanumerics folded to one underscore
     (`env_key` below), so `comms/slot-1/names` is `MOXI_COMMS_SECRET_COMMS_SLOT_1_NAMES`
     and a role credential already named like a variable (`ANTHROPIC_API_KEY`) keeps its
     own spelling under the prefix.
  2. `store` -- the custody store's DECLARED READ, `TokenStore.read_secret(owner,
     secret_id)`, with the `owner` that rides down from the send verb. A store hit has
     no note: the store is a flat key/value map.

The default chain is both, `["env", "store"]`, and a box that wants only one names it.

A MISS IS NOT A FAILURE AND A FAILURE IS NOT A MISS. A step that does not hold the
secret falls through to the next one. A step that BREAKS -- a store file the rest of the
box can read -- is loud and names the step, because "the credential is not here" and "I
could not tell whether the credential is here" are different sentences and only one of
them is safe to answer with the next step's answer. When every step misses, one error
names the steps tried and the holder, in the caller's language (the send verb prints
it).
"""

from __future__ import annotations

import logging
import os
import re

from .gateway import GatewayError

log = logging.getLogger("comms.secrets")

#: The env step's prefix. `MOXI_COMMS_SECRET_<KEY>`.
ENV_PREFIX = "MOXI_COMMS_SECRET_"

STEP_ENV = "env"
STEP_STORE = "store"

#: The steps, in their default order. A name outside this tuple is refused at config
#: load (`config.load`), not at resolve time: a typo in the chain must fail the box's
#: first read of its own config, not the first send of the day.
STEPS = (STEP_ENV, STEP_STORE)

#: What a box whose config sets no `[secrets] chain` gets (settings.toml's default).
DEFAULT_CHAIN = STEPS


def default_chain() -> tuple:
    """The chain for a box whose config names none."""
    return DEFAULT_CHAIN


class SecretError(GatewayError):
    """A chain refusal. A `GatewayError`, so the send verb's exit 5 is unchanged."""


class SecretNotFound(SecretError):
    """Every step missed. Names the steps tried and the holder."""


class SecretStepFailed(SecretError):
    """One step BROKE. Named, and never quietly treated as a miss."""


def env_key(secret_id: str) -> str:
    """The env step's variable name for one secret id, stated once and used everywhere.

    Every run of characters that is not a letter or a digit folds to one underscore and
    the rest upper-cases: `comms/slot-1/names` -> `MOXI_COMMS_SECRET_COMMS_SLOT_1_NAMES`,
    `ANTHROPIC_API_KEY` -> `MOXI_COMMS_SECRET_ANTHROPIC_API_KEY`. Two ids that differ only in
    punctuation therefore share a variable, which is why this is the step for comms' own
    ROLE credentials (a handful of named keys) and not a general back door for bundle
    tokens (which have a store and a provision verb).
    """
    folded = re.sub(r"[^A-Za-z0-9]+", "_", str(secret_id)).strip("_")
    return ENV_PREFIX + folded.upper()


def _holder(owner) -> str:
    """WHO the value was asked for, for an error sentence. Never a path, never a value."""
    return str(owner) if owner is not None else "this process's own holder"


class Resolver:
    """One secret id in, `{"value", "note"}` out, or a miss.

    `get` returns None for a MISS (this resolver does not hold it) and raises
    `SecretStepFailed` when it could not tell. `name` is what the chain's error calls it
    and `describe` is the phrase that reads inside a sentence.
    """

    name = ""

    def describe(self, secret_id: str) -> str:
        return self.name

    def get(self, secret_id: str, *, owner=None) -> dict | None:
        raise NotImplementedError


class EnvResolver(Resolver):
    """Step 1: `MOXI_COMMS_SECRET_<KEY>` out of the injected environment. No note."""

    name = STEP_ENV

    def __init__(self, env=None):
        self.env = env if env is not None else os.environ

    def describe(self, secret_id: str) -> str:
        return f"the environment ({env_key(secret_id)})"

    def get(self, secret_id: str, *, owner=None) -> dict | None:
        value = (self.env.get(env_key(secret_id)) or "").strip()
        if not value:
            return None
        log.info("secret %s: from the environment", secret_id)   # never the value
        return {"value": value, "note": ""}


class StoreResolver(Resolver):
    """Step 2: the custody store's declared read, keyed by the holder that is spending.

    `store` is a `TokenStore`, a path, or None for the configured store; it is a
    CONSTRUCTOR argument, so a test's chain is as fake-able as a test's store.
    """

    name = STEP_STORE

    def __init__(self, store=None, *, env=None):
        from .tokens import TokenStore

        self.env = env if env is not None else os.environ
        self.store = (store if isinstance(store, TokenStore)
                      else TokenStore(store, env=self.env))

    def describe(self, secret_id: str) -> str:
        return "the holder's token store"

    def get(self, secret_id: str, *, owner=None) -> dict | None:
        from .tokens import TokenStoreError

        if owner is None:
            return None                      # a store lookup is keyed by holder
        try:
            value = self.store.read_secret(owner, secret_id)
        except TokenStoreError as e:
            # A store that refuses (0600 wider than the rule) is not a holder without the
            # key: it is a store that could not be read, and falling through would answer
            # the wrong question. The store's refusals are already caller-shaped.
            raise SecretStepFailed(
                f"cannot resolve the posting credential {secret_id}: the token store "
                f"step failed. {e}") from None
        if not value:
            return None
        log.info("secret %s: resolved from the token store", secret_id)   # never which
        return {"value": value, "note": ""}


class Chain(Resolver):
    """The steps in declared order. The only thing the backends hold."""

    name = "chain"

    def __init__(self, steps):
        self.steps = tuple(steps)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    def __repr__(self) -> str:
        return f"Chain({', '.join(self.names)})"

    def get(self, secret_id: str, *, owner=None) -> dict:
        tried = []
        for step in self.steps:
            tried.append(step.describe(secret_id))
            found = step.get(secret_id, owner=owner)
            if found is not None:
                return found
        raise SecretNotFound(_not_found(secret_id, owner, tried,
                                        STEP_STORE in self.names))


def _not_found(secret_id, owner, tried, has_store) -> str:
    """The one miss sentence, in the caller's language. It names the steps and the
    holder and nothing else: not a path, not a key, and never a value."""
    where = ", then ".join(tried) if tried else "nothing (the chain is empty)"
    if has_store:
        tail = "Provisioning writes it into the store; ask the operator."
    else:
        tail = "Nothing was attempted beyond those steps."
    return (f"cannot resolve the posting credential {secret_id}: tried {where}, and none "
            f"of them holds it for {_holder(owner)}. {tail}")


# --------------------------------------------------------------------------- building

def build(names=None, *, env=None, store=None) -> Chain:
    """A chain from step NAMES. The one constructor; every backend gets its chain here.

    `store` is the injection point a test uses. An unknown name raises `SecretError` --
    `config.load` refuses it first, so this is the second fence.
    """
    names = tuple(names) if names is not None else default_chain()
    steps = []
    for name in names:
        if name == STEP_ENV:
            steps.append(EnvResolver(env=env))
        elif name == STEP_STORE:
            steps.append(StoreResolver(store, env=env))
        else:
            raise SecretError(
                f"unknown secret resolver {name!r}: the chain's steps are "
                f"{', '.join(STEPS)} (config.toml [secrets] chain).")
    return Chain(steps)


def from_config(cfg=None, *, env=None, store=None) -> Chain:
    """The box's declared chain. `cfg` is a loaded `CommsConfig`, or None to load."""
    if cfg is None:
        from . import config

        cfg = config.load(env=env)
    if store is None and cfg.secrets.store:
        store = cfg.secrets.store
    return build(cfg.secrets.chain, env=env, store=store)


__all__ = ["Resolver", "EnvResolver", "StoreResolver", "Chain",
           "SecretError", "SecretNotFound", "SecretStepFailed", "env_key", "build",
           "from_config", "ENV_PREFIX", "STEPS", "DEFAULT_CHAIN", "default_chain",
           "STEP_ENV", "STEP_STORE"]
