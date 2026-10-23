"""The launch roster snapshot: `moxi-comms-shim-roster` and its SessionStart wiring.

The injection is the
`moxi comms agents` verb's output VERBATIM inside a labeled envelope; comms-offline at
launch degrades to one honest line; launch is never blocked (the hook always exits 0).
The own-binding race is stated, never dressed up as an outage.
"""

import ast
import json
from pathlib import Path

import pytest
from conftest import queued, wait_until

import moxi_turns
from moxi_comms import listener, roster_view, scoping
from moxi_comms.adapters import session, shim_roster
from moxi_comms.paths import daemon_socket


def event(name, *, env=None, harness="claude", session_id="s-1"):
    """turns' `SessionEvent`, as the endpoint hands it to comms' listener."""
    return moxi_turns.SessionEvent(event=name, harness=harness, session_id=session_id,
                                   env=dict(env or {}))


# ------------------------------------------------------------------- the envelope

def waiting_for(q) -> bool:
    """Whether the daemon's `agents` answer counts mail waiting for `q`: under
    `waiting`, or under `pending` from an older daemon."""
    from moxi_comms.client import DaemonClient
    c = DaemonClient(daemon_socket()).connect()
    try:
        info = c.query("agents")
    finally:
        c.close()
    return q in (info.get("waiting") or {}) or bool((info.get("pending") or {}).get(q))


def test_the_snapshot_body_is_the_verb_output_verbatim(rig):
    """One rendering exists: snapshot body == `moxi comms agents` for the same daemon
    state -- 'verbatim' held by construction. Mail waiting for a name is in neither
    (the default view is the names an agent can message)."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    rig.shim(slot=2)
    rig.cli(["send", "--posture", "none", "curator@proj-a", "waiting"], slot=1,
            name="planner", scope="proj-a")
    wait_until(lambda: waiting_for("curator@proj-a"))

    code, out = rig.cli(["agents"], slot=1)
    assert code == 0
    envelope, online = shim_roster.snapshot(daemon_socket(), env={})
    assert online
    # The block leads, then the line naming which comms to run; the snapshot follows.
    body = envelope.split("\n\n", 2)[2]
    assert body.splitlines()[0] == shim_roster.LABEL
    assert "`moxi comms agents`" in shim_roster.LABEL, "truth in labeling names the cure"
    assert body == shim_roster.LABEL + "\n" + out.rstrip("\n")
    assert "curator@proj-a" not in envelope and "message(s)" not in envelope, \
        "waiting mail is the --seats view's"


def test_the_snapshot_injects_no_seat_context(rig, capsys):
    """Agents get NO slot/seat context in anything
    injected. The snapshot is the DEFAULT rendering -- it never asks for `--seats` --
    so the guarantee holds through the one renderer, not through a second filter."""
    rig.shim(slot=1, name="planner", scope="proj-a")
    rig.shim(slot=2)
    rig.cli(["send", "--posture", "none", "--new-name", "curator@proj-a", "waiting"], slot=1,
            name="planner", scope="proj-a")
    wait_until(lambda: queued(rig.daemon, "curator@proj-a"))

    envelope, online = shim_roster.snapshot(daemon_socket(), env={})
    assert online
    assert "slot" not in envelope and "seat" not in envelope
    assert "planner@proj-a" in envelope and "chosen" in envelope, "names and tiers stay"
    assert "adapter claude-shim" in envelope

    # ...and the same holds of the text the listener hands turns for the session.
    injected = listener.answer(event("ready", env=session_env(name="x")))
    assert "planner@proj-a" in injected
    assert "slot" not in injected and "seat" not in injected


def test_the_verb_output_is_unchanged_by_the_extraction(rig):
    """`test_agents_lists_bindings_and_pending` pins the shape; this pins the empty
    roster's exact line, which the race note keys on."""
    code, out = rig.cli(["agents"], slot=1)
    assert code == 0
    assert out == f"{roster_view.NO_BINDINGS}\n"
    assert roster_view.NO_BINDINGS == "no live bindings — nothing is addressable right now."


def test_offline_is_the_single_honest_line(rig, capsys):
    rig.daemon.stop()
    envelope, online = shim_roster.snapshot(daemon_socket(), env={"MOXI_COMMS_NAME": "x"})
    assert not online
    assert envelope == shim_roster.with_block(
        shim_roster.OFFLINE, comms=shim_roster.comms_command({}))
    assert "\n" not in shim_roster.OFFLINE, \
        "one line; no label, no race note rides the outage"
    assert "`moxi comms agents`" in envelope
    assert "comms roster:" in capsys.readouterr().err, "diagnostics go to stderr"


def test_the_race_line_appears_only_when_stamped_and_empty(rig):
    """The hook may beat the shim's own registration by milliseconds. An empty
    roster in an identity-stamped session gets ONE honest line about its own binding
    -- never a claim that partners are unreachable."""
    envelope, online = shim_roster.snapshot(daemon_socket(), env={"MOXI_COMMS_NAME": "rev"})
    assert online
    lines = envelope.split("\n\n", 2)[2].splitlines()
    assert lines[1] == "no live bindings — nothing is addressable right now."
    assert len(lines) == 3 and "rev" in lines[2]
    assert "may not have completed registration" in lines[2]
    assert "unreachable" not in lines[2]

    envelope, _ = shim_roster.snapshot(daemon_socket(), env={})
    assert len(envelope.split("\n\n", 2)[2].splitlines()) == 2, \
        "no identity stamp, no race line"

    rig.shim(slot=1, name="planner", scope="proj-a")
    envelope, _ = shim_roster.snapshot(daemon_socket(), env={"MOXI_COMMS_NAME": "planner"})
    assert "may not have completed registration" not in envelope, \
        "a non-empty roster needs no caveat -- the staleness label carries it"


# ------------------------------------------------------------------- the listener

def session_env(name="reviewer", sid="ls1", **extra) -> dict:
    """A launched session's environment: the stamps and its claim socket."""
    s = session.Session(sid, name=name, scope="proj-a", project="proj-a")
    s.write()
    return dict(s.env(), **extra)


def test_the_listener_answers_layer_one_at_ready(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    env = session_env()
    expected, _ = shim_roster.render_snapshot(shim_roster.fetch(daemon_socket()), env=env)
    assert listener.answer(event("ready", env=env)) == expected
    assert expected.startswith(shim_roster.BLOCK) and "planner@proj-a" in expected
    memory = listener.memory_path(env)
    assert memory == session.sessions_dir() / "ls1" / "roster.json"
    assert "planner@proj-a" in json.loads(memory.read_text()), "ready seeds the memory"


def test_the_listener_gives_the_joins_at_a_prompt_and_nothing_when_nothing_changed(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    env = session_env()
    listener.answer(event("ready", env=env))
    assert listener.answer(event("prompt", env=env)) is None, "most prompts get nothing"
    rig.shim(slot=3, name="curator", scope="proj-a")
    delta = listener.answer(event("prompt", env=env))
    assert delta.startswith(shim_roster.DELTA_OPEN) and "curator@proj-a" in delta
    assert listener.answer(event("prompt", env=env)) is None


def test_offline_the_listener_gives_the_block_and_the_offline_line(rig, capsys):
    rig.daemon.stop()
    env = session_env()
    assert listener.answer(event("ready", env=env)) == \
        shim_roster.with_block(shim_roster.OFFLINE, comms=shim_roster.comms_command(env))
    assert listener.answer(event("prompt", env=env)) is None
    capsys.readouterr()


@pytest.mark.parametrize("partial", [
    {"pending": {}, "waiting": {}},                # a daemon that stopped sending bindings
    {"bindings": {}},                              # right key, wrong type
    {"bindings": None, "waiting": {}},
    {"error": "unknown query"},
    [],
])
def test_a_roster_response_this_build_cannot_RENDER_is_treated_as_offline(
        rig, monkeypatch, partial):
    """A version-skewed daemon answers. A response we cannot render is a roster we did
    not get: the offline line, with the block."""
    monkeypatch.setattr(shim_roster, "fetch", lambda *a, **k: partial)
    env = session_env()
    injected = listener.answer(event("ready", env=env))
    assert injected == shim_roster.with_block(shim_roster.OFFLINE,
                                              comms=shim_roster.comms_command(env))
    assert injected.startswith(shim_roster.BLOCK), "the block ships regardless"


def test_a_session_comms_did_not_launch_gets_nothing(rig):
    rig.shim(slot=1, name="planner", scope="proj-a")
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOXI_COMMS_NAME")}
    assert listener.answer(event("ready", env=env)) is None
    assert listener.answer(event("prompt", env=env)) is None


def test_the_listener_answers_for_claude_sessions_only(rig):
    env = session_env()
    assert listener.answer(event("ready", env=env, harness="codex")) is None
    assert listener.answer(event("turn", env=env)) is None


def test_a_claim_socket_outside_a_session_dir_gets_layer_one_and_no_deltas(rig):
    from moxi_comms.paths import runtime_root
    env = dict(session_env(), MOXI_COMMS_CLAIM_SOCKET=str(runtime_root() / "other.sock"))
    assert listener.memory_path(env) is None
    assert listener.answer(event("ready", env=env)).startswith(shim_roster.BLOCK)
    assert listener.answer(event("prompt", env=env)) is None


def test_comms_registers_the_listener_by_entry_point_and_the_target_is_stdlib_only():
    from importlib.metadata import entry_points

    from moxi_comms import listeners
    (ep,) = [e for e in entry_points(group="moxi.turns.listeners") if e.name == "comms"]
    assert ep.value == "moxi_comms.listeners"
    assert listeners.LISTENERS == ((("ready", "prompt"), "moxi_comms.listener:answer"),)
    tree = ast.parse(Path(listeners.__file__).read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


def test_a_partial_roster_response_does_not_advance_the_delta_memory(rig, tmp_path,
                                                                     monkeypatch, capsys):
    """Same honesty on the per-prompt side: unrenderable is a silent skip, and the state
    file stays where it was so the next good diff carries the accumulated truth."""
    state = tmp_path / "roster.json"
    state.write_text(json.dumps({"planner@proj-a": "chosen"}))
    monkeypatch.setattr(shim_roster, "fetch", lambda *a, **k: {"waiting": {}})
    envelope, online = shim_roster.delta(daemon_socket(), state, env={})
    assert (envelope, online) == (None, False)
    assert json.loads(state.read_text()) == {"planner@proj-a": "chosen"}
    capsys.readouterr()


# ------------------------------------------------------------------- the launcher

def test_the_launcher_writes_no_settings_and_passes_no_comms_flag():
    """comms has no hook, plugin or settings file: the label, the
    passthrough, and nothing else of comms' on the argv."""
    s = session.Session("lch1", name="reviewer", scope="proj-a", project="proj-a")
    made = s.write()
    assert made == s.dir and sorted(p.name for p in made.iterdir()) == []
    assert s.claude_argv(["--model", "x"]) == [
        "claude", "--model", "x", "-n", "reviewer-at-proj-a"]


def test_files_an_earlier_launch_left_are_removed():
    s = session.Session("lch4", name="reviewer", project="proj-a")
    s.dir.mkdir(parents=True)
    (s.dir / "settings.json").write_text("{}")
    (s.dir / "mcp.json").write_text("{}")
    s.write()
    assert not (s.dir / "settings.json").exists() and not (s.dir / "mcp.json").exists()


def test_everything_after_the_double_dash_reaches_claude_unchanged(capsys):
    assert session.split_passthrough(["--name", "x", "--", "-p", "--", "--name", "y"]) == \
        (["--name", "x"], ["-p", "--", "--name", "y"])
    sid = "5e55a1d0-0000-4000-8000-0000000000c2"
    assert session.main(["--name", "rev", "--session-id", sid, "--dry-run", "--",
                         "--model", "opus", "--", "-n", "z"]) == 0
    out = capsys.readouterr().out
    launch = next(line for line in out.splitlines() if line.startswith("  launch"))
    assert launch.endswith(f"claude --session-id {sid} --model opus -- -n z -n rev"), \
        "the named id goes ahead of claude's own --"
    assert "--settings" not in launch and "hooks" not in out


def test_moxi_comms_claude_is_the_launcher(capsys):
    from moxi_comms import cli
    sid = "5e55a1d0-0000-4000-8000-0000000000c5"
    assert cli.main(["claude", "--name", "rev", "--session-id", sid, "--dry-run"],
                    prog="moxi comms") == 0
    out = capsys.readouterr().out
    assert out.startswith(f"session {sid}")
    assert f"  launch     claude --session-id {sid} -n rev" in out
    assert cli.main(["claude", "--name", "bad name"], prog="moxi comms") == 2
    assert capsys.readouterr().err.startswith("moxi comms claude: --name ")


def test_the_session_env_stamps_the_project_and_the_claim_socket_only():
    s = session.Session("lch3", name="reviewer", scope="proj-a", project="proj-a")
    env = s.env(base={"MOXI_COMMS_SHIM_SOCKET": "/old.sock"})
    assert env["MOXI_COMMS_PROJECT"] == "proj-a"
    assert env["MOXI_COMMS_CLAIM_SOCKET"] == str(s.dir / "claim.sock")
    assert "MOXI_COMMS_SHIM_SOCKET" not in env
    assert not [k for k in env if "CHANNEL" in k or "LAUNCH_PID" in k]


def test_the_launcher_names_no_hook_command():
    assert not [name for name in ("ping_command", "roster_command", "hook_settings")
                if hasattr(session, name)]


def test_the_shim_has_no_mcp_seam_and_needs_the_inject_file(capsys):
    from moxi_comms.adapters import claude_shim

    assert claude_shim.main(["--name", "x"]) == 2
    assert "--inject-file" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        claude_shim.build_parser().parse_args(["--mcp"])


# --------------------------------------------------------------- the launcher's label and refusals

def test_the_native_address_renders_a_project_scope_with_at():
    """The harness reserves `@` in its own address grammar (a native send to
    `charset-probe@nonexistent` was refused: "to must be a bare teammate name"), so a
    `name@scope` label would leave a project-scoped session natively unaddressable.
    Project scope renders `name-at-scope`; the identity the shim registers and the
    environment stamps
    is untouched, still `@`-spelled. Two spellings, one direction: identity -> label."""
    s = session.Session("abc126a", name="reviewer", scope="proj-a", project="proj-a")
    s.write()
    argv = s.claude_argv()
    assert argv[-2:] == ["-n", "reviewer-at-proj-a"]
    assert argv[-1] == session.native_name(s.name, s.scope)
    assert "@" not in argv[-1], "the harness cannot address an `@`; that is the whole point"

    # The COMMS side keeps its `@` spelling. Nothing about the identity moved.
    assert scoping.qualify(s.name, s.scope) == "reviewer@proj-a"
    assert s.env(base={})["MOXI_COMMS_NAME"] == "reviewer"
    assert s.env(base={})["MOXI_COMMS_SCOPE"] == "proj-a"

    # `--project p` alone does not take the scope with it: the session is
    # global, so the label is the bare name, and the project only feeds the environment.
    p = session.Session("abc126b", name="reviewer", project="proj-b")
    p.write()
    assert p.claude_argv()[-2:] == ["-n", "reviewer"]
    assert p.env(base={})["MOXI_COMMS_GLOBAL"] == "1"
    assert p.env(base={})["MOXI_COMMS_PROJECT"] == "proj-b"


def test_two_scopes_of_one_name_stay_DISTINCT_natively():
    """The cross-scope collision, pinned as a test: the label is
    the only thing a sender types, so two identities that differ must differ here too. A
    render that dropped the scope -- or truncated it -- would silently merge `main@proj-a`
    and `main@proj-b` into one native addressee."""
    a = session.Session("abc126x", name="main", scope="proj-a", project="proj-a")
    b = session.Session("abc126y", name="main", scope="proj-b", project="proj-b")
    assert session.native_name("main", "proj-a") == "main-at-proj-a"
    assert session.native_name("main", "proj-b") == "main-at-proj-b"
    assert a.claude_argv()[-1] != b.claude_argv()[-1]
    # and neither collides with the bare global `main`
    assert session.native_name("main", scoping.GLOBAL) == "main"
    assert len({a.claude_argv()[-1], b.claude_argv()[-1],
                session.native_name("main", scoping.GLOBAL)}) == 3


def test_the_launchers_label_is_what_n_carries(tmp_path, monkeypatch):
    """A caller with its own session-name field names the Claude Code session through
    `--native-name`; `-n` carries it verbatim, last. No label = the identity spelling."""
    monkeypatch.setattr(session, "sessions_dir", lambda: tmp_path / "sessions")
    labelled = session.Session("s1", name="reviewer", scope="proj-a",
                               native="Sep-15-2026 15:19 reviewer")
    argv = labelled.claude_argv(["--model", "opus"])
    assert argv[-4:] == ["--model", "opus", "-n", "Sep-15-2026 15:19 reviewer"]
    assert labelled.native_label() == "Sep-15-2026 15:19 reviewer"
    plain = session.Session("s2", name="reviewer", scope="proj-a")
    assert plain.claude_argv()[-1] == "reviewer-at-proj-a"


def test_a_GLOBAL_identity_renders_BARE_in_the_native_address():
    """A global identity renders bare, so the native label of a global session is the
    bare name and gains no uniqueness from qualification -- exactly the ambiguity the
    one-way rule already names."""
    g = session.Session("abc126c", name="herald", global_=True, project="proj-a")
    g.write()
    argv = g.claude_argv(["--model", "opus"])
    assert argv[-2:] == ["-n", "herald"], "global renders bare, not herald@global"
    assert "herald@global" not in argv
    assert "herald-at-global" not in argv


def test_the_x_at_y_ambiguity_is_DECLARED_not_defended(capsys):
    """A GLOBAL identity literally named `x-at-y` renders the
    same native label as project-scoped `x@y`. Both spellings pass NAME_RE, so neither is
    refused, and this pins that comms builds NO machinery against the collision: the
    harness auto-renames native duplicates and nobody parses a label back into an
    identity, so the cost is a rename, not a confusion. If someone later 'fixes' this by
    escaping or refusing, this test should fail loudly and be reconsidered, not patched."""
    collide = session.Session("abc126z", name="x-at-y", global_=True, project="proj-a")
    scoped = session.Session("abc126w", name="x", scope="y", project="y")
    assert collide.claude_argv()[-1] == scoped.claude_argv()[-1] == "x-at-y"
    # the identities themselves never collide -- only the label does
    assert scoping.qualify("x-at-y", scoping.GLOBAL) != scoping.qualify("x", "y")
    # and the launcher refuses neither name
    assert session.main(["--name", "x-at-y", "--global",
                         "--session-id", "5e55a1d0-0000-4000-8000-0000000000a1",
                         "--dry-run"]) == 0
    assert "-n x-at-y" in capsys.readouterr().out


def test_nothing_parses_a_native_label_BACK_into_an_identity():
    """The one-way rule as what comms refuses to build: `-at-` is WRITTEN in exactly one
    place and read
    nowhere. A reverse index would begin with a second module that knows the separator."""
    def code_only(path: Path) -> str:
        """CODE as text: docstrings dropped, comments never in the AST at all, so prose
        ABOUT the separator ("tolerate-at-launch") is not mistaken for a use of it. The
        idiom is test_cards.code_text's, next door."""
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and body:
                first = body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                        and isinstance(first.value.value, str):
                    body[0] = ast.Pass()
        return ast.unparse(tree)

    package = Path(session.__file__).resolve().parents[1]
    writer = package / "adapters" / "session.py"
    for path in sorted(package.rglob("*.py")):
        if path == writer:
            continue
        assert "-at-" not in code_only(path), \
            f"{path.name} knows the native separator; the render has one home"
    body = writer.read_text()
    assert body.count('"-at-"') == 1, "one spelling of the separator, one constant"
    for reverse in ("split", "partition", "rsplit", "removesuffix", "removeprefix",
                    "replace", "index", "find"):
        assert f"NATIVE_AT.{reverse}" not in body, "the separator is written, never read"
        assert f".{reverse}(NATIVE_AT" not in body, "the separator is written, never read"
    assert not hasattr(session, "parse_native_name"), "no parse-back helper, by rule"
    assert not [n for n in dir(session)
                if "native" in n.lower() and n not in {"native_name", "NATIVE_AT"}], \
        "one native-label symbol pair, both write-side"


def test_the_comms_render_sits_AFTER_the_passthrough_so_a_stale_n_is_overridden():
    """The position is the mechanism. A launcher that has not yet dropped its own
    `-n` puts it in the passthrough; comms' render lands later in the argv and wins,
    because a repeated `-n` is last-wins (measured at 2.1.235: `-n probeA -n
    probeB` stores `probeB`). Comms neither inspects nor strips `extra` --
    the stale flag is still passed through verbatim, it is simply outranked."""
    s = session.Session("abc126d", name="reviewer", scope="proj-a", project="proj-a")
    s.write()
    argv = s.claude_argv(["-n", "stale-launcher-label", "--model", "opus"])
    assert argv.count("-n") == 2, "the operator's/launcher's flag is passed through, not stripped"
    assert argv.index("-n") < argv.index("-n", argv.index("-n") + 1)
    assert argv[-2:] == ["-n", "reviewer-at-proj-a"], "the comms render is the LAST -n"
    assert "stale-launcher-label" in argv


def test_a_nameless_Session_renders_no_native_address():
    """`main()` refuses a nameless launch (twice test-pinned below), so this arm is
    CLI-unreachable -- but `Session.__init__` accepts name=None and `claude_argv` is
    public, and without the fence a direct construction hands `os.execve` either
    `-n None@proj-a` or a literal `None` inside the argv list."""
    s = session.Session("abc126e", name=None, scope="proj-a", project="proj-a")
    s.write()
    argv = s.claude_argv(["--model", "opus"])
    assert "-n" not in argv
    assert all(isinstance(part, str) for part in argv)
    assert argv[-2:] == ["--model", "opus"]


def test_the_identity_is_stamped_where_the_cli_reads_it():
    s = session.Session("abc127", name="reviewer", slot=2, scope="proj-a", project="proj-a")
    env = s.env(base={})
    assert env["MOXI_COMMS_NAME"] == "reviewer" and env["MOXI_COMMS_SLOT"] == "2"
    assert env["MOXI_COMMS_SCOPE"] == "proj-a" and env["MOXI_COMMS_PROJECT"] == "proj-a"
    assert env["MOXI_COMMS_CLAIM_SOCKET"] == str(s.claim_socket)
    assert "MOXI_COMMS_SHIM_SOCKET" not in env, "no hook ping reads a socket now"
    assert "MOXI_COMMS_GLOBAL" not in env

    g = session.Session("abc128", name="herald", global_=True, project="proj-a")
    assert g.env(base={})["MOXI_COMMS_GLOBAL"] == "1"
    assert g.env(base={})["MOXI_COMMS_SCOPE"] == "global"


def test_a_nameless_slotless_launch_is_refused_rather_than_guessed(capsys):
    assert session.main(["--dry-run"]) == 2
    assert "agent-N" in capsys.readouterr().err


@pytest.mark.parametrize("slot", ["3", "next-free"])
def test_a_nameless_SLOT_launch_is_refused_because_a_claim_is_a_name(capsys, slot):
    """The last route to the fabricated identity. `--comm-slot 3` alone used to launch a
    session whose environment carried MOXI_COMMS_SLOT and no MOXI_COMMS_NAME, and whose `moxi comms
    send` then wore `slot-3` on the wire. The refusal says a claim needs a name, and
    carries no slot vocabulary beyond naming the flag the operator typed."""
    assert session.main(["--comm-slot", slot, "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "pass --name" in err
    assert "A claim is a name" in err


@pytest.mark.parametrize("name", ["bad name@x!", "main@acme", "has space", "sl/ash"])
def test_a_name_outside_the_grammar_is_refused_at_the_LAUNCHER(capsys, name):
    """`--name 'bad name@x!'` must not be accepted in full, rendered into `-n` and then
    fail at register time inside the session where nobody reads the log. The check
    belongs here, at the launcher, against comms' own NAME_RE -- and it catches the
    likeliest operator error too, a qualified name typed into --name where the scope half
    has its own flag."""
    assert session.main(["--name", name, "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "is not a name" in err


def test_a_bad_slot_is_refused_at_the_launcher(capsys):
    assert session.main(["--comm-slot", "seven", "--dry-run"]) == 2


def test_the_old_slot_spelling_is_gone(capsys):
    """The flag is `--comm-slot`, so it cannot be confused with another launcher's slot
    flag."""
    with pytest.raises(SystemExit) as exc:
        session.main(["--name", "reviewer", "--slot", "3", "--dry-run"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --slot" in capsys.readouterr().err

