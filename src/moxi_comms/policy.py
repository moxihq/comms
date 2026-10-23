"""Names with special treatment, from the `[policy]` table of comms' config file.

Two rules name identities rather than kinds:

- `cache_callers`: the callers `moxi comms cache` answers. The key is the whole list,
  `["operator"]` by default; a box that adds a caller lists `operator` too:

      [policy]
      cache_callers = ["operator", "planner@proj"]

- `labels`: the words the send and reply verbs use when they refuse a send-only
  recipient, by qualified name. An identity without a label is "a send-only identity":

      [policy]
      labels = { "notifier@proj" = "the notifier, which only sends" }

Neither is about who may send to a cold cache: no sender is exempt from the cold-cache
hold, the operator's lane and every send-only identity included. Two keys an earlier comms
read, `[policy] never_held` and `next_turn_refused`, are declared retired in
`settings.toml`, so a config file that still names one is refused with the fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The human's identity name on any install, and the one default cache caller.
OPERATOR = "operator"

#: What a send-only recipient is called when `[policy] labels` names no words for it.
SEND_ONLY = "a send-only identity"


@dataclass(frozen=True)
class Policy:
    cache_callers: tuple = ()
    labels: dict = field(default_factory=dict, hash=False)

    def refusal_words(self, qualified: str) -> str:
        """What the send-only refusal calls `qualified`: its label, else `SEND_ONLY`."""
        return self.labels.get(qualified, SEND_ONLY)


#: The policy with no config file: the operator alone answers to `moxi comms cache`.
DEFAULTS = Policy(cache_callers=(OPERATOR,))


def defaults() -> Policy:
    return DEFAULTS


def merged(base: Policy, table) -> Policy:
    """`base` under a parsed `[policy]` table (`config.PolicyConfig`): the table's
    `cache_callers` is the whole list and replaces base's; its labels are added to
    base's, the table's words winning for a name both carry."""
    if table is None:
        return base
    return Policy(cache_callers=tuple(table.cache_callers),
                  labels={**base.labels, **dict(table.labels)})


def for_env(env=None) -> Policy:
    """This box's policy: the config file's `[policy]` over the defaults."""
    from . import config

    return merged(DEFAULTS, config.load(env=env).policy)


__all__ = ["Policy", "DEFAULTS", "defaults", "merged", "for_env", "OPERATOR", "SEND_ONLY"]
