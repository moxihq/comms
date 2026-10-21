"""comms' locations: its own five directories under Moxi's roots (`moxi.paths`).

    DATA/comms      ~/.local/share/moxi/comms/            MOXI_COMMS_DATA_HOME
    STATE/comms     ~/.local/state/moxi/<device>/comms/   MOXI_COMMS_STATE_HOME
    CACHE/comms     ~/.cache/moxi/<device>/comms/         MOXI_COMMS_CACHE_HOME
    CONFIG/comms    ~/.config/moxi/comms/                 MOXI_COMMS_CONFIG_HOME
    RUNTIME/comms   $XDG_RUNTIME_DIR/moxi/comms/          MOXI_COMMS_RUNTIME_DIR

`MOXI_COMMS_HOME` collapses all five under one directory and `MOXI_COMMS_DEVICE` names
the device (`moxi.paths.Layout`). comms writes only under these five; its sockets live
in RUNTIME/comms, which `runtime_root()` names. Only comms-specific subpaths live here.

Every function takes an optional `env` mapping and reads the process environment
without one.

`refuse_outside_runtime` is the guard on a socket path handed in from the environment:
a claim or daemon socket outside RUNTIME/comms is refused, naming the variable and the
directory, so a stamp inherited from another program's session cannot point comms at
that program's sockets.
"""

from __future__ import annotations

import os
from pathlib import Path

from moxi.paths import Layout

from .release import MODULE

#: comms' layout: the core's roots, comms' prefix `MOXI_COMMS_`.
LAYOUT = Layout(MODULE)


def data_dir(env=None) -> Path:
    """DATA/comms."""
    return LAYOUT.data_dir(env)


def state_dir(env=None) -> Path:
    """STATE/comms, device-keyed."""
    return LAYOUT.state_dir(env)


def cache_dir(env=None) -> Path:
    """CACHE/comms, device-keyed."""
    return LAYOUT.cache_dir(env)


def config_dir(env=None) -> Path:
    """CONFIG/comms, which holds `config.toml`."""
    return LAYOUT.config_dir(env)


def runtime_dir(env=None) -> Path:
    """RUNTIME/comms: every socket comms makes, and nothing that outlives a reboot."""
    return LAYOUT.runtime_dir(env)


def runtime_root(env=None) -> Path:
    """comms' runtime root, RUNTIME/comms (`runtime_dir`): the directory every comms
    socket is made in, and the one `refuse_outside_runtime` holds a socket path to."""
    return LAYOUT.runtime_dir(env)


class OutsideRuntime(ValueError):
    """A socket path from the environment that is not under RUNTIME/comms."""


def refuse_outside_runtime(path, variable: str, env=None) -> Path:
    """`path` when it lies under RUNTIME/comms, else raise `OutsideRuntime` naming
    `variable` and the directory.

    The check reads both paths with their links resolved, so a link inside RUNTIME/comms
    that points elsewhere is refused too.
    """
    root = runtime_dir(env)
    given = Path(path)
    real = Path(os.path.realpath(given))
    real_root = Path(os.path.realpath(root))
    if real != real_root and real_root in real.parents:
        return given
    raise OutsideRuntime(
        f"{variable} names {given}, which is outside comms' runtime directory {root}; "
        f"comms uses only sockets under {root}. Start the session from `moxi comms` "
        f"or unset {variable}.")


def directory_db(env=None) -> Path:
    return data_dir(env) / "directory.db"


def bindings_db(env=None) -> Path:
    """DATA/comms/bindings.db -- the binding custody journal, beside directory.db.

    DATA and deliberately not STATE, though a binding is a live-session fact: what the
    journal records is used to INTERPRET the record, and the record is device-agnostic.
    An attribution answer must not depend on which box is asking.
    """
    return data_dir(env) / "bindings.db"


def cards_dir(env=None) -> Path:
    """DATA/comms/cards -- holder-authored peer-facing agent cards, one per identity.

    DATA and deliberately not STATE: a card is durable user data and device-agnostic --
    who someone IS does not depend on which box is asking, the same argument
    `bindings_db()` makes above. It is not derived (nothing can regenerate a card; only
    its holder can write one), so it is not CACHE; it is content, not config; and it
    outlives a login, so it is not RUNTIME. comms creates nothing here: the reader is
    read-only and the first holder to write a card makes the directory.
    """
    return data_dir(env) / "cards"


def cursors_dir(env=None) -> Path:
    return state_dir(env) / "cursors"


def slot_keeps(env=None) -> Path:
    """STATE/comms/slot-keeps.json -- the daemon's keep mark.

    Which chosen and standing names held which slot, and which slots are still kept for a
    name that has not come back, so a restart gives each name back the slot it held. Only
    the daemon writes it. STATE and not DATA: it describes this box's daemon and nothing
    should sync it. It is rebuildable in the only sense that matters -- losing it costs
    the keeps and nothing else, and slot choice relaxes to what it was before keeps.
    """
    return state_dir(env) / "slot-keeps.json"


def cold_gate(env=None) -> Path:
    """STATE/comms/cold-gate.json -- the daemon's cold-cache gate file.

    `{"held": {name: [ids]}, "refused": {name: [ids]}}`: the ids the daemon's gate held
    for a claude-shim name's own next turn, and the ids it refused for an adapter that
    takes no delayed mail. The record has no `[next-turn]` token for a cold-held id, so
    this file is the only place a restart can rebuild one from. Its `refused` half is a
    second copy: the local record's `cold_refusals` table is the first. Only the daemon
    writes it, whole, with an atomic replace. STATE, not DATA: it describes this box's
    daemon. Losing it costs a re-read of the cold-held mail at the next replay or rescan,
    which the gate then checks again.
    """
    return state_dir(env) / "cold-gate.json"


def tokens_dir(env=None) -> Path:
    """STATE/comms/tokens -- the per-holder token store.

    STATE and deliberately not DATA: a stored credential is a DEVICE credential, and DATA
    is THE sync surface, so a token there would ride a future sync to boxes it was never
    minted for. CONFIG holds no tokens at all. `[secrets] store` in config.toml (or
    `MOXI_COMMS_TOKEN_STORE`) overrides this path -- `MOXI_COMMS_HOME` collapses every
    root, so an alternate-home recipe needs a way to still name the real store.
    """
    return state_dir(env) / "tokens"


def record_db(env=None) -> Path:
    """STATE/comms/record.db -- the local backend's record, beside the cursors and tokens.

    STATE and deliberately not DATA: DATA is the sync surface, and a live database is
    never continuous-file-synced. The record is the BOX's, one file per device, the same
    reasoning as for per-device credentials.
    `[local] record` in config.toml and `MOXI_COMMS_LOCAL_RECORD` override it.
    """
    return state_dir(env) / "record.db"


def daemon_socket(env=None) -> Path:
    """RUNTIME/comms/moxi-comms-daemon.sock -- the daemon's one listening socket."""
    return runtime_dir(env) / "moxi-comms-daemon.sock"


def codex_threads_dir(env=None) -> Path:
    """STATE/comms/codex -- thread-id <-> identity, beside the cursors: the thread of
    each identity's current launch."""
    return state_dir(env) / "codex"


def opencode_runtime_dir(env=None) -> Path:
    """RUNTIME/comms/opencode -- what an `opencode` launcher writes for its own server's launch:
    the layer-one instructions file the server reads by absolute path."""
    return runtime_dir(env) / "opencode"


def opencode_sessions_dir(env=None) -> Path:
    """STATE/comms/opencode -- session-id <-> identity for the `opencode` adapter, and
    the launcher's log and status lines.

    The same category as `codex_threads_dir` (the adapter writes it, the daemon never
    does). It holds the session of the identity's current launch: each launch creates
    a new session and never loads one an earlier launch recorded. The status lines
    carry across launches.
    """
    return state_dir(env) / "opencode"


def log_dir(env=None) -> Path:
    """STATE/comms/logs -- the daemon's and the launchers' logs."""
    return state_dir(env) / "logs"
