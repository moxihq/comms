"""`moxi comms claude` (and `moxi-comms-claude`) -- the launcher: one command starts a
Claude Code session with a comms identity.

It stamps the session's identity into the environment, makes the session's RUNTIME
directory, forks the session's adapter, and execs `claude`:

  * **The adapter** (`claude_adapter`) is a comms process, one per session. The launcher
    takes a handle on its own process (a pidfd: the pid `claude` keeps once this process
    execs it), forks, and the child detaches (its own session, its standard streams to
    comms' log) with the environment `claude` gets. Before `claude` starts it binds the
    name with the daemon and listens on the claim socket, `MOXI_COMMS_CLAIM_SOCKET`, with
    the `claude` process as the claim's root: the launcher waits for its ready byte, at
    most `READY_WAIT` seconds, so an in-session `moxi comms send` is answered from the
    session's first command. It then finds the session's turns endpoint and delivers
    through it, and ends when `claude` exits.
  * **No hooks, no settings file, no plugin.** turns is the one interface to the session:
    its plugin is the one a session loads, and comms' context (layer one, the roster and
    its deltas) reaches the session through comms' listener, which turns asks
    (`moxi_comms.listener`). This launcher writes no `--settings` file, passes no
    `--mcp-config`, and adds no plugin flag of its own. A session that is to load turns'
    plugin for one launch passes `--plugin-dir` itself, after `--`.

It also passes the session's NATIVE LABEL: `-n`, rendered here by `native_name()`
(see there for the spelling). Two flags, two namespaces -- `--agent` carries a ROLE file
and is whoever launches the session's to pass, `-n` carries the label and is comms' to
render, because only this process knows both halves of the identity. See
`Session.claude_argv`. Everything after `--` reaches `claude` exactly as given.

The identity is stamped into the session's environment at launch (`MOXI_COMMS_SLOT` /
`MOXI_COMMS_NAME` / `MOXI_COMMS_SCOPE` / `MOXI_COMMS_GLOBAL`, and `MOXI_COMMS_PROJECT`),
beside the adapter's claim socket (`MOXI_COMMS_CLAIM_SOCKET`). `moxi comms send` inside that
session reads who it is speaking as from the claim socket; the stamp alone sends nothing.

The launcher also stamps how its own comms is run, `MOXI_COMMS_COMMAND` (its `moxi`
script by absolute path, `context.own_comms`). comms' listener names it in the session's
layer one, so the agent runs this install's comms and not whatever `moxi` its shell's
PATH finds first.

`--director <name>` (repeatable) declares this session's directors. The names are
qualified HERE -- bare -> the global identity, `name@scope` explicit, `name@global`
renders bare -- and go two places, both of them already-qualified: `MOXI_COMMS_DIRECTORS` in
the session environment (beside the identity stamp) and the adapter's claim, which is
what makes the frame line say `directive from`. Authority is the recipient's grant, it is
invisible on the wire, and it dies with the session.
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import sys
from pathlib import Path

from .. import directors as directors_mod
from ..context import COMMAND_ENV, own_comms
from ..paths import log_dir, runtime_root
from ..scoping import GLOBAL, NAME_RE, project_key

#: The old launcher stamp, scrubbed from a child's environment so a nested session
#: does not inherit its parent's (no comms code reads it).
SOCKET_ENV = "MOXI_COMMS_SHIM_SOCKET"

#: How long the launcher waits for the adapter's ready byte before it execs `claude`
#: anyway (the adapter's bind and listen take well under a second; a daemon that is not
#: there does not hold it up, since the adapter then binds when the daemon returns).
READY_WAIT = 5.0
#: The claim socket's name in the session's RUNTIME directory.
CLAIM_SOCKET_NAME = "claim.sock"

#: The separator the native layer wears where the comms identity wears `@`.
NATIVE_AT = "-at-"


def native_name(name: str, scope: str) -> str:
    """The session's NATIVE label -- what `-n` carries, and nothing else.

    **Why `-at-`.** `-n` cannot carry the qualified comms identity verbatim,
    `name@scope`: the harness's own SendMessage reserves `@` in its address grammar -- a
    live probe to `charset-probe@nonexistent` was REFUSED ("to must be a bare teammate
    name -- there is only one team per session") -- so a project-scoped session labelled
    that way would be natively unaddressable. A project-scoped identity therefore renders
    `{name}-at-{scope}`; a GLOBAL identity renders bare.

    This changes the HARNESS-FACING LABEL and nothing else. The comms identity, the
    machine-line tags, the directory, attribution, `moxi comms agents`, every wire surface
    keeps its `@` spelling untouched -- `scoping.qualify()` is still the one render of
    the identity, and this function is not a second one.

    **One-way, by construction.** Nothing may
    ever infer a comms identity FROM a native label, and there is no reverse index. So
    there is deliberately NO parse-back helper here and none anywhere else: `-at-` is
    written and never read. Do not add one -- render from the two halves you already
    hold (`name`, `scope`), the way this function does, or do without.

    **Declared ambiguity, not solved.** A GLOBAL identity
    literally named `x-at-y` renders the same native label as project-scoped `x@y`. The
    harness auto-renames native collisions and nobody parses labels back, so the
    collision costs a rename and no identity confusion. It is declared, not defended
    against; no machinery here guards it.

    The renderer lives HERE, in the launcher, rather than beside `qualify()`, because
    the native label is a harness artifact with exactly one writer -- this process, at
    launch. Keeping it out of `scoping` keeps the identity module free of a spelling no
    comms surface should ever be tempted to consume.
    """
    return name if scope == GLOBAL else f"{name}{NATIVE_AT}{scope}"


def sessions_dir(env=None) -> Path:
    """RUNTIME/comms/comms-sessions: one directory per session this launcher starts,
    holding its claim socket and its roster memory."""
    return runtime_root(env) / "comms-sessions"


# Every variable comms sets on a session. A child's comms identity is composed ONLY of
# what its caller stamps, so all of these come off first -- see `stamped_env`.
#: The claim socket: a session never inherits its launcher's.
CLAIM_SOCKET_ENV = "MOXI_COMMS_CLAIM_SOCKET"

INHERITED_COMMS_ENV = ("MOXI_COMMS_NAME", "MOXI_COMMS_SLOT", "MOXI_COMMS_SCOPE", "MOXI_COMMS_GLOBAL",
                       "MOXI_COMMS_DIRECTORS", SOCKET_ENV, "MOXI_COMMS_SOCKET",
                       "MOXI_COMMS_PROJECT", CLAIM_SOCKET_ENV, COMMAND_ENV)


def stamped_env(base=None, **claim) -> dict:
    """An environment for a child, carrying THIS identity's stamp and no other's.

    **Scrub, then stamp.** `identity_env` is an overlay, and
    an overlay only sets the keys it has a value for: an identity with no declared
    directors stamps no `MOXI_COMMS_DIRECTORS`, a global one stamps no `MOXI_COMMS_PROJECT`,
    a scoped one stamps no `MOXI_COMMS_GLOBAL`. Overlay those gaps onto an inherited
    environment and they fill with whoever launched the process.

    Without the scrub, a child launched from inside a session comes out holding the
    PARENT session's `MOXI_COMMS_SHIM_SOCKET`, pointed at the control socket of the
    session that launched it. The socket is the visible symptom; the one that
    matters is `MOXI_COMMS_DIRECTORS`, because authority is the recipient's own grant and
    nobody may be handed one by proximity. An agent that declared no directors would
    have inherited its launcher's and rendered another agent's mail as `directive from`.

    Every stamp site goes through here -- `Session.env`, the codex roster and the
    opencode roster -- because the leak was in the pattern, not in any one of them.
    """
    env = dict(os.environ if base is None else base)
    for var in INHERITED_COMMS_ENV:
        env.pop(var, None)
    env.update(identity_env(**claim))
    return env


def identity_env(*, name=None, slot=None, scope=None, global_=False,
                 project=None, directors=()) -> dict:
    """The claim, stamped where the CLI reads it.

    `MOXI_COMMS_DIRECTORS` rides beside it: the qualified,
    comma-separated list this session was launched with. It is a declaration, not a
    credential -- nothing downstream authorizes on it; the adapter renders with it.
    """
    env = {}
    env.update(directors_mod.stamp(directors))
    if name:
        env["MOXI_COMMS_NAME"] = name
    if slot is not None:
        env["MOXI_COMMS_SLOT"] = str(slot)
    if global_:
        env["MOXI_COMMS_GLOBAL"] = "1"
        env["MOXI_COMMS_SCOPE"] = GLOBAL
    elif scope:
        env["MOXI_COMMS_SCOPE"] = scope
    if project:
        env["MOXI_COMMS_PROJECT"] = project
    return env


class Session:
    """Everything one launched session needs, written down in one place."""

    def __init__(self, session_id: str, *, name=None, slot=None, scope=None,
                 global_=False, project=None, directors=(), native=None, comms=None):
        self.id = session_id
        #: How the launching install's comms is run (`context.own_comms`), stamped as
        #: `MOXI_COMMS_COMMAND` for the listener to name in layer one; None stamps none.
        self.comms = comms
        #: The session id this launch names to `claude`, or None for an
        #: unnamed resume, which states none until the adapter's first look.
        self.stated_id: str | None = None
        self.name = name
        # The caller's LABEL for `-n` (`--native-name`).
        # None = the identity spelling, `native_name()`, as before.
        self.native = native
        self.slot = slot
        # Neither `--scope` nor `--global` is exactly `--global`: a session binds in a
        # project scope only when its launch names one. The project still feeds the
        # session's environment; it is not the scope.
        self.global_ = bool(global_ or not scope)
        self.project = project or project_key()
        self.scope = GLOBAL if self.global_ else scope
        # Qualified HERE and nowhere else, by the one rule: a bare director is the
        # global identity.
        self.directors = directors_mod.declare(directors)
        self.dir = sessions_dir() / session_id
        #: The adapter's claim socket, in the session's own directory.
        self.claim_socket = self.dir / CLAIM_SOCKET_NAME

    # ------------------------------------------------------------------ pieces
    def env(self, base=None) -> dict:
        """The session's environment: the identity stamp, the claim socket and the
        launcher's comms command, over a scrubbed `base`."""
        env = stamped_env(base, name=self.name, slot=self.slot, scope=self.scope,
                          global_=self.global_, project=self.project,
                          directors=self.directors)
        env[CLAIM_SOCKET_ENV] = str(self.claim_socket)   # scrubbed above; this session's
        if self.comms:
            env[COMMAND_ENV] = self.comms                # scrubbed above too
        return env

    def write(self) -> Path:
        """Make the session's RUNTIME directory (0700) and return it. It holds the
        adapter's claim socket and the listener's roster memory, and nothing else: no
        settings file. Files a launch by an earlier comms left under this session id
        (`mcp.json`, `settings.json`) are removed rather than left to mislead."""
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir.parent, 0o700)
        os.chmod(self.dir, 0o700)
        for stale in ("mcp.json", "settings.json"):
            (self.dir / stale).unlink(missing_ok=True)
        return self.dir

    def log_path(self) -> Path:
        """Where the session's adapter writes its log."""
        return log_dir() / f"claude-{self.id}.log"

    def claude_argv(self, extra=(), claude: str = "claude") -> list[str]:
        """The `claude` argv, ending in the native ADDRESS this session answers to.

        Two flags, two namespaces. `--agent` carries the ROLE file and is the caller's
        to pass; `-n` carries the native label, rendered here and never reimplemented
        elsewhere, because this is the only process that knows both halves of the
        identity.

        `native_name()` spells a project-scoped identity `name-at-scope`, because the
        harness reserves `@` in its own address grammar and a `name@scope` label is
        unaddressable. Global still renders bare. See `native_name`.

        Position is deliberate: the render goes AFTER `*extra`, the launcher
        passthrough, so a stale launcher that still passes `-n` is overridden by
        position. A repeated `-n` is LAST-WINS -- measured, not assumed
        (`claude --bg -n first -n second` -> session store `name`
        `second`, banner and `claude agents --json` agreeing; Claude Code
        2.1.235). This is override, not belt-and-braces: on a comms-holding session
        the label is not the passthrough's to choose, because the claim is the
        identity and the label mirrors it one way. `extra` is still never inspected
        or stripped.

        `-n` rides the CLAUDE process only; the adapter's claim never carries it, so the
        claim gate is untouched.

        Fenced on `self.name`: `main()` refuses a nameless launch (twice
        test-pinned), so the unnamed arm is CLI-unreachable -- but `__init__`
        accepts `name=None` and this method is public, and an unfenced render would
        hand `os.execve` a `-n "None-at-proj"` or a literal `None`.

        **`--native-name` overrides the render.** A caller that names the session
        (a launcher with its own session-name field) hands the name here as
        `--native-name`. When one is given it is what `-n` carries, verbatim; the identity
        spelling (`native_name()`) remains the render when no label is given, so a bare
        `moxi-comms-claude --name x` still comes up as `x`. The position (last, after the
        passthrough) and the fence on `self.name` are unchanged.
        """
        argv = [claude, *extra]
        # The label goes LAST, after the passthrough. See the docstring.
        if self.name:
            argv += ["-n", self.native_label()]
        return argv

    def native_label(self) -> str:
        """What `-n` carries: the launcher's label or the identity spelling."""
        return self.native or native_name(self.name, self.scope)


# ----------------------------------------------------------- the bundle must exist

def provisioned_slots(env=None) -> list[int]:
    """The comms slot numbers this box holds a token bundle for, ascending.

    `owners_present` is a key-level question of the declared store boundary: which holders
    have a file, never what any of them holds.
    """
    from ..tokens import TokenStore
    store = TokenStore(env=env if env is not None else os.environ)
    out = []
    for key in store.owners_present():
        head, _, _ = key.partition("/")
        if head.startswith("slot-") and head[5:].isdigit():
            out.append(int(head[5:]))
    return sorted(set(out))


def unprovisioned(slot: int, env=None) -> str | None:
    """Why `--comm-slot <slot>` cannot launch, or None when its bundle is present.

    Reads the token store the way the shim will at bind (`owner_for_slot`), so a
    refusal here and a silent unbound session there cannot disagree. A store that
    refuses to be read (loose permissions) is reported as that, not as absence.

    The question is whether the bundle EXISTS, so it is asked of the holder's keys and
    no credential is read to answer it. `secret_ids` is the store's key-level half of
    that boundary, and it answers for a holder whose file carries posting tokens and no
    machine token, which `read` by its meaning cannot.
    """
    from ..tokens import TokenStore, TokenStoreError, owner_for_slot
    store = TokenStore(env=env if env is not None else os.environ)
    try:
        present = bool(store.secret_ids(owner_for_slot(slot)))
    except TokenStoreError as e:
        return f"--comm-slot {slot}: the token store could not answer: {e}"
    if present:
        return None
    have = provisioned_slots(env)
    inventory = (f"provisioned comms slots on this box: {', '.join(map(str, have))}"
                 if have else "this box has no provisioned comms slot at all")
    return (f"--comm-slot {slot} names no provisioned webhook bundle ({inventory}). "
            f"A session launched on it would register against nothing and come up "
            f"comms-mute, so this is a refusal. Pick a provisioned number, or run "
            f"moxi-comms-provision to add bundles.")


# --------------------------------------------------------------------------- main

def split_passthrough(argv) -> tuple[list[str], list[str]]:
    """The launcher's own arguments, and everything after the first `--`, which goes to
    `claude` exactly as given (a later `--` included)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        at = argv.index("--")
        return argv[:at], argv[at + 1:]
    return argv, []


def _is_uuid(value) -> bool:
    import uuid
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except ValueError:
        return False


#: Claude Code flags that resume a session without naming its id.
RESUMES_UNNAMED = ("--continue", "-c", "--from-pr", "--teleport")


def _flag_value(passthrough, *names):
    """(present, value) of the first of `names` in `passthrough`: `--x V`, `--x=V`, or
    `--x` with no value (the next token is absent or another flag)."""
    for i, tok in enumerate(passthrough):
        for name in names:
            if tok == name:
                nxt = passthrough[i + 1] if i + 1 < len(passthrough) else None
                return True, (nxt if nxt is not None and not nxt.startswith("-") else None)
            if tok.startswith(name + "="):
                return True, tok[len(name) + 1:]
    return False, None


def _claim_socket_is_live(path) -> bool:
    """True when another launch of the same session id still serves its claim socket.
    A stale socket file or none at all is False."""
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def launch_session_id(passthrough, own=None):
    """(the id this launch names to `claude`, the argv to add for it).

    The adapter states that id from its bind, so a session whose turns endpoint is not up
    yet still gets a cache verdict.

    An id the passthrough names is the id: `--session-id U`, or `--resume U` / `-r U`
    with `U` a UUID and no `--fork-session`. A launch that resumes without naming one
    (`--continue`, `-c`, `--resume` with no UUID, `--from-pr`, `--teleport`) and does not
    fork passes through unchanged and names none: (None, []). Otherwise the launcher
    names one, its own `--session-id` or a fresh UUID, and adds `--session-id U`.
    Raises ValueError when the launcher's own `--session-id` or the passthrough's is not
    a UUID, or the two differ.

    The id comes back in lowercase, the spelling `claude` and turns report it in and
    the transcript's file name carries, whatever spelling the caller gave. A
    passthrough is never rewritten, so a `--resume U` keeps the caller's spelling in
    `claude`'s argv."""
    import uuid
    if "--" in passthrough:
        # What follows claude's own `--` is its positional arguments, not flags.
        passthrough = passthrough[:passthrough.index("--")]
    if own is not None and not _is_uuid(own):
        raise ValueError(f"--session-id {own!r} is not a UUID; it is the id `claude` "
                         "gets, so it must be one")
    fork = "--fork-session" in passthrough
    has_sid, sid = _flag_value(passthrough, "--session-id")
    has_resume, resumed = _flag_value(passthrough, "--resume", "-r")
    named = sid if has_sid else (resumed if has_resume and not fork
                                  and _is_uuid(resumed) else None)
    if named is not None:
        if not _is_uuid(named):
            raise ValueError(f"--session-id {named!r} after -- is not a UUID, which claude "
                             "refuses; the session's comms directory is named by it")
        if own is not None and own.lower() != named.lower():
            raise ValueError(f"--session-id {own} differs from the id the passthrough "
                             f"names to claude ({named})")
        return named.lower(), []
    unnamed = (any(t in RESUMES_UNNAMED or t.startswith(("--from-pr=", "--teleport="))
                   for t in passthrough) or has_resume)
    if unnamed and not fork:
        return None, []
    named = (own or str(uuid.uuid4())).lower()
    return named, ["--session-id", named]


def main(argv=None, prog: str = "moxi-comms-claude") -> int:
    own, passthrough = split_passthrough(argv)
    ap = argparse.ArgumentParser(
        prog=prog,
        description="start a Claude Code session with a comms identity",
        epilog="anything after -- is passed to claude unchanged")
    ap.add_argument("--name", help="claim this chosen (or standing) name")
    ap.add_argument("--comm-slot", dest="slot",
                    help="spend this comms slot's webhook bundle ('next-free' or a "
                         "provisioned number). Operator inventory, never an identity: it "
                         "requires --name.")
    ap.add_argument("--scope", help="a project scope, only to keep apart two agents of one "
                                    "name in different projects (default: global)")
    ap.add_argument("--global", dest="global_", action="store_true")
    ap.add_argument("--project", help="override the computed project key")
    ap.add_argument("--director", action="append", default=[],
                    help="declare a director for THIS session (repeatable): messages "
                         "from that identity, verified, render as directives. Qualified "
                         "here like any other name (bare -> the global identity, "
                         "name@scope explicit). One hop, no transitivity.")
    ap.add_argument("--native-name", dest="native",
                    help="the label Claude Code shows for this session (-n), verbatim: "
                         "for a caller that names its sessions. Absent, -n is the "
                         "identity spelling "
                         "(bare for global, name-at-scope for a project scope).")
    ap.add_argument("--session-id", help="the session id (a UUID) this launch names to "
                                         "claude, which also names its runtime dir")
    ap.add_argument("--claude", default="claude", help="the claude binary")
    ap.add_argument("--dry-run", action="store_true",
                    help="make the session directory and print the launch line; fork and exec nothing")
    args = ap.parse_args(own)
    from ..config import ConfigError, load
    try:
        load()                  # a bad config file fails here, before claude starts
    except ConfigError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 2

    slot = args.slot
    if slot is not None and slot != "next-free":
        try:
            slot = int(slot)
        except ValueError:
            print(f"{prog}: --comm-slot {args.slot!r} is not a number or 'next-free'",
                  file=sys.stderr)
            return 2
    if not args.name:
        # A claim is a NAME. An auto-named claim resolves to `agent-N` only when the shim
        # registers, which happens after this process has already handed the environment
        # to claude, so there is no honest MOXI_COMMS_NAME to stamp -- and a slot is no
        # substitute: a slot alone would stamp a nameless claim whose verb then wore
        # `slot-3` on the wire, a fabricated identity.
        # Refuse rather than stamp a seat where a name belongs.
        print(f"{prog}: pass --name <name>.\n"
              "  A claim is a name: it is what the record shows, what a recipient's frame\n"
              "  line says, and what `moxi comms send` speaks as. `agent-N` is minted by the\n"
              "  daemon at register time — after this launcher has handed the session its\n"
              "  environment — so there is no identity to stamp into it, and --comm-slot\n"
              "  names a webhook bundle, not an agent. Add --name; --comm-slot <n> may ride\n"
              "  with it.",
              file=sys.stderr)
        return 2
    if not NAME_RE.match(args.name):
        # The grammar was never checked on this path, so `--name 'bad name@x!'` was
        # accepted in full and rendered into the address as `-n 'bad name@x!@proj-a'`.
        # It then failed at REGISTER time, inside the session, where the shim's refusal
        # goes to a debug log nobody reads.
        # NAME_RE is comms' own; this is the launcher consulting it, not a second copy.
        print(f"{prog}: --name {args.name!r} is not a name. A comms identity is "
              f"{NAME_RE.pattern} -- letters, digits, dot, underscore, hyphen. The scope "
              "half goes in --scope or --global, never in the name.", file=sys.stderr)
        return 2

    if isinstance(slot, int):
        # A pinned bundle number must EXIST. A launch that pins a slot the box does not
        # have registers against nothing: the shim exits and the session comes up
        # unbound and silent. Presence is the token store's answer -- the same
        # file the shim would read at bind -- so the refusal and the failure it prevents
        # have one source of truth.
        reason = unprovisioned(slot, env=os.environ)
        if reason:
            print(f"{prog}: {reason}", file=sys.stderr)
            return 2

    import uuid
    try:
        stated, named_argv = launch_session_id(passthrough, args.session_id)
    except ValueError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 2
    # An unnamed resume states no id until the look; its runtime dir is still named, by
    # the launcher's own --session-id when it has one, in lowercase as a stated id is.
    session_id = stated or (args.session_id or "").lower() or uuid.uuid4().hex[:8]
    try:
        session = Session(session_id, name=args.name,
                          slot=slot, scope=args.scope, global_=args.global_,
                          project=args.project,
                          directors=args.director, native=args.native,
                          comms=own_comms())
    except directors_mod.BadDirector as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 2
    session.stated_id = stated
    if _claim_socket_is_live(session.claim_socket):
        # `claude --resume U` can be open in two terminals. A second launch would take the
        # first's claim socket and share its session dir, which either adapter's end
        # removes under the other.
        print(f"{prog}: session {session.id} is already running under comms; "
              "its claim socket answers", file=sys.stderr)
        return 2
    session.write()
    env = session.env()
    # The named id goes first, ahead of any `--` in the passthrough.
    cmd = session.claude_argv([*named_argv, *passthrough], claude=args.claude)

    print(f"session {session.id}")
    print(f"  identity   {session.name} @ {session.scope}")
    # The native label is worth saying out loud: it is the caller's label when one was
    # given, else the identity spelling.
    print(f"  native -n  {session.native_label()}")
    if session.directors:
        print(f"  directors  {', '.join(session.directors)} "
              "(verified messages from these render as directives)")
    print(f"  claim      {session.claim_socket}")
    print(f"  adapter    log {session.log_path()}")
    print("  launch     " + " ".join(cmd))
    if args.dry_run:
        return 0
    binary = shutil.which(args.claude)
    if binary is None:
        print(f"{prog}: no {args.claude!r} on PATH", file=sys.stderr)
        return 2
    sys.stdout.flush()
    sys.stderr.flush()
    launch(session, binary, cmd, env, prog=prog)   # pragma: no cover - replaces this process
    return 0


# ----------------------------------------------------------------- the adapter fork

def launch(session: Session, binary: str, cmd: list[str], env: dict, *,
           prog: str = "moxi-comms-claude", ready_wait: float = READY_WAIT,
           exec_=os.execve):
    """Fork the session's adapter, wait for it to be ready, and exec `claude`.

    This process takes a pidfd on itself first: it becomes `claude` at the exec, keeping
    its pid, so the handle is the adapter's watch on `claude`, and a reused pid cannot
    stand in for it. The adapter is forked twice over (its first parent leaves at once),
    so it is never `claude`'s child and is not left a zombie under it, and it runs in a
    session of its own. The launcher waits for its ready byte, at most `ready_wait`
    seconds, then execs whatever it heard; past the bound it says so on stderr."""
    harness_pid = os.getpid()
    handle = os.pidfd_open(harness_pid)
    r, w = os.pipe()
    child = os.fork()
    if child == 0:                                         # pragma: no cover - forked
        code = 70
        try:
            os.close(r)
            os.setsid()
            if os.fork() == 0:
                code = 70
                try:
                    code = _adapter_process(session, env, harness_pid, handle, w)
                finally:
                    os._exit(code)
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    os.waitpid(child, 0)
    os.close(handle)
    ready = _await_ready(r, ready_wait)
    os.close(r)
    if not ready:
        print(f"{prog}: the session's comms adapter was not ready after {ready_wait:g}s; "
              f"starting claude anyway (its log: {session.log_path()})", file=sys.stderr)
    return exec_(binary, cmd, env)


def _await_ready(fd: int, timeout: float) -> bool:
    """True when the adapter wrote its ready byte within `timeout` seconds."""
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return False
    try:
        return os.read(fd, 1) == b"1"
    except OSError:
        return False


def _adapter_process(session: Session, env: dict, harness_pid: int, handle: int,
                     ready_fd: int) -> int:                # pragma: no cover - forked
    """The forked adapter: the session's environment, standard streams to its log, then
    `claude_adapter.serve` until `claude` has ended."""
    import logging

    os.environ.clear()
    os.environ.update(env)
    log_path = session.log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.close(null)
    os.close(out)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = os.fdopen(2, "w", buffering=1)
    logging.basicConfig(level="INFO", stream=sys.stderr, force=True,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    def ready() -> None:
        try:
            os.write(ready_fd, b"1")
            os.close(ready_fd)
        except OSError:
            pass

    from . import claude_adapter
    shim = adapter_shim(session, harness_pid)
    try:
        return claude_adapter.serve(shim, harness_pid, handle, ready=ready)
    finally:
        # The session has ended: its directory (the claim socket's, the roster memory's)
        # goes with it.
        shutil.rmtree(session.dir, ignore_errors=True)


def adapter_shim(session: Session, harness_pid: int, **extra):
    """The session's `ClaudeShim`: its claim (name, slot, scope, directors) keyed by the
    session id, its claim socket in the session's directory with the `claude` process as
    the claim's root, and its supervisor on. The session id is the one the launch named
    to `claude`, stated from the bind until the first look replaces it; an
    unnamed resume states none. Never an inherited `CLAUDE_CODE_SESSION_ID` (a launcher
    run inside another session carries that session's)."""
    from ..membership import TREE, root_of
    from .claude_shim import ClaudeShim

    root = root_of(harness_pid, TREE)
    shim = ClaudeShim(slot=session.slot if session.slot is not None else "next-free",
                      name=session.name, scope=None if session.global_ else session.scope,
                      global_=session.global_, project=session.project,
                      directors=session.directors, autoreconnect=True,
                      session_id="", claim_key=session.id,
                      claim_socket=session.claim_socket,
                      claim_roots=[root] if root else [], listen_claims=True, **extra)
    shim.session_id = session.stated_id
    return shim


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
