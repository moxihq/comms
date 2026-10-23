"""The codex roster: the `[codex]` table of comms' config file, NON-SECRET ids only.

**Which app-servers exist is provisioner-configured, never register-triggered.**
The `moxi-comms-codex` launcher runs an app-server only for an identity named here; an
inbound `register` on the daemon socket never causes a spawn. The file holds, per
identity: the pinned slot, the cwd, the sandbox/permission profile, and the thread-id
file.

    CONFIG/comms/config.toml

    [codex.defaults]                   # every row takes these unless it names its own
    sandbox        = "workspace-write"

    [codex.agents."planner@proj-a"]
    slot           = 3                 # PINNED. The launcher never registers next-free.
    cwd            = "/home/me/src/proj-a"
    sandbox        = "workspace-write" # or `permissions = "<profile-id>"`, not both
    writable_roots = ["/run/user/1000/moxi"]
    network_access = true              # see below
    directors      = ["main"]          # THIS agent's declared directors (authority)
    # thread_file  = ".../codex/planner@proj-a.json"   # default: STATE/comms/codex/<q>.json
    # model        = "<model id>"

The roster lives by the config file's refusal rule (`config.refuse_secrets`): a
token-shaped key is a hard stop. A roster is exactly where a credential would go if
nobody stopped it.

`[codex.defaults]` holds any row key but the ones that name one identity's own things
(`slot`, `thread_file`). A row that names `sandbox`, `permissions` or
`profile` takes none of the three from the defaults: they are one choice, and the
schema refuses `sandbox` beside `permissions`.

`directors` is the codex half of the authority declaration, the roster's answer to
`moxi-comms-claude --director`. It is the RECIPIENT's grant -- listing `main` under
`planner@proj-a` says planner treats main's verified messages as directives, and says
nothing about how anyone else sees main (the global main: a bare name is the global
identity). Names are qualified once at load, one spelling per identity, and a malformed entry is refused like every other bad
key here.

**EXPLICITLY INTERIM:** `network_access`
defaults to **true**, i.e. full outbound network for the codex agent. This is not a
preference. On Linux/codex 0.146.0 an AF_UNIX `connect()` out of the codex sandbox is
gated solely by `networkAccess` -- `network.unix_sockets` is inert without it and
unenforced with it -- so there is no least-privilege configuration that grants the
daemon socket, and the reply path (the agent runs `moxi comms reply` itself) needs
that socket. Truth in labeling: this makes agent and daemon mutually un-isolated, and
`thread/shellCommand` on the control socket already ran unsandboxed on the host. Never
describe a codex comms agent as contained. Destination: syscall-level `unix_sockets`
enforcement upstream; revisit when codex ships it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import directors as directors_mod
from ..config import ConfigError
from ..paths import codex_threads_dir
from ..scoping import GLOBAL, NAME_RE, is_auto_name, qualify, split_tag

log = logging.getLogger("comms.codex.roster")

DEFAULT_SANDBOX = "workspace-write"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
SANDBOX_POLICY_TYPES = {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                        "danger-full-access": "dangerFullAccess"}

_ENTRY_KEYS = {"slot", "cwd", "thread_file", "sandbox", "permissions",
               "profile", "writable_roots", "network_access", "model", "directors"}
#: Keys that name one identity's own things, so `[codex.defaults]` may not hold them.
_OWN_KEYS = {"slot", "thread_file"}
#: What `[codex.defaults]` may hold: the sandbox mode alone. Every other key is set on
#: each row, `directors` above all, since it is one recipient's authority grant.
_DEFAULT_KEYS = {"sandbox"}
#: One choice: a row naming any of them takes none of them from the defaults.
_CONTAINMENT_KEYS = {"sandbox", "permissions", "profile"}


@dataclass
class CodexAgent:
    """One roster row: everything the launcher needs about one identity."""

    qualified: str
    name: str
    scope: str
    slot: int
    cwd: str
    thread_file: Path
    sandbox: str | None = DEFAULT_SANDBOX
    permissions: str | None = None
    profile: dict = field(default_factory=dict)
    writable_roots: list = field(default_factory=list)
    network_access: bool = True
    model: str | None = None
    directors: list = field(default_factory=list)   # qualified at load, one spelling

    # ---------------------------------------------------------------- app-server
    def env(self, base=None) -> dict:
        """Identity stamped into the app-server's environment AT SPAWN.

        The agent's shell inherits this, with the launcher's claim socket beside it,
        which is how `moxi comms send` inside the thread knows who it is speaking as (it
        reads the claim socket; the stamp alone sends nothing). The slot comes from the roster rather than from
        the register response because the environment is fixed at spawn and the slot
        would otherwise be decided later -- which is exactly why the roster pins it.

        **Scrubbed before it is stamped.** Overlaying the stamp onto an inherited
        environment would let any key the roster has no value for keep whatever the
        process that started `moxi-comms-codex` was carrying -- `MOXI_COMMS_DIRECTORS` above all,
        which would hand this agent its launcher's authority. `stamped_env` handles it,
        for all three stamp sites at once.
        """
        from .session import stamped_env           # one implementation of the stamp

        return stamped_env(base, name=self.name, slot=self.slot, scope=self.scope,
                           global_=self.scope == GLOBAL,
                           project=None if self.scope == GLOBAL else self.scope,
                           directors=self.directors)

    # ---------------------------------------------------------------- containment
    def effective_writable_roots(self, added_root=None) -> list:
        """The row's own writable roots, then the added one when it is not among them."""
        roots = [str(r) for r in self.writable_roots]
        if added_root is not None and not self.permissions:
            added = str(added_root)
            known = {os.path.realpath(r) for r in roots}
            if os.path.realpath(added) not in known:
                roots.append(added)
        return roots

    def launch_options(self, added_root=None) -> dict:
        """The roster-owned options for every `thread/start` and `thread/resume`.

        The first resume that loads a thread into an app-server process fixes the
        thread's sandbox for that process, and a resume that carries only the thread id
        loads it read-only with the network off. So a resume carries exactly what the
        start carried, or a relaunched agent can no longer reach the daemon socket.

        `added_root` is the directory the agent's own `comms` writes the record into
        (`record_write_root`). It is appended to the row's writable roots. A row with a
        named permission profile is its writer's own containment, and gets nothing added.

        The config also turns Codex's multi-agent mode off: a comms identity is one
        agent among comms' agents, and is not told that it leads a team of its own.

        turns owns cwd, developer instructions and approval policy. This mapping omits
        those keys so turns can lay it, unread, over each load.
        """
        params: dict = {}
        config: dict = {"agents": {"enabled": False}}
        if self.permissions:
            # A named permission profile; `sandbox` cannot be combined with it (schema).
            params["permissions"] = self.permissions
            if self.profile:
                config["permissions"] = {self.permissions: dict(self.profile)}
        else:
            params["sandbox"] = self.sandbox
            config["sandbox_workspace_write"] = {
                "writable_roots": self.effective_writable_roots(added_root),
                # The one switch that lets the agent's shell reach the daemon socket.
                "network_access": bool(self.network_access),
            }
        params["config"] = config
        if self.model:
            params["model"] = self.model
        return params

    def verify_load(self, options: dict, response: dict | None) -> str | None:
        """Why a load answer does not report the roster's requested sandbox."""
        mode = options.get("sandbox")
        reported = (response or {}).get("sandbox")
        if not mode or not isinstance(reported, dict):
            return None
        want = SANDBOX_POLICY_TYPES.get(mode)
        if want and reported.get("type") != want:
            return f"sandbox is {reported.get('type')!r}, not {want!r}"
        if want != "workspaceWrite":
            return None
        settings = (options.get("config") or {}).get("sandbox_workspace_write") or {}
        if bool(reported.get("networkAccess")) != bool(settings.get("network_access")):
            return (f"network access is {'on' if reported.get('networkAccess') else 'off'}, "
                    f"not {'on' if settings.get('network_access') else 'off'}")
        # The app-server omits cwd from writableRoots because workspace-write grants it.
        have = {os.path.realpath(str(root))
                for root in reported.get("writableRoots") or []}
        for cwd in ((response or {}).get("cwd"), self.cwd):
            if cwd:
                have.add(os.path.realpath(str(cwd)))
        missing = [root for root in settings.get("writable_roots") or []
                   if os.path.realpath(str(root)) not in have]
        if missing:
            return f"writable roots lack {', '.join(missing)}"
        return None


@dataclass
class Roster:
    agents: dict = field(default_factory=dict)
    source: str = "defaults"

    def require(self, qualified: str) -> CodexAgent:
        agent = self.agents.get(qualified)
        if agent is None:
            known = ", ".join(sorted(self.agents)) or "(none)"
            raise ConfigError(
                f"{qualified!r} is not in the codex roster ({self.source}). Which "
                "app-servers exist is provisioner-configured, never register-triggered: "
                f"registering an identity never starts one -- add a [codex.agents.\"{qualified}\"] "
                f"section to comms' config file. Known: {known}.")
        return agent


def record_write_root(env=None, cwd=None) -> tuple:
    """The directory the agent's own `comms` writes the record into, and why.

    Returns `(directory or None, reason)`. It is read the way the CLI reads it, from the
    same environment and the same config file, because the agent's shell inherits the
    launcher's environment:

      * `local`: the parent of the record (`MOXI_COMMS_LOCAL_RECORD`, else `[local] record`,
        else `STATE/comms/record.db`). Every local send writes the record and its WAL
        from the sender's own process.
      * `mock`: the directory of the `MOXI_COMMS_MOCK_GATEWAY` store, which the mock writes
        through a temp file beside it.

    A relative path resolves against `cwd`, the agent's working directory, as the CLI
    run from the agent's shell would resolve it.
    """
    from .. import config as config_mod
    from ..gateway import LOCAL, MOCK, GatewayError, backend_name
    from ..paths import record_db

    env = os.environ if env is None else env
    try:
        backend = backend_name(None, env=env)
    except (GatewayError, ConfigError) as e:
        return None, f"comms' config could not be read ({e})"

    def absolute(value) -> Path:
        path = Path(os.path.expanduser(str(value)))
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        return Path(os.path.normpath(path))

    if backend == LOCAL:
        try:
            record = config_mod.load(env=env).local.record
        except ConfigError as e:
            return None, f"comms' config could not be read ({e})"
        source = absolute(record) if record else record_db()
        return source.parent, f"the local backend's record ({source})"
    if backend == MOCK:
        store = env.get("MOXI_COMMS_MOCK_GATEWAY")
        if not store:
            return None, "the mock backend names no store (MOXI_COMMS_MOCK_GATEWAY is unset)"
        source = absolute(store)
        return source.parent, f"the mock backend's store ({source})"
    return None, f"backend {backend!r} writes nothing locally"


def load(env=None, path=None) -> Roster:
    """The roster from comms' config file (`config.load(env).codex`), or from the comms
    config file `path` when a launch names one (`--config`). A file with no `[codex]`
    table is an empty roster; a bad row is refused, loudly."""
    from .. import config as config_mod

    cfg = (config_mod.load_file(path, env=env) if path is not None
           else config_mod.load(env=env))
    return from_table(cfg.codex, cfg.source)


def from_table(table: dict, source: str = "defaults") -> Roster:
    """The roster from the raw `[codex]` table, `{"defaults": {...}, "agents": {...}}`.
    `source` is the file the table came from, which every refusal names."""
    defaults = dict((table or {}).get("defaults") or {})
    where = f"{source}: [codex.defaults]"
    unknown = sorted(set(defaults) - _ENTRY_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; expected "
                          f"{', '.join(sorted(_DEFAULT_KEYS))}")
    own = sorted(set(defaults) - _DEFAULT_KEYS)
    if own:
        raise ConfigError(f"{where}: {', '.join(own)} name(s) one identity's own; set "
                          "them on its [codex.agents.\"<name>\"] row (the defaults "
                          f"hold only {', '.join(sorted(_DEFAULT_KEYS))})")
    sandbox = defaults.get("sandbox")
    if "sandbox" in defaults and sandbox not in SANDBOX_MODES:
        raise ConfigError(f"{where}: sandbox {sandbox!r} is not one of "
                          f"{', '.join(SANDBOX_MODES)}")
    agents = {}
    for qualified, section in ((table or {}).get("agents") or {}).items():
        agent = _entry(source, qualified, _with_defaults(defaults, section))
        agents[agent.qualified] = agent
    return Roster(agents=agents, source=source)


def _with_defaults(defaults: dict, section):
    if not isinstance(section, dict) or not defaults:
        return section
    taken = {k: v for k, v in defaults.items() if k not in section}
    if set(section) & _CONTAINMENT_KEYS:
        taken = {k: v for k, v in taken.items() if k not in _CONTAINMENT_KEYS}
    return {**taken, **section}


def _entry(path, qualified: str, section) -> CodexAgent:
    where = f"{path}: [codex.agents.\"{qualified}\"]"
    if not isinstance(section, dict):
        raise ConfigError(f"{where} is not a table")
    unknown = sorted(set(section) - _ENTRY_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; expected "
                          f"{', '.join(sorted(_ENTRY_KEYS))}")
    name, scope = split_tag(qualified)
    scope = scope or GLOBAL
    # `planner@global` and `planner` are the same identity: a global name renders bare.
    # Normalising here means the roster key can be written either way and the thread
    # file, the cursor and the daemon's binding still agree on one string.
    qualified = qualify(name, scope)
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: {name!r} is not a valid name")
    if is_auto_name(name):
        raise ConfigError(
            f"{where}: the agent-N namespace is reserved for the daemon's auto-namer in "
            "every scope; a roster identity is chosen or standing, never ephemeral")

    slot = section.get("slot")
    if slot is None:
        raise ConfigError(
            f"{where}: no slot. The roster PINS the slot per identity and the launcher "
            "registers with it explicitly, never next-free -- the app-server's "
            "environment is stamped at spawn, before any register could answer.")
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise ConfigError(f"{where}: slot must be a number, not {slot!r}")

    cwd = section.get("cwd")
    if not cwd:
        raise ConfigError(f"{where}: no cwd (the directory the codex thread works in)")

    sandbox, permissions = section.get("sandbox"), section.get("permissions")
    if sandbox and permissions:
        raise ConfigError(
            f"{where}: `sandbox` and `permissions` cannot be combined -- a named "
            "permission profile replaces the sandbox mode (app-server schema).")
    if sandbox and sandbox not in SANDBOX_MODES:
        raise ConfigError(f"{where}: sandbox {sandbox!r} is not one of "
                          f"{', '.join(SANDBOX_MODES)}")
    profile = section.get("profile") or {}
    if profile and not permissions:
        raise ConfigError(f"{where}: `profile` needs the `permissions` id it defines")

    # The recipient's grant, qualified exactly as `moxi-comms-claude --director` qualifies
    # at launch: a bare name is the global identity, `name@scope` is explicit, a global
    # one renders bare.
    declared = section.get("directors", [])
    if not isinstance(declared, list):
        raise ConfigError(f"{where}: directors must be a list of names, not "
                          f"{declared!r} -- authority is declared by the RECIPIENT, one "
                          "entry per director, no transitivity")
    try:
        declared = directors_mod.declare(declared)
    except directors_mod.BadDirector as e:
        raise ConfigError(f"{where}: {e}") from e

    network_access = section.get("network_access", True)
    agent = CodexAgent(
        qualified=qualified, name=name, scope=scope, slot=int(slot), cwd=str(cwd),
        thread_file=(Path(section["thread_file"]) if section.get("thread_file")
                     else codex_threads_dir() / f"{qualified}.json"),
        sandbox=None if permissions else (sandbox or DEFAULT_SANDBOX),
        permissions=permissions,
        profile=dict(profile),
        writable_roots=list(section.get("writable_roots") or []),
        network_access=bool(network_access),
        model=section.get("model"),
        directors=declared,
    )
    if not permissions and not agent.network_access:
        # Honored, but said out loud: with networkAccess off the agent cannot reach the
        # daemon socket at all, so `moxi comms reply` inside the thread fails (verified).
        log.warning("%s: network_access is off, so this agent cannot reach the daemon "
                    "socket and cannot run `moxi comms reply`", where)
    return agent


__all__ = ["DEFAULT_SANDBOX", "SANDBOX_MODES", "CodexAgent", "Roster", "ConfigError",
           "from_table", "load", "record_write_root"]
