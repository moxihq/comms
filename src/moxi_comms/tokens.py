"""The token store: where comms' credentials REST, one 0600 file per holder.

THE DECLARED PAIR. The store's reader and writer are two functions, declared, and every
caller goes through them:

    TokenStore.read_secret(owner, secret_id)          the one declared READ
    TokenStore.write_secret(owner, secret_id, value)  the one declared WRITE

Key-level questions (`secret_ids`, `owners_present`, `has_any`) are declared here too and
answer WHICH keys a holder has, never what they hold: asking whether a bundle exists is
not a reason to read a credential.

What may rest here, and what may not:

  * **may:** posting tokens and role credentials, each under its own key, 0600 in a
    0700 directory;
  * **may not:** the provisioner credential (fenced off, refused in code below).

THE FILE IS A MAP. One holder's file is a flat TOML table of `key = "value"`, written by
the emitter below (stdlib only; `tomllib` reads, there is no writer in it):

    "comms/slot-3/names" = "…"      a posting token, under its own `secret_id`
    "ANTHROPIC_API_KEY" = "…"       a role credential, under its KEY

A file that does not parse as such a table holds no comms key: a read of it misses,
and a write refuses it. It is never guessed at and never overwritten.

The store is device-keyed STATE (`paths.tokens_dir()`), because a stored credential is a
DEVICE credential and DATA is the sync surface. config.toml's `[secrets] store` (or its
override `MOXI_COMMS_TOKEN_STORE`) names another location, so an alternate-home recipe
(one that sets `MOXI_COMMS_HOME`, which collapses every root) can still point at the real
store instead of silently finding nothing.

One file per HOLDER:

    slot-3            a slot's bundle       (keys `comms/slot-3/<room>`)
    proj-a/main       a standing identity   (keys `comms/proj-a/<name>/<room>`)
    daemon            the daemon process    (holds no credential)
    operator          the operator's lane   (the local backend's `post_as_user` token)

and each role holder the box names in config.toml's `[secrets] roles`, under its own
name. A role the config does not list is unknown, with no store path, like any role the
store has never heard of.

Slot vocabulary in a PATH is operator inventory and stays legal (only agent-facing
surfaces carry no slots); the refusals raised from here therefore name the path in
the LOG and never in the sentence an agent reads.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("comms.tokens")

#: The variable that overrides config.toml's `[secrets] store` for one process
#: (settings.toml declares it; moxi.config reads it).
STORE_ENV_VAR = "MOXI_COMMS_TOKEN_STORE"
DIR_MODE = 0o700
FILE_MODE = 0o600
LOOSE_MASK = 0o077                      # any group/other bit at all is too wide

ROLE_READER = "reader"
ROLE_DAEMON = "daemon"
ROLE_PROVISIONER = "provisioner"
#: The OPERATOR -- the role holder whose credential is a posting token minted on the
#: box. One key, `OPERATOR_SECRET_ID` below, written by `moxi-comms-provision` on the
#: local backend in the run that stores its hash in the record, and read by the send
#: verb's operator lane.
ROLE_OPERATOR = "operator"
#: The operator holder's one key, spelled like every other posting token's `secret_id`
#: (the `comms/<holder>/<row>` family) even though what it guards is a `meta` hash and
#: not a webhook row: one shape, and one place the sender and provision both look.
OPERATOR_SECRET_ID = "comms/operator/token"

KEY_PREFIX = "comms"                    # every bundle's secret ids start here
COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

PROVISIONER_FENCE = (
    "the provisioner credential never rests in the token store: it is the crown-jewel "
    "credential the store fences off, so provisioning stays an operator-shell act. "
    "Nothing was read and nothing was written.")

NOT_A_MAP = (
    "a stored credential file is not one the token store wrote: it is not a flat table "
    "of strings, so comms holds no key in it and does not overwrite it. Nothing was "
    "written. The "
    "store is the operator's; ask them. (The path is named in this process's comms "
    "log.)")

# Agent-facing, so it names no path and no seat: the store is operator inventory and the
# cure is the operator's (same shape as the CLI's malformed-stamp refusal).
LOOSE_PERMISSIONS = (
    "a stored posting credential is not readable safely: its file permissions are wider "
    "than 0600 and comms refuses to read a credential the rest of the box can see. "
    "Nothing was attempted. The store is the operator's; ask them. (The path is named in "
    "this process's comms log.)")


class TokenStoreError(Exception):
    """A refusal from the store. Never carries token material."""


@dataclass(frozen=True)
class TokenOwner:
    """WHO spends a credential: a bundle holder, or a process role. The store's key.

    `parts` are already-validated path components; `role` is set only for the role-keyed
    owners (`reader`/`daemon`, and `provisioner` — which exists solely to be refused).
    """

    parts: tuple[str, ...]
    role: str = ""

    @property
    def key(self) -> str:
        return "/".join(self.parts)

    @property
    def refused(self) -> bool:
        return self.role == ROLE_PROVISIONER

    def __str__(self) -> str:
        return self.key or f"role:{self.role}"


def _component(value, *, what: str) -> str:
    """One path component, validated. `scope` and `name` are ENV-derived: a crafted
    value must not be able to walk out of the store directory."""
    text = str(value)
    if text in (".", "..") or not COMPONENT_RE.match(text):
        raise TokenStoreError(
            f"{what} {text!r} is not a token-store name: the store's components are "
            "letters, digits, dot, dash and underscore, and nothing that could leave the "
            "store directory.")
    return text


def owner_for_slot(slot_id) -> TokenOwner:
    try:
        number = int(slot_id)
    except (TypeError, ValueError):
        raise TokenStoreError(f"slot {slot_id!r} is not a slot number") from None
    return TokenOwner((f"slot-{_component(number, what='slot')}",))


def owner_for_standing(name: str, scope: str) -> TokenOwner:
    return TokenOwner((_component(scope, what="scope"), _component(name, what="name")))


def box_roles(env=None) -> tuple[str, ...]:
    """The role holders this box adds, from config.toml's `[secrets] roles`."""
    from . import config

    return tuple(config.load(env=env).secrets.roles)


def owner_for_role(role: str, *, env=None) -> TokenOwner:
    """A role holder's key comes from its ROLE, because it has no claim env.

    `reader` and `daemon` are one holder, the daemon process, which holds no
    credential: comms provisions nothing for it. `operator` is the holder for the local
    backend's operator lane -- a holder like any other, which is the whole of what makes
    that lane credentialled. It keys under its own name, and so does each role the box
    names in `[secrets] roles`.
    `provisioner` returns an owner that can only ever be refused; every other role is
    unknown and gets no store access.
    """
    if role == ROLE_PROVISIONER:
        return TokenOwner((), role=ROLE_PROVISIONER)
    if role in (ROLE_READER, ROLE_DAEMON):
        return TokenOwner((ROLE_DAEMON,), role=role)
    if role == ROLE_OPERATOR:
        return TokenOwner((role,), role=role)
    if role in box_roles(env):
        return TokenOwner((role,), role=role)
    return TokenOwner((), role=str(role))


def owner_for_sender(sender) -> TokenOwner | None:
    """The owner of a sender's OWN bundle. `credential_for` prefers the row it actually
    resolved (a chosen-tier sender carries both a name and a slot), so this is the
    fallback shape, not the authority."""
    slot_id = getattr(sender, "slot_id", None)
    if slot_id is not None:
        return owner_for_slot(slot_id)
    name = getattr(sender, "name", None)
    if name:
        return owner_for_standing(name, getattr(sender, "scope", ""))
    return None


# --------------------------------------------------------------------------- keys

def holder_of_key(key: str) -> str | None:
    """`comms/slot-1/names` -> `comms/slot-1`; `comms/proj-a/main/names` ->
    `comms/proj-a/main`.

    The key already names the bundle — the room is only its last segment — so the
    holder is the key minus that segment.
    """
    parts = [p for p in str(key).split("/") if p]
    if len(parts) < 3 or parts[0] != KEY_PREFIX:
        return None
    return "/".join(parts[:-1])


def owner_of_holder(holder: str, *, env=None) -> TokenOwner | None:
    """`comms/slot-1` -> the slot-1 owner; `comms/daemon` -> the daemon owner;
    `comms/proj-a/main` -> the standing owner; `comms/<role>` -> that role's owner, for
    a role the box names in `[secrets] roles`.
    Anything else: not a holder we know."""
    parts = [p for p in str(holder).split("/") if p]
    if not parts or parts[0] != KEY_PREFIX:
        return None
    rest = parts[1:]
    if rest == [ROLE_DAEMON]:
        return owner_for_role(ROLE_DAEMON)
    if len(rest) == 1 and rest[0].startswith("slot-"):
        try:
            return owner_for_slot(int(rest[0].removeprefix("slot-")))
        except (TypeError, ValueError):
            return None
    if len(rest) == 1 and rest[0] in box_roles(env):
        return owner_for_role(rest[0], env=env)
    if len(rest) == 2:
        scope, name = rest
        return owner_for_standing(name, scope)
    return None


def project_name(owner: TokenOwner) -> str:
    """The holder's name in key form: the same string `holder_of_key` reads back off
    any of its secret keys."""
    return f"{KEY_PREFIX}/{owner.key}"


# ------------------------------------------------------------------- the file's shape

def _secret_id(value, *, what: str = "secret id") -> str:
    """One map KEY, validated. A key rides into a TOML file, so a newline or a control
    character in it would write a second line and mean something else on the way back."""
    text = str(value).strip()
    if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise TokenStoreError(
            f"{what} {value!r} is not a token-store key: a key is one printable line.")
    return text


def _quote(text: str) -> str:
    """A TOML basic string. The stdlib reads TOML and does not write it, so the emitter
    is here, minimal and escaped, and `_entries` is the round trip that proves it."""
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in "\b\f\n\r\t" or ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append({"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r",
                        "\t": "\\t"}.get(ch, f"\\u{ord(ch):04X}"))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _emit(entries: dict) -> str:
    """A flat map as TOML, keys quoted and sorted so a rewrite is a stable diff."""
    return "".join(f"{_quote(k)} = {_quote(v)}\n" for k, v in sorted(entries.items()))


def _entries(text: str) -> dict | None:
    """The file's contents as a map, or None when the file is not one.

    Empty is nothing; a flat TOML table of strings is the map; anything else is not a
    file this store wrote, and is never guessed at.
    """
    body = text.strip()
    if not body:
        return {}
    try:
        data = tomllib.loads(body)
    except tomllib.TOMLDecodeError:
        return None
    if all(isinstance(v, str) for v in data.values()):
        return data
    return None


# --------------------------------------------------------------------------- the store

class TokenStore:
    """Read/write the per-holder token files. Injectable root, so a test's store is its
    own.

    The door is `read_secret` and `write_secret`, the declared pair of the module
    docstring; `secret_ids` / `owners_present` / `has_any` answer key-level questions
    without opening a value.

    `root=None` resolves lazily: the injected env's `MOXI_COMMS_TOKEN_STORE` first (an
    explicit operator override that survives a `MOXI_COMMS_HOME` collapse), then this
    process's config.toml `[secrets] store` (with the same override), then STATE/comms/
    tokens under this process's roots. The injected env names no roots: a caller's
    session stamps are not where the box keeps its store.
    """

    def __init__(self, root=None, *, env=None):
        self._root = Path(root) if root is not None else None
        self.env = env if env is not None else {}

    # ------------------------------------------------------------------ location
    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        override = self.env.get(STORE_ENV_VAR)
        if override:
            return Path(override)
        from . import config
        from .paths import tokens_dir

        store = config.load().secrets.store
        return Path(store) if store else tokens_dir()

    def path_for(self, owner: TokenOwner) -> Path:
        if owner is None or not owner.parts:
            raise TokenStoreError(PROVISIONER_FENCE if getattr(owner, "refused", False)
                                  else "no owner: the token store is keyed by holder, and "
                                       "an unkeyed lookup has nothing to find.")
        if owner.refused:
            raise TokenStoreError(PROVISIONER_FENCE)
        return self.root.joinpath(*owner.parts)

    # ------------------------------------------------------------------ reading
    def read_secret(self, owner: TokenOwner, secret_id: str) -> str | None:
        """THE DECLARED READ. One key out of one
        holder's file, or None when that holder has no such key.

        Every read of a stored value goes through here: the resolver chain's `store`
        step and the local backend's `resolve_secret`. A file whose permissions are wider than
        0600 is REFUSED, not read and not warned about: 0600 is the whole mechanism, and
        a store that reads a world-readable credential anyway has only a comment where
        its rule was.
        """
        entries = self._entries_for(owner)
        value = (entries or {}).get(_secret_id(secret_id), "").strip()
        return value or None

    def secret_ids(self, owner: TokenOwner) -> list[str]:
        """WHICH keys this holder has, never what they hold. The key-level question, so
        that asking whether a bundle is provisioned (`adapters/session.py`) is not a
        reason to read a credential."""
        return sorted(self._entries_for(owner) or {})

    def _entries_for(self, owner: TokenOwner, *, writing: bool = False) -> dict | None:
        """One holder's file as a map, or None when there is no file. The mode check
        lives here, so every reader and the read-modify-write of `write_secret` share
        it.

        A file that is not a flat table of strings (a bare token an older install
        wrote, or another program's file) holds no comms key: a read of it is a miss,
        as it always was. A write refuses it (`NOT_A_MAP`), so comms never overwrites a
        file it did not write."""
        path = self.path_for(owner)
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as e:
            raise TokenStoreError(f"the token store is unreadable ({e.strerror}). "
                                  "Nothing was attempted.") from None
        self._check_mode(path, st.st_mode, "file")
        self._check_mode(self.root, self.root.stat().st_mode, "directory")
        entries = _entries(path.read_text())
        if entries is None:
            if writing:
                log.error("token store file %s is not a flat table of strings; comms "
                          "writes only the map it wrote, and leaves this file alone", path)
                raise TokenStoreError(NOT_A_MAP)
            log.warning("token store file %s is not a flat table of strings; it holds no "
                        "comms key", path)
            return {}
        return entries

    def _check_mode(self, path: Path, mode: int, what: str) -> None:
        if stat.S_IMODE(mode) & LOOSE_MASK:
            log.error("token store %s %s is mode %04o; 0%o is the rule and comms refuses "
                      "to read a credential wider than that",
                      what, path, stat.S_IMODE(mode),
                      DIR_MODE if what == "directory" else FILE_MODE)
            raise TokenStoreError(LOOSE_PERMISSIONS)

    def has_any(self) -> bool:
        """Could ANY holder resolve from this store?"""
        root = self.root
        if not root.is_dir():
            return False
        return any(p.is_file() for p in root.rglob("*"))

    # ------------------------------------------------------------------ writing
    def write_secret(self, owner: TokenOwner, secret_id: str, value: str) -> Path:
        """THE DECLARED WRITE. One key into one
        holder's file, 0600 in a 0700 tree, leaving that holder's other keys alone.

        Every write of a stored value goes through here: provision's local posting
        tokens and the role credentials an operator stores. Operator machinery only.
        """
        key = _secret_id(secret_id)
        if not value or not value.strip():
            raise TokenStoreError("an empty token is not a credential; nothing was written.")
        path = self.path_for(owner)                  # refuses the provisioner FIRST
        entries = dict(self._entries_for(owner, writing=True) or {})
        entries[key] = value.strip()
        self._mkdirs(path.parent)
        # Create the file private FIRST, then fill it: a token must never exist, even for
        # one syscall, at a mode the rest of the box could read.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.write(fd, _emit(entries).encode())
        finally:
            os.close(fd)
        os.chmod(path, FILE_MODE)
        log.info("token store: wrote %s [%s]", path, key)   # the PATH and the KEY; never
        return path                                         # the value

    def _mkdirs(self, directory: Path) -> None:
        root = self.root
        root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(root, DIR_MODE)
        if directory != root:
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            os.chmod(directory, DIR_MODE)

    def owners_present(self) -> list[str]:
        """Holder keys with a file, for the provision verb's summary. Never values."""
        root = self.root
        if not root.is_dir():
            return []
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


__all__ = ["TokenOwner", "TokenStore", "TokenStoreError", "STORE_ENV_VAR", "DIR_MODE",
           "FILE_MODE", "PROVISIONER_FENCE", "LOOSE_PERMISSIONS", "NOT_A_MAP",
           "ROLE_READER", "ROLE_DAEMON", "ROLE_PROVISIONER", "ROLE_OPERATOR",
           "OPERATOR_SECRET_ID", "COMPONENT_RE", "box_roles",
           "owner_for_slot",
           "owner_for_standing", "owner_for_role", "owner_for_sender", "holder_of_key",
           "owner_of_holder", "project_name"]
