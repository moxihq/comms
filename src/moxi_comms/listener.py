"""comms' listener: the context a session `moxi comms claude` launched gets from comms.

turns asks it in the session's endpoint process (`moxi_comms.listeners` registers it),
with the session's facts and the environment the endpoint inherited from the session,
which carries the launcher's identity stamps. What it answers is what the roster hooks
printed when comms wrote its own hooks (`adapters/shim_roster`):

    ready    layer one: the context block, the line naming which comms to run (the
             command the launcher stamped, `MOXI_COMMS_COMMAND`), then the roster
             snapshot (or the offline line)
    prompt   the roster's joins and leaves since this session last looked, and each
             ignored reminder naming this session as its author, once; or nothing

A session comms did not launch has no identity stamp, and comms binds no such session, so
the listener answers None there. It answers only for Claude Code sessions: a Codex thread
gets layer one as developer instructions at its launch.

**The roster memory.** The deltas diff against what this session last saw, kept in
`roster.json` in the session's runtime directory, the directory that holds the claim
socket the launcher stamped (`MOXI_COMMS_CLAIM_SOCKET`). `ready` seeds it on a roster it
could read; a session with no such directory gets no deltas, since deltas without memory
would announce the world every prompt.

**Time.** turns bounds every listener of an event together by `listen.wait_ms` (2000 ms
by default) and gives the event no context from a listener that is late. The daemon query
here is bounded at `QUERY_TIMEOUT`, below that default. turns' library gives a caller no
way to read the setting, so the bound is fixed, and `docs/configuration.md` asks for a
`listen.wait_ms` above it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The harnesses this listener answers for.
HARNESSES = ("claude",)
#: The daemon query's bound, inside turns' default `listen.wait_ms` of 2 s. A box that
#: sets `listen.wait_ms` at or below it loses a late answer (docs/configuration.md).
QUERY_TIMEOUT = 1.5
#: The session's roster memory, beside its claim socket.
MEMORY = "roster.json"


def memory_path(env) -> Path | None:
    """The session's roster memory file, or None when the session has no runtime
    directory of comms' own (a claim socket in a session directory the launcher made)."""
    from .adapters.session import sessions_dir
    from .claimhold import CLAIM_SOCKET_ENV
    from .paths import OutsideRuntime, refuse_outside_runtime

    value = env.get(CLAIM_SOCKET_ENV)
    if not value:
        return None
    try:
        sock = refuse_outside_runtime(value, CLAIM_SOCKET_ENV, env)
    except OutsideRuntime:
        return None
    directory = Path(sock).parent
    if directory.parent != sessions_dir(env):
        return None
    return directory / MEMORY


def answer(event) -> str | None:
    """turns' `SessionEvent` in, the added context out (None for none)."""
    from .adapters import shim_roster
    from .paths import daemon_socket

    if event.harness not in HARNESSES or event.event not in ("ready", "prompt"):
        return None
    env = dict(event.env) if event.env else dict(os.environ)
    if not env.get("MOXI_COMMS_NAME"):
        return None
    socket_path = daemon_socket(env)
    state = memory_path(env)
    if event.event == "ready":
        info = shim_roster.fetch(socket_path, timeout=QUERY_TIMEOUT)
        text, online = shim_roster.render_snapshot(info, env=env)
        if online and state is not None:
            shim_roster.write_state(state, shim_roster.roster_of(info))
        return text
    if state is None:
        return None
    text, _online = shim_roster.delta(socket_path, state, env=env, timeout=QUERY_TIMEOUT)
    return text


__all__ = ["HARNESSES", "MEMORY", "QUERY_TIMEOUT", "answer", "memory_path"]
