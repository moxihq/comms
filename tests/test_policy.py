"""`[policy]`: the callers `moxi comms cache` answers, and the send-only refusal's
words, from comms' config file (design §7.2-§7.4)."""

from __future__ import annotations

import pytest
from conftest import write_config

from moxi_comms import config, policy


def test_with_no_file_the_operator_alone_is_a_cache_caller():
    got = policy.for_env()
    assert got == policy.DEFAULTS == policy.defaults()
    assert got.cache_callers == (policy.OPERATOR,) == ("operator",)
    assert got.labels == {}


def test_cache_callers_is_the_whole_list():
    """A box that adds a caller lists `operator` too; one that leaves it out has
    dropped it."""
    write_config(None, '[policy]\ncache_callers = ["operator", "planner@proj"]\n')
    assert policy.for_env().cache_callers == ("operator", "planner@proj")
    write_config(None, '[policy]\ncache_callers = ["planner@proj"]\n')
    assert policy.for_env().cache_callers == ("planner@proj",)


def test_a_label_names_a_send_only_identity_and_the_rest_are_generic():
    write_config(None, '[policy.labels]\n"console@proj" = "the build console\'s '
                       'send-only identity"\n')
    got = policy.for_env()
    assert got.refusal_words("console@proj") == "the build console's send-only identity"
    assert got.refusal_words("other@proj") == policy.SEND_ONLY == "a send-only identity"


def test_merged_takes_the_tables_callers_whole_and_adds_its_labels():
    base = policy.Policy(cache_callers=("operator",), labels={"a@x": "A", "b@x": "B"})
    table = config.PolicyConfig(cache_callers=("pal@x",), labels={"b@x": "B2", "c@x": "C"})
    got = policy.merged(base, table)
    assert got.cache_callers == ("pal@x",)
    assert got.labels == {"a@x": "A", "b@x": "B2", "c@x": "C"}
    assert policy.merged(base, None) is base


def test_the_policy_reads_a_given_environment(tmp_path):
    other = {"MOXI_COMMS_CONFIG_HOME": str(tmp_path / "cfg"), "HOME": str(tmp_path)}
    write_config(other, '[policy]\ncache_callers = ["pal@x"]\n')
    assert policy.for_env(other).cache_callers == ("pal@x",)
    assert policy.for_env().cache_callers == ("operator",)


@pytest.mark.parametrize("key", ["never_held", "next_turn_refused", "voice_companion"])
def test_a_retired_policy_key_is_refused_with_its_fix(key):
    write_config(None, f'[policy]\n{key} = ["x"]\n')
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert caught.value.key == f"policy.{key}"
    assert "remove it" in caught.value.fix or "remove it" in str(caught.value)


def test_the_fleet_names_are_gone():
    for gone in ("THINKER", "CONSOLE", "FLEET_DEFAULTS", "STANDALONE_DEFAULTS"):
        assert not hasattr(policy, gone), gone
    assert not hasattr(policy.Policy(), "never_held")
    assert not hasattr(policy.Policy(), "voice_companion")
