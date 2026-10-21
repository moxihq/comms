"""The announcement of Moxi's comms module, which moxi.modules reads.

Standard library only, and imports nothing of comms: the umbrella reads it to list the
module, dispatch `moxi comms`, offer its extras and check its contract.
"""

INTERFACES = frozenset({1})
#: The module other code calls: sending, the reads, the names, `api.tokens`.
API = "moxi_comms.api"
#: What `moxi comms` runs, in process, as `main(argv, prog="moxi comms")`.
CLI = "moxi_comms.cli:main"

SUMMARY = 'Messages between coding-agent sessions, delivered into their turns.'
OPTIONAL = {
    "codex": "Codex sessions as comms agents: moxi-comms-codex launches each one "
             "through turns' Codex host, which needs the websockets package.",
    "cache": "The cold-send hold: mail to an agent whose prompt cache went cold waits for "
             "its next turn, and a cache comms cannot read refuses the send. Without it "
             "every agent reads as having no cache and gets its mail live.",
}
GATED = {"moxi-comms-codex": "codex"}
#: Each variable of comms' prefix it reads outside moxi.paths and moxi.config, and why.
INTERNAL_ENV = {
    "MOXI_COMMS_NAME": "launcher stamp: the session's chosen identity name",
    "MOXI_COMMS_SLOT": "launcher stamp: the session's slot",
    "MOXI_COMMS_SCOPE": "launcher stamp: the session's identity scope",
    "MOXI_COMMS_GLOBAL": "launcher stamp: the identity is the global one",
    "MOXI_COMMS_DIRECTORS": "launcher stamp: the directors this launch grants",
    "MOXI_COMMS_SOCKET": "launcher stamp: the daemon socket the session was launched "
                         "against",
    "MOXI_COMMS_CLAIM_SOCKET": "launcher stamp: the claim socket the session's adapter "
                               "answers",
    "MOXI_COMMS_PROJECT": "launcher stamp and override: the session's project key",
    "MOXI_COMMS_OPERATOR": "wiring: the operator lane's user id for one send",
    "MOXI_COMMS_SECRET_": "the secrets chain's env step: one credential per variable",
    "MOXI_COMMS_MOCK_GATEWAY": "test seam: the mock record's JSON store",
}
SETTINGS = "settings.toml"
SETUP = None
SEGMENT = None
