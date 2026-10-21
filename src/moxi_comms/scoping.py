"""Identity = (name, scope).

A scope is `global` unless the launch names a project scope, which it does only to keep
apart two agents of one name in different projects. A bare name is the global identity,
typed or read back from the record. The project key (git-root basename of the
cwd, overridable by `MOXI_COMMS_PROJECT` / `--project`) is the session's project, not
its scope.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GLOBAL = "global"
AUTO_NAME_RE = re.compile(r"^agent-\d+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def project_key(cwd: str | Path | None = None, env=None) -> str:
    """git-root basename of cwd; MOXI_COMMS_PROJECT wins; `global` when there is no repo."""
    env = env if env is not None else os.environ
    if override := env.get("MOXI_COMMS_PROJECT"):
        return override
    start = Path(cwd) if cwd is not None else Path.cwd()
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return GLOBAL


def qualify(name: str, scope: str) -> str:
    """Qualified form. Global identities render bare (`agent-7`, `steward`)."""
    return name if scope == GLOBAL else f"{name}@{scope}"


def split_tag(tag: str) -> tuple[str, str | None]:
    """`main@proj-b` -> ('main', 'proj-b'); `main` -> ('main', None), the global main."""
    name, sep, scope = tag.partition("@")
    return (name, scope if sep else None)


def is_auto_name(name: str) -> bool:
    return AUTO_NAME_RE.match(name) is not None


def identity_of(tag: str) -> str:
    """The one identity a tag names, as `qualify` spells it. There is no point of view:
    the tag alone decides. `x@S` (S not global) is x@S; `x` and `x@global` are the global x, spelled `x`;
    `agent-N`, however spelled, is the ephemeral `agent-N`. Nothing else is consulted:
    not the sender's scope, the author's, the reader's, a claim's or the working
    directory's."""
    name, scope = split_tag(tag)
    if is_auto_name(name):
        return name
    return qualify(name, scope or GLOBAL)


def tag_matches(tag: str, identity: str) -> bool:
    """Does `tag` name `identity` (qualified)? True only when the identity the tag names
    by the one rule (`identity_of`) is `identity`: a bare tag never matches `x@S`, and
    `x@S` never matches the global x. The daemon matches its sent index with it, and the
    verbs match the record with it, with no daemon call."""
    return identity_of(tag) == identity
