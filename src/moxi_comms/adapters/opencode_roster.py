"""The opencode roster: the `[opencode]` table of comms' config file, NON-SECRET ids only.

Same law as the codex roster: **which OpenCode servers exist is provisioner-configured,
never register-triggered.** `moxi-comms-opencode` runs a server only for an identity named
here; a `register` on the daemon socket never causes a spawn.

    CONFIG/comms/config.toml

    [opencode.defaults]                # every row takes these unless it names its own
    model     = ["opencode/minimax-m3.1-flash-free", "opencode/glm-5.3-flash-free"]

    [opencode.agents."coder@proj-a"]
    slot      = 6                      # PINNED. The launcher never registers next-free.
    cwd       = "/home/me/src/proj-a"  # best at a git root (see below)
    directors = ["main"]               # THIS agent's declared directors (authority)
    # model   = "opencode/minimax-m3.1-flash-free"   # the launch default; a list names
    #                                                # the fallbacks after it, in order
    # binary  = "/home/me/.opencode/bin/opencode"
    # session_file = ".../opencode/coder@proj-a.json"

**The model is a launch default, and every model named here must be a free one.** The
launcher sets OpenCode's `model` and `small_model` to the first model in `model` that
the server offers for free, and never changes it after that: comms' posts name no
model, and a model the person picks in the window is theirs. Comms never names a paid
model and never adds a credential, a key or a provider entry; the OpenCode sign-in and
environment the person starts the launcher with are theirs and are used as they are.

**A row that names no `model` gets `DEFAULT_MODELS`, in order.** Before it starts the
server, the launcher has OpenCode refresh its model list
(`opencode.OpenCodeAdapter.refresh_models`), so a model retired since OpenCode last
fetched the list is not offered and the next one is chosen.

**The session loads the person's own configuration.** The server runs with the
environment the launcher was started in, so it reads the person's OpenCode config and
sign-in, their `~/.claude/CLAUDE.md` and their skills, as OpenCode does when they run
it themselves. With no git root above `cwd`, OpenCode treats an ancestor `.opencode/`
directory as project config and writes into it; a `cwd` at a git root bounds that.

The roster lives by the config file's refusal rule (`config.refuse_secrets`): a
token-shaped key is a hard stop. `[opencode.defaults]` holds any row key but `slot` and
`session_file`, which name one identity's own things.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import directors as directors_mod
from ..config import ConfigError
from ..paths import opencode_sessions_dir
from ..scoping import GLOBAL, NAME_RE, is_auto_name, qualify, split_tag

#: The launch defaults when a row names none, in order: the free OpenCode Zen models
#: whose provider keeps no data and trains on none. Source: https://opencode.ai/docs/zen/,
#: read 2026-10-20, where each "follows a zero-retention policy and does not use your
#: data for model training"; MiniMax M3.1 Flash Free is free "for a limited time", so a
#: launch whose model list no longer offers it takes the next. Terms and the free list
#: change; a row's `model` overrides the whole list.
DEFAULT_MODELS = ("opencode/minimax-m3.1-flash-free", "opencode/glm-5.3-flash-free",
                  "opencode/deepseek-v4.1-flash-free")
#: The first of them, for the callers that name one.
DEFAULT_MODEL = DEFAULT_MODELS[0]

#: The name the launcher looks for on PATH, then where OpenCode's installer puts it
#: (only an interactive shell adds that directory to PATH).
BINARY = "opencode"
INSTALL_DIR = Path("~/.opencode/bin")

_ENTRY_KEYS = {"slot", "cwd", "directors", "model", "binary", "session_file"}
#: Keys that name one identity's own things, so `[opencode.defaults]` may not hold them.
_OWN_KEYS = {"slot", "session_file"}
#: What `[opencode.defaults]` may hold: the launch model alone. Every other key is set
#: on each row, `directors` above all, since it is one recipient's authority grant.
_DEFAULT_KEYS = {"model"}


@dataclass
class OpenCodeAgent:
    """One roster row: everything the launcher needs about one identity."""

    qualified: str
    name: str
    scope: str
    slot: int
    cwd: str
    session_file: Path
    models: list = field(default_factory=lambda: list(DEFAULT_MODELS))
    binary: str | None = None
    directors: list = field(default_factory=list)   # qualified at load, one spelling

    def env(self, base=None) -> dict:
        """Identity stamped into the server's environment AT SPAWN, scrubbed first.

        OpenCode runs a session's tools in the server's own process, so the agent's
        shell inherits exactly this, and `moxi comms send` there speaks as this identity
        through the launcher's claim socket stamped beside it."""
        from .session import stamped_env

        return stamped_env(base, name=self.name, slot=self.slot, scope=self.scope,
                           global_=self.scope == GLOBAL,
                           project=None if self.scope == GLOBAL else self.scope,
                           directors=self.directors)


@dataclass
class Roster:
    agents: dict = field(default_factory=dict)
    source: str = "defaults"

    def require(self, qualified: str) -> OpenCodeAgent:
        agent = self.agents.get(qualified)
        if agent is None:
            known = ", ".join(sorted(self.agents)) or "(none)"
            raise ConfigError(
                f"{qualified!r} is not in the opencode roster ({self.source}). Which "
                "OpenCode servers exist is provisioner-configured: registering an "
                f"identity never starts one. Add an [opencode.agents.\"{qualified}\"] section "
                "to comms' config file. "
                f"Known: {known}.")
        return agent


def resolve_binary(name: str | None, path: str | None = None) -> str | None:
    """The opencode binary, as an absolute path, or None when nothing resolves: the
    row's `binary`, else `opencode` on PATH, else the installer's directory."""
    if name and os.sep in name:
        candidate = Path(name).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
        return None
    found = shutil.which(name or BINARY, path=path)
    if found:
        return str(Path(found).absolute())
    if name:
        return None
    installed = INSTALL_DIR.expanduser() / BINARY
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    return None


def session_path(qualified: str) -> Path:
    return opencode_sessions_dir() / f"{qualified}.json"


def load(env=None, path=None) -> Roster:
    """The roster from comms' config file (`config.load(env).opencode`), or from the comms
    config file `path` when a launch names one (`--config`). A file with no `[opencode]`
    table is an empty roster; a bad row is refused, loudly."""
    from .. import config as config_mod

    cfg = (config_mod.load_file(path, env=env) if path is not None
           else config_mod.load(env=env))
    return from_table(cfg.opencode, cfg.source)


def from_table(table: dict, source: str = "defaults") -> Roster:
    """The roster from the raw `[opencode]` table, `{"defaults": {...}, "agents":
    {...}}`. `source` is the file the table came from, which every refusal names."""
    defaults = dict((table or {}).get("defaults") or {})
    where = f"{source}: [opencode.defaults]"
    unknown = sorted(set(defaults) - _ENTRY_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; expected "
                          f"{', '.join(sorted(_DEFAULT_KEYS))}")
    own = sorted(set(defaults) - _DEFAULT_KEYS)
    if own:
        raise ConfigError(f"{where}: {', '.join(own)} name(s) one identity's own; set "
                          "them on its [opencode.agents.\"<name>\"] row (the defaults "
                          f"hold only {', '.join(sorted(_DEFAULT_KEYS))})")
    if "model" in defaults and not _models_ok(defaults["model"]):
        raise ConfigError(f"{where}: model must be a \"provider/model\" id or a list of "
                          f"them, not {defaults['model']!r}")
    agents = {}
    for qualified, section in ((table or {}).get("agents") or {}).items():
        if isinstance(section, dict):
            section = {**{k: v for k, v in defaults.items() if k not in section},
                       **section}
        agent = _entry(source, qualified, section)
        agents[agent.qualified] = agent
    return Roster(agents=agents, source=source)


def _models_ok(model) -> bool:
    """A `model` value: one "provider/model" id, or a non-empty list of them."""
    models = [model] if isinstance(model, str) else model
    return (isinstance(models, list) and bool(models)
            and all(isinstance(m, str) and "/" in m for m in models))


def _entry(path, qualified: str, section) -> OpenCodeAgent:
    where = f"{path}: [opencode.agents.\"{qualified}\"]"
    if not isinstance(section, dict):
        raise ConfigError(f"{where} is not a table")
    unknown = sorted(set(section) - _ENTRY_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; expected "
                          f"{', '.join(sorted(_ENTRY_KEYS))}")
    name, scope = split_tag(qualified)
    scope = scope or GLOBAL
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
            "registers with it explicitly, never next-free: the server's environment is "
            "stamped at spawn, before any register could answer.")
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise ConfigError(f"{where}: slot must be a number, not {slot!r}")
    cwd = section.get("cwd")
    if not cwd:
        raise ConfigError(f"{where}: no cwd (the directory the OpenCode session works in)")

    model = section.get("model", list(DEFAULT_MODELS))
    models = [model] if isinstance(model, str) else model
    if not _models_ok(model):
        raise ConfigError(f"{where}: model must be a \"provider/model\" id or a list of "
                          f"them, not {model!r}")

    declared = section.get("directors", [])
    if not isinstance(declared, list):
        raise ConfigError(f"{where}: directors must be a list of names, not "
                          f"{declared!r} -- authority is declared by the RECIPIENT, one "
                          "entry per director, no transitivity")
    try:
        declared = directors_mod.declare(declared)
    except directors_mod.BadDirector as e:
        raise ConfigError(f"{where}: {e}") from e
    binary = section.get("binary")
    if binary is not None and not isinstance(binary, str):
        raise ConfigError(f"{where}: binary must be a path, not {binary!r}")
    return OpenCodeAgent(
        qualified=qualified, name=name, scope=scope, slot=int(slot), cwd=str(cwd),
        session_file=(Path(section["session_file"]) if section.get("session_file")
                      else session_path(qualified)),
        models=list(models), binary=binary, directors=declared)


__all__ = ["BINARY", "DEFAULT_MODEL", "DEFAULT_MODELS", "INSTALL_DIR", "OpenCodeAgent", "Roster",
           "ConfigError", "from_table", "load", "resolve_binary", "session_path"]
