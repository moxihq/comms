"""comms' settings: one config file, CONFIG/comms/config.toml, read through moxi.config.

Every key is declared in `settings.toml` beside this file, with its type, default, one
line of doc and, for a few, the one variable that overrides it for one process:

    backend = "local"                       # or "mock"         MOXI_COMMS_GATEWAY
    block = false                           # read at each use

    [local]
    record      = "/var/lib/.../record.db"  # empty: STATE/comms/record.db
    retain_days = 90                        # 0 disables the trim

    [threads]
    reply_cap = 8                           # an open thread's default reply cap

    [secrets]
    chain = ["env", "store"]                # the resolver chain, in order
    store = "~/.../tokens"                  # empty: STATE/comms/tokens
    roles = ["calendar"]                    # role holders this box adds

    [policy]
    cache_callers = ["operator"]            # the identities `moxi comms cache` answers
    labels = { "notifier@proj" = "the notifier, which only sends" }

    [provision]
    slots = 6                               # what a first start provisions
    operator = "operator"                   # the human's identity name

    [replay]
    messages = 50                           # a reader's catch-up bound with no cursor
    hours = 24

    [cache]
    required = false                        # true: the daemon refuses to start
                                            # without the cache module

    [codex.agents."planner@proj-a"]         # the Codex roster (adapters/codex_roster)
    [opencode.agents."planner@proj-a"]      # the OpenCode roster

`load(env=None)` returns a `CommsConfig`: the typed tables comms reads, with the
rosters' `codex` and `opencode` tables as raw dicts for their own parsers. A test points
`MOXI_COMMS_CONFIG_HOME` (or `MOXI_COMMS_HOME`) at a directory of its own.

Everything here is a public id. The file is REFUSED if it carries anything that looks
like a token: a config file is exactly the place a token would go if nobody stopped it,
so this module stops it out loud rather than trusting discipline.
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from moxi import config as _moxi_config
from moxi.paths import Layout

from .release import MODULE

_TOKENISH = ("token", "secret_value", "password", "webhook_url")

#: The schema's resource, in this package (the announcement's `SETTINGS`).
SETTINGS = "settings.toml"

#: comms' own role holders, which `[secrets] roles` may not name again.
BUILTIN_ROLES = ("reader", "daemon", "provisioner", "operator")


class ConfigError(_moxi_config.ConfigError):
    """A config file that cannot be read: names the file, the key and the fix.

    Raised with moxi.config's four parts (`file, key, problem, fix`), or with one
    message, as the roster parsers raise it.
    """

    def __init__(self, *args):
        if len(args) == 4:
            super().__init__(*args)
        else:
            Exception.__init__(self, *args)
            self.file = self.key = None
            self.problem = str(args[0]) if args else ""
            self.fix = ""


@lru_cache(maxsize=1)
def schema() -> _moxi_config.Schema:
    """The declared keys, read once from the package's `settings.toml`."""
    return _moxi_config.Schema.from_resource(__package__, SETTINGS)


def _default(key: str):
    return schema().keys[key].default


#: The local record's retention, owned by the schema: the gateway's default reads it.
LOCAL_RETAIN_DAYS = _default("local.retain_days")


@dataclass
class LocalConfig:
    """`[local]`. An empty `record` means `paths.record_db()`."""

    record: str = ""
    retain_days: int = LOCAL_RETAIN_DAYS


@dataclass
class ThreadsConfig:
    """`[threads]`: the default reply cap of a thread whose root states `[open]`, for a
    thread whose opener set none. A whole number, 1 or more."""

    reply_cap: int = _default("threads.reply_cap")


@dataclass
class SecretsConfig:
    """`[secrets]`: WHICH resolvers this box has, in what order, where the token store
    is (empty: `paths.tokens_dir()`), and the role holders this box adds."""

    chain: tuple = tuple(_default("secrets.chain"))
    store: str = ""
    roles: tuple = ()


@dataclass
class PolicyConfig:
    """`[policy]`: the identities `moxi comms cache` answers, and each send-only
    identity's refusal words, by qualified name."""

    cache_callers: tuple = tuple(_default("policy.cache_callers"))
    labels: dict = field(default_factory=dict)


@dataclass
class ProvisionConfig:
    """`[provision]`: what a first start makes in an empty directory."""

    slots: int = _default("provision.slots")
    operator: str = _default("provision.operator")


@dataclass
class ReplayConfig:
    """`[replay]`: a reader's catch-up bound when it has no cursor."""

    messages: int = _default("replay.messages")
    hours: int = _default("replay.hours")

    @property
    def seconds(self) -> int:
        return self.hours * 3600


@dataclass
class CacheConfig:
    """`[cache]`: whether the daemon refuses to start when the cache module is not
    present, so the cold-send hold cannot switch off without a word."""

    required: bool = _default("cache.required")


@dataclass
class CommsConfig:
    backend: str = _default("backend")
    local: LocalConfig = field(default_factory=LocalConfig)
    threads: ThreadsConfig = field(default_factory=ThreadsConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    provision: ProvisionConfig = field(default_factory=ProvisionConfig)
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    block: bool = _default("block")
    #: The rosters' tables, raw: `{"defaults": {...}, "agents": {...}}`.
    codex: dict = field(default_factory=dict)
    opencode: dict = field(default_factory=dict)
    #: The file's path when it exists, else "defaults".
    source: str = "defaults"
    #: Where each key's value came from: `default`, `file` or `env` (moxi.config).
    sources: dict = field(default_factory=dict)


#: The name a pre-Moxi caller used for the whole.
GatewayConfig = CommsConfig


def config_path(env=None) -> Path:
    """CONFIG/comms/config.toml, under the roots `env` (else the process) names."""
    return Layout(MODULE).config_file(env if env is not None else os.environ)


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as e:
        return ("error", str(e))
    return (st.st_size, st.st_mtime_ns, st.st_ino)


#: The secrets check's last answer per file, keyed by the file's stamp, so a key read at
#: each use (`block`) parses the TOML again only when the file changed.
_checked: dict[Path, tuple[tuple, ConfigError | None]] = {}
_checked_lock = threading.Lock()


def _check_file(path: Path) -> None:
    """Refuse a file that is not TOML or that holds a token-shaped key."""
    stamp = _stamp(path)
    if stamp is None:
        return
    with _checked_lock:
        hit = _checked.get(path)
    if hit is not None and hit[0] == stamp:
        error = hit[1]
    else:
        error = None
        try:
            refuse_secrets(path, tomllib.loads(path.read_text()))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            error = ConfigError(path, None, f"is not readable TOML ({e})",
                                "correct the file's TOML syntax")
        except ConfigError as e:
            error = e
        with _checked_lock:
            _checked[path] = (stamp, error)
    if error is not None:
        raise error


def load(env=None) -> CommsConfig:
    """The file, then each key's declared `env` override. A missing file is every
    default; a bad one raises `ConfigError` naming the file, the key and the fix.

    A one-shot reader (a verb, an adapter at start) calls this. A process that runs all
    day reads through `Config`, which keeps the last good settings."""
    environ = env if env is not None else os.environ
    path = config_path(environ)
    _check_file(path)
    try:
        settings = _moxi_config.load(MODULE, schema(), env=environ)
    except ConfigError:
        raise
    except _moxi_config.ConfigError as e:
        raise ConfigError(e.file, e.key, e.problem, e.fix) from None
    return _typed(settings, path)


class Config(_moxi_config.Config):
    """The reader a long-running comms process keeps (the daemon): moxi.config's
    `Config` over comms' whole read, so the token refusal and comms' own range checks
    hold at use as at start.

    `start()` reads the file once, and a `ConfigError` there refuses the start.
    `current()` reads it again at each use (moxi.config re-parses only a changed file);
    while the file is in error it returns the last good settings and reports the error
    once per change of the file.
    """

    def __init__(self, *, env=None, report=None):
        super().__init__(MODULE, schema(), env=env, report=report)

    def start(self) -> CommsConfig:
        settings = load(self._env)
        with self._lock:
            self._good = settings
            self._reported = None
        return settings

    def current(self) -> CommsConfig:
        with self._lock:
            if self._good is None:
                raise RuntimeError("Config.current() before start()")
            try:
                self._good = load(self._env)
                self._reported = None
            except _moxi_config.ConfigError as exc:
                stamp = _stamp(config_path(self._env if self._env is not None
                                           else os.environ))
                if stamp != self._reported:
                    self._reported = stamp
                    self._report(exc)
            return self._good

    def use(self, key: str):
        """One table or key of the settings now, by attribute name (`block`, `replay`)."""
        return getattr(self.current(), key)


def load_file(path, env=None) -> CommsConfig:
    """A comms config file named on a command line (`moxi-comms-codex --config PATH`),
    read whole in place of the box's: the same schema, the same token refusal, the same
    checks. Nothing else in the process reads it; the caller takes the table it named
    the file for (a launcher, its roster).

    moxi.config reads a module's file only at its layout's path, so `path` is read in
    place through a link under a private config root, and every error names `path`."""
    import tempfile

    path = Path(os.path.expanduser(str(path)))
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise ConfigError(path, None, "is not a file",
                          "name a comms config file (the format of config.toml)")
    environ = dict(env if env is not None else os.environ)
    with tempfile.TemporaryDirectory(prefix="moxi-comms-config-") as root:
        environ["MOXI_COMMS_CONFIG_HOME"] = root
        link = config_path(environ)
        link.parent.mkdir(parents=True)
        link.symlink_to(path.resolve())
        try:
            cfg = load(environ)
        except ConfigError as e:
            if e.file is None or Path(e.file) != link:
                raise
            raise ConfigError(path, e.key, e.problem, e.fix) from None
        finally:
            with _checked_lock:
                _checked.pop(link, None)
    cfg.source = str(path)
    return cfg


def _typed(settings, path: Path) -> CommsConfig:
    v = settings.values

    def bad(key: str, problem: str, fix: str) -> ConfigError:
        source = settings.source(key)
        where = (f"environment variable {schema().keys[key].env}" if source == "env"
                 else path)
        return ConfigError(where, key, problem, fix)

    record = v["local.record"]
    if v["threads.reply_cap"] < 1:
        raise bad("threads.reply_cap", f"is {v['threads.reply_cap']}",
                  "set it to a whole number of replies, 1 or more")
    if v["local.retain_days"] < 0:
        raise bad("local.retain_days", f"is {v['local.retain_days']}",
                  "set it to a whole number of days, or 0 to keep every message")
    for key in ("provision.slots", "replay.messages"):
        if v[key] < 1:
            raise bad(key, f"is {v[key]}", "set it to 1 or more")
    if v["replay.hours"] < 0:
        raise bad("replay.hours", f"is {v['replay.hours']}", "set it to 0 or more hours")
    operator = v["provision.operator"].strip()
    if not operator:
        raise bad("provision.operator", "is empty", "name the human's identity")

    return CommsConfig(
        backend=v["backend"],
        local=LocalConfig(record=str(record) if record is not None else "",
                          retain_days=v["local.retain_days"]),
        threads=ThreadsConfig(reply_cap=v["threads.reply_cap"]),
        secrets=SecretsConfig(chain=_chain(bad, v["secrets.chain"]),
                              store=str(v["secrets.store"] or ""),
                              roles=_roles(bad, v["secrets.roles"])),
        policy=PolicyConfig(cache_callers=_names(bad, "policy.cache_callers",
                                                 v["policy.cache_callers"]),
                            labels=_labels(bad, v["policy.labels"])),
        provision=ProvisionConfig(slots=v["provision.slots"], operator=operator),
        replay=ReplayConfig(messages=v["replay.messages"], hours=v["replay.hours"]),
        cache=CacheConfig(required=v["cache.required"]),
        block=v["block"],
        codex=_roster(bad, "codex", v),
        opencode=_roster(bad, "opencode", v),
        source=str(path) if path.exists() else "defaults",
        sources=dict(settings.sources),
    )


def _chain(bad, chain) -> tuple:
    """`[secrets] chain`, validated HERE: a name outside the steps is refused at config
    load, so a typo fails the box's first read of its own config and not the first send
    of the day."""
    from .secrets import STEPS

    names = []
    for name in chain:
        text = str(name).strip()
        if text not in STEPS:
            raise bad("secrets.chain", f"names {text!r}, which is not a resolver",
                      f"use the steps {', '.join(STEPS)}")
        names.append(text)
    if not names:
        raise bad("secrets.chain", "is empty, so nothing on this box could resolve a "
                  "credential", f"name at least one of {', '.join(STEPS)}, or remove "
                  "the key for the default")
    return tuple(names)


def _names(bad, key: str, value) -> tuple:
    names = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise bad(key, f"holds {item!r}", 'list qualified names, e.g. ["name@scope"]')
        names.append(item.strip())
    return tuple(names)


def _roles(bad, value) -> tuple:
    """`[secrets] roles`: each a token-store name of its own, and none of comms' own."""
    from .tokens import COMPONENT_RE

    roles = []
    for item in _names(bad, "secrets.roles", value):
        if item in (".", "..") or not COMPONENT_RE.match(item):
            raise bad("secrets.roles", f"holds {item!r}, which is not a token-store "
                      "name", "use letters, digits, dot, dash and underscore")
        if item in BUILTIN_ROLES:
            raise bad("secrets.roles", f"holds {item!r}, one of comms' own roles",
                      "remove it; comms knows it already")
        roles.append(item)
    return tuple(roles)


def _labels(bad, value) -> dict:
    labels = {}
    for name, words in value.items():
        if not isinstance(words, str) or not words.strip():
            raise bad("policy.labels", f"maps {name!r} to {words!r}",
                      "map each qualified name to the words its refusal uses")
        labels[str(name)] = words.strip()
    return labels


def _roster(bad, table: str, v) -> dict:
    out = {}
    for part in ("defaults", "agents"):
        value = v[f"{table}.{part}"]
        if part == "agents":
            for name, row in value.items():
                if not isinstance(row, dict):
                    raise bad(f"{table}.agents", f"holds {name!r} as a value",
                              f'write it as [{table}.agents."{name}"] with its keys')
        out[part] = value
    return out


def refuse_secrets(path: Path, raw: dict) -> None:
    """The refusal every comms config table shares: a token-shaped key is a hard stop."""
    def walk(node, trail):
        if isinstance(node, dict):
            for k, v in node.items():
                if any(t in k.lower() for t in _TOKENISH):
                    raise ConfigError(
                        path, ".".join(trail + [k]), "looks like a secret",
                        "remove it: the config file holds non-secret ids only, and "
                        "tokens live in the token store")
                walk(v, trail + [k])
    walk(raw, [])


__all__ = ["Config", "ConfigError", "LocalConfig", "ThreadsConfig", "SecretsConfig", "PolicyConfig",
           "ProvisionConfig", "ReplayConfig", "CommsConfig", "GatewayConfig",
           "BUILTIN_ROLES", "config_path", "load", "load_file", "refuse_secrets", "schema",
           "LOCAL_RETAIN_DAYS"]
