"""`comms whois <name>` -- local, read-only, one file.

What these pin: the 10-line cap and the character budget it needs to have teeth; the
help model (no daemon, no db, no credential, no working environment -- not even a
readable launch stamp); absence as a normal answer and refusal only for a request comms
will not answer; the CLAIM scope as own-scope, which is what keeps `comms whois x` and
`comms send x` talking about the same identity; the render contract, because a card is
untrusted text authored by another agent and rendered into this one's context; the
native-session display claim and its one-way rule; and the import fence that makes
"never an authority surface" structural instead of promised.
"""

import ast
import io
import re
from pathlib import Path

import pytest

from moxi_comms import cards, cli
from moxi_comms.constants import CARD_MAX_CHARS, CARD_MAX_LINES, CARD_READ_LIMIT

ALARM = ("error", "failed", "cannot")


def run(argv, *, env=None, socket_path="/nonexistent/comms-whois-must-not-connect.sock"):
    """The verb with nothing behind it: a db path that is not a file and a socket path
    no daemon is on -- test_help.py's posture, for the same reason. If `whois` ever grows
    a round-trip, this stops passing."""
    out = io.StringIO()
    ctx = cli.Context(db_path="/nonexistent/comms-whois-must-not-read.db",
                      socket_path=socket_path, env={} if env is None else env, out=out)
    return cli.main(argv, ctx=ctx), out.getvalue()


def write_card(qualified: str, text: str) -> Path:
    path = cards.card_path(qualified)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def body(out: str) -> list[str]:
    """The card's own lines: everything inside the envelope."""
    lines = out.rstrip("\n").split("\n")
    assert lines[0].startswith("[comms card] ") and lines[-1] == cards.FOOTER
    return lines[1:-1]


# ------------------------------------------------------------------- the line cap

def test_a_ten_line_card_renders_whole():
    text = "\n".join(f"line {n}" for n in range(1, 11))
    write_card("planner@proj-a", text)
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert body(out) == [f"  line {n}" for n in range(1, 11)]
    assert "over budget" not in out


def test_an_eleven_line_card_shows_ten_and_says_so():
    """The line cap itself."""
    text = "\n".join(f"line {n}" for n in range(1, 12))
    write_card("planner@proj-a", text)
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert "line 11" not in out, "the eleventh line is not shown anywhere"
    assert "over budget: this card is 11 lines; comms shows the first 10." in out
    assert len([b for b in body(out) if b.startswith("  line ")]) == 10


# ------------------------------------------------------------------- budget mechanics

def test_a_trailing_newline_is_not_an_eleventh_line():
    write_card("planner@proj-a", "\n".join(f"line {n}" for n in range(1, 11)) + "\n")
    assert "over budget" not in run(["whois", "planner@proj-a"])[1]
    write_card("planner@proj-a", "\n".join(f"line {n}" for n in range(1, 12)) + "\n")
    assert "over budget" in run(["whois", "planner@proj-a"])[1]


def test_blank_lines_count():
    """Nothing is exempt; an exempt line is how a budget starts growing."""
    write_card("planner@proj-a", "\n" * 9 + "one\ntwo")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert "over budget: this card is 11 lines" in out
    assert "two" not in out


def test_the_character_budget_binds_independently():
    """A line cap alone is evaded by one very long line."""
    write_card("planner@proj-a", "x" * 900)
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert body(out) == ["  " + "x" * CARD_MAX_CHARS,
                         f"  over budget: this card is 900 characters; comms shows the "
                         f"first {CARD_MAX_CHARS}."]


def test_an_enormous_card_is_not_slurped_and_the_notice_does_not_guess():
    """The read cap: the reader never reports a count of a file it did not
    finish reading."""
    path = write_card("planner@proj-a", "a line of text\n" * 200_000)   # ~2.8 MB
    assert path.stat().st_size > 2_000_000
    data, truncated = cards._read(path)
    assert truncated and len(data) == CARD_READ_LIMIT
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert f"over budget: this card is more than {CARD_MAX_LINES} lines" in out
    assert not re.search(r"this card is \d+ lines", out), "no counted total is invented"
    assert len(out) < 2000


def test_the_notice_states_only_the_cut_comms_made():
    """When the LINE cap binds first the character cap may never bind at all, so a
    notice reporting the body's character count would name a cut comms did not make."""
    write_card("planner@proj-a", "\n".join("x" * 50 for _ in range(20)))   # 1019 chars
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert "over budget: this card is 20 lines; comms shows the first 10." in out
    assert "characters" not in out, "the character cap did not bind; nothing claims it did"
    shown = [b for b in body(out) if not b.startswith("  over budget")]
    assert len(shown) == 10 and all(len(s) == 52 for s in shown)


def test_both_caps_binding_reports_each_against_what_it_cut():
    """The other half: when both bind, the character clause counts the lines that were
    eligible to be shown -- which is the quantity it actually truncated."""
    write_card("planner@proj-a", "\n".join("y" * 200 for _ in range(20)))
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert ("over budget: this card is 20 lines; comms shows the first 10, and 2009 "
            "characters in the first 10 lines; comms shows the first 800.") in out
    shown = [b for b in body(out) if not b.startswith("  over budget")]
    assert sum(len(s) - 2 for s in shown) + len(shown) - 1 == CARD_MAX_CHARS


def test_an_empty_or_newline_only_card_has_no_body_lines():
    """The single trailing newline is the one the budget removes, and nothing is left
    behind it."""
    assert cards._budget("", False) == ([], "")
    assert cards._budget("\n", False) == ([], "")
    assert cards._budget("\n\n", False) == (["", ""], "")
    write_card("planner@proj-a", "\n")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0 and body(out) == []


def test_the_over_budget_notice_renders_inside_the_envelope():
    """An unenveloped notice is a line a card body could forge."""
    write_card("planner@proj-a", "\n".join(f"line {n}" for n in range(1, 20)))
    out = run(["whois", "planner@proj-a"])[1]
    notice = [b for b in body(out) if "over budget" in b]
    assert len(notice) == 1
    assert notice[0].startswith("  "), "the notice carries the body indent"
    assert body(out)[-1] == notice[0], "after the card, before the footer"


# ------------------------------------------------------------------- the help model

def test_the_verb_answers_with_nothing_behind_it(monkeypatch):
    """Copied from test_help.py: both seams a verb could reach the world through
    are booby-trapped, and so is the socket."""
    def boom(*a, **kw):
        raise AssertionError("comms whois must not connect to anything")

    monkeypatch.setattr(cli.Context, "daemon", boom)
    monkeypatch.setattr(cli.Context, "db", boom)
    monkeypatch.setattr(cli.Context, "gateway", property(boom))
    monkeypatch.setattr(cli.DaemonClient, "connect", boom)
    import socket
    monkeypatch.setattr(socket.socket, "connect", boom)

    write_card("planner@proj-a", "Design + planning for proj-a.")
    assert run(["whois", "planner@proj-a"])[0] == 0
    assert run(["whois", "nobody@proj-a"])[0] == 0
    assert run(["whois", "agent-7"])[0] == 2


def test_comms_offline_is_unreachable():
    """There is no daemon in this path to be down, so exit 4 cannot happen."""
    verb = function_code(Path(cli.__file__), "cmd_whois")
    assert set(re.findall(r"EXIT_\w+", verb)) == {"EXIT_OK", "EXIT_REFUSED", "EXIT_FAILED"}
    assert "EXIT_" not in code_text(Path(cards.__file__)), "the reader knows no exit codes"


def test_a_malformed_launch_stamp_is_answered_not_tracebacked():
    """`sender_from_env` has three answers and one of them is an exception;
    this verb never calls it, and never looks at MOXI_COMMS_SLOT at all. The sibling of
    test_help.py::test_help_needs_no_claim_and_no_environment."""
    env = {"MOXI_COMMS_NAME": "planner", "MOXI_COMMS_SCOPE": "proj-a", "MOXI_COMMS_SLOT": "not-a-number"}
    write_card("reviewer@proj-a", "I review things.")
    code, out = run(["whois", "reviewer@proj-a"], env=env)
    assert code == 0 and "I review things." in out
    # ...and the whole malformed-stamp class is out of scope by construction: the reader
    # never names the spend-side variable, and never imports the function that raises.
    assert "MOXI_COMMS_SLOT" not in code_text(Path(cards.__file__))
    assert "moxi_comms.sender" not in module_imports(Path(cards.__file__))


# ------------------------------------------------------------------- answers and refusals

def test_absence_is_a_normal_answer():
    """Most identities have no card; nothing is wrong, and nothing is created."""
    code, out = run(["whois", "ghost@proj-a"])
    assert code == 0
    assert "no card" in out and "nothing is wrong" in out
    for word in ALARM:
        assert word not in out.lower(), f"a miss must not say {word!r}"
    assert not cards.cards_dir().exists(), "a read-only verb creates no directory"


def test_the_self_miss_names_the_path_and_a_peer_miss_does_not():
    """The holder is told where to write, and how; peer-facing output carries
    no filesystem plumbing."""
    env = {"MOXI_COMMS_NAME": "planner", "MOXI_COMMS_SCOPE": "proj-a"}
    code, out = run(["whois", "planner@proj-a"], env=env)
    assert code == 0
    assert str(cards.card_path("planner@proj-a")) in out
    assert "rename it into place" in out
    code, out = run(["whois", "reviewer"], env=env)
    assert code == 0 and "/" not in out
    # A bare `planner` is the global planner, a peer of planner@proj-a ([C10]).
    code, out = run(["whois", "planner"], env=env)
    assert code == 0 and "/" not in out


@pytest.mark.parametrize("bad", ["a/b", "", "a\x00b", "planner@", "a@b@c", "../x", "x y"])
def test_a_malformed_name_refuses_before_composing_a_path(bad, monkeypatch):
    """Refused before any path exists. Path escape is structural (NAME_RE's
    charset has no separator in it), so this is the shape being named, not a guard."""
    monkeypatch.setattr(cards, "card_path",
                        lambda q: pytest.fail(f"composed a path for {q!r}"))
    code, out = run(["whois", bad])
    assert code == 2
    assert "is not a comms identity" in out and "`name@scope`" in out


def test_a_trailing_newline_in_a_name_is_refused(monkeypatch):
    """Contained here: `NAME_RE`'s `$` matches before a final newline, so `x\\n`
    passes `.match()` — and this verb is the first surface to put a validated name
    straight into rendered envelope text, where a raw newline splits the header in two."""
    monkeypatch.setattr(cards, "card_path",
                        lambda q: pytest.fail(f"composed a path for {q!r}"))
    assert run(["whois", "q\n"])[0] == 2
    assert run(["whois", "q@proj-a\n"])[0] == 2


def test_dot_and_dotdot_are_accepted_and_inert():
    """Both match NAME_RE and compose a harmless filename. Pinned as
    accepted-and-inert so a later reader does not assume a guard that is not there."""
    assert cards.card_path("..") == cards.cards_dir() / "...md"
    assert cards.card_path(".") == cards.cards_dir() / "..md"
    code, out = run(["whois", ".."])
    assert code == 0 and "no card" in out


def test_an_ephemeral_name_is_refused():
    """An agent-N name describes nobody in particular, and `_auto_seq` resets at daemon
    init, so the number really is handed to a stranger later."""
    code, out = run(["whois", "agent-7"])
    assert code == 2
    assert "no continuity" in out and "agent-N" in out


def test_a_card_that_cannot_be_decoded_exits_five():
    """Never mojibake, never a traceback, and it says whose card it is."""
    path = cards.card_path("planner@proj-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"role: planner\n\xff\xfe not utf-8\n")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 5
    assert "planner@proj-a" in out and "not valid UTF-8" in out
    assert "�" not in out


# ------------------------------------------------------------------- hostile files

def test_a_fifo_at_a_card_path_answers_instead_of_hanging():
    """The byte cap bounds what a card can cost in context and memory; nothing
    bounded what it could cost in TIME. A FIFO blocked `open()` forever, wedging every
    peer that looked that identity up."""
    import os
    import time as _time
    path = cards.card_path("victim@proj-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    started = _time.monotonic()
    code, out = run(["whois", "victim@proj-a"])
    assert _time.monotonic() - started < 5, "the reader must not wait on a card"
    assert code == 5
    assert "victim@proj-a" in out and "not a regular file" in out


def test_a_symlink_at_a_card_path_is_refused_not_followed():
    """A followed symlink renders up to 8 KB of any readable file inside a
    `claims to be self-authored` envelope: one agent choosing what lands in another's
    context, which is an agent boundary even inside the single-operator model."""
    secret = cards.cards_dir().parent / "not-a-card.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("SECRET-NOT-A-CARD\n")
    path = cards.card_path("planner@proj-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(secret)
    code, out = run(["whois", "planner@proj-a"])
    assert code == 5
    assert "SECRET-NOT-A-CARD" not in out
    assert "symbolic link" in out and "planner@proj-a" in out


def test_a_directory_at_a_card_path_is_refused():
    """The same guard, the mundane case."""
    cards.card_path("planner@proj-a").mkdir(parents=True)
    code, out = run(["whois", "planner@proj-a"])
    assert code == 5 and "not a regular file" in out


# ------------------------------------------------------------------- scope resolution

def test_a_bare_name_reads_the_global_card_whatever_the_claim_scope():
    """A bare name is the global identity, whoever asks ([C10], §7.10.2): neither the
    claim scope nor the working directory picks a scoped card."""
    write_card("x@proj-a", "the claim-scope card")
    write_card("x@proj-b", "the working-directory card")
    write_card("x", "the global card")
    code, out = run(["whois", "x"],
                    env={"MOXI_COMMS_SCOPE": "proj-a", "MOXI_COMMS_PROJECT": "proj-b"})
    assert code == 0
    assert out.startswith("[comms card] x (")
    assert "the global card" in out
    assert "claim-scope" not in out and "working-directory" not in out
    code, out = run(["whois", "x@proj-a"], env={"MOXI_COMMS_SCOPE": "proj-b"})
    assert code == 0
    assert out.startswith("[comms card] x@proj-a ") and "the claim-scope card" in out


def test_comms_global_makes_global_the_own_scope():
    write_card("x", "the global card")
    write_card("x@proj-a", "the scoped card")
    code, out = run(["whois", "x"],
                    env={"MOXI_COMMS_GLOBAL": "1", "MOXI_COMMS_SCOPE": "proj-a"})
    assert code == 0
    assert out.startswith("[comms card] x (")
    assert "the global card" in out


def test_with_no_claim_the_project_key_is_not_a_scope():
    """An unclaimed process gets the global card for a bare name: the project key
    (`MOXI_COMMS_PROJECT`, `project_key`'s own override) is never a scope ([R75])."""
    write_card("x@proj-b", "the project card")
    code, out = run(["whois", "x"], env={"MOXI_COMMS_PROJECT": "proj-b"})
    assert code == 0
    assert out.startswith("[comms card] x ") and "the project card" not in out


def test_an_unusable_own_scope_plays_no_part_and_adds_no_note():
    """The own scope is not read for a bare lookup any more, so a scope that is not a
    name changes nothing and is not mentioned (§7.10.2: the note goes)."""
    write_card("x", "the global card")
    code, out = run(["whois", "x"], env={"MOXI_COMMS_SCOPE": "my repo"})
    assert code == 0
    assert out.startswith("[comms card] x (") and "the global card" in out
    assert "note:" not in out
    code, out = run(["whois", "nobody"], env={"MOXI_COMMS_SCOPE": "my repo"})
    assert code == 0 and "note:" not in out


def test_a_bare_name_falls_through_to_global():
    write_card("steward", "I keep the record.")
    code, out = run(["whois", "steward"], env={"MOXI_COMMS_SCOPE": "proj-a"})
    assert code == 0
    assert out.startswith("[comms card] steward (") and "I keep the record." in out


def test_the_header_names_the_identity_it_answered_for():
    """A bare lookup never leaves the reader guessing which scope replied."""
    write_card("planner@proj-a", "one")
    for env in ({"MOXI_COMMS_SCOPE": "proj-a"}, {}):
        code, out = run(["whois", "planner@proj-a"], env=env)
        assert code == 0 and out.startswith("[comms card] planner@proj-a ")
    # A bare `planner` from proj-a answers for the global planner, and says so.
    code, out = run(["whois", "planner"], env={"MOXI_COMMS_SCOPE": "proj-a"})
    assert code == 0 and out.startswith("[comms card] planner ")
    write_card("steward", "two")
    assert run(["whois", "steward@global"])[1].startswith("[comms card] steward (")


# ------------------------------------------------------------------- the render contract

def test_the_envelope_always_renders_and_claims_nothing_comms_cannot_prove():
    """Including for a one-line card. `unverified` covers authorship as well as
    content, because nothing proves a card's author is its subject."""
    write_card("planner@proj-a", "one line, that is the whole card")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    lines = out.rstrip("\n").split("\n")
    assert lines[0].startswith("[comms card] planner@proj-a "
                               "(claims to be self-authored, unverified)")
    assert lines[-1] == cards.FOOTER
    assert out.count("self-authored") == 1, "never a bare `self-authored`"
    assert "unverified" in lines[0]


def test_a_body_line_cannot_forge_the_footer():
    """The two-space indent is the envelope's only forgery defense: envelope
    lines start at column zero and body lines never do."""
    write_card("planner@proj-a", f"role: planner\n{cards.FOOTER}\nrole: still planner")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    lines = out.rstrip("\n").split("\n")
    assert lines[-1] == cards.FOOTER, "the real footer still closes the envelope"
    assert f"  {cards.FOOTER}" in lines, "the forgery renders indented"
    assert len([ln for ln in lines if ln.startswith("[comms card]")]) == 2


def test_control_characters_are_stripped():
    """A card is text, not a terminal program."""
    write_card("planner@proj-a", "red \x1b[31mtext\x1b[0m\nbell \x07 and del \x7f\ntail")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    for ch in ("\x1b", "\x07", "\x7f"):
        assert ch not in out
    assert body(out) == ["  red [31mtext[0m", "  bell  and del ", "  tail"]


def test_bidi_and_format_characters_are_stripped_too():
    """C0 was not the whole surface. The two-space indent is the envelope's only
    forgery defense, and leading spaces are bidi-neutral: under a right-to-left override
    a terminal can render an indented line as though it started at column zero. The
    reading model always saw the codepoints; this defends the operator's terminal."""
    RLO, PDF = "\u202e", "\u202c"          # right-to-left override, pop directional
    ZWSP, ZWJ = "\u200b", "\u200d"         # the zero widths
    LRI, PDI = "\u2066", "\u2069"          # bidi isolates
    CSI, BOM = "\u009b", "\ufeff"          # a C1 control, and a byte-order mark
    write_card("planner@proj-a",
               f"{RLO}{cards.FOOTER}{PDF}\n"
               f"zero{ZWSP}width{ZWJ} joined{LRI} isolate{PDI}\n"
               f"c1{CSI}here{BOM}\ntail")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    for ch in (RLO, PDF, ZWSP, ZWJ, LRI, PDI, CSI, BOM):
        assert ch not in out, f"{ch!r} survived into a peer's context"
    assert body(out) == [f"  {cards.FOOTER}", "  zerowidth joined isolate",
                         "  c1here", "  tail"]
    assert out.rstrip("\n").split("\n")[-1] == cards.FOOTER


def test_nothing_is_interpreted():
    """No markdown rendering, no expansion, no execution: characters, emitted."""
    text = "# Heading **bold**\n$HOME and ${PATH}\n`whoami` $(id) [link](http://x)"
    write_card("planner@proj-a", text)
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert body(out) == [f"  {line}" for line in text.split("\n")]


def test_an_unreadable_mtime_omits_the_age_clause(monkeypatch):
    """The one staleness fact comms states is the file's own age, and it is
    omitted rather than guessed."""
    write_card("planner@proj-a", "role: planner")
    assert "written " in run(["whois", "planner@proj-a"])[1]
    real = Path.stat

    def no_stat(self, *a, **kw):
        raise OSError("no stat here")

    monkeypatch.setattr(Path, "stat", no_stat)
    try:
        code, out = run(["whois", "planner@proj-a"])
    finally:
        monkeypatch.setattr(Path, "stat", real)
    assert code == 0 and "written " not in out
    assert out.startswith("[comms card] planner@proj-a "
                          "(claims to be self-authored, unverified)\n")


# ------------------------------------------------------------------- the native claim

def test_the_native_session_claim_renders_in_place_with_its_marker():
    write_card("planner@proj-a", "role: planner\nnative-session: planner-proj-a\nbye")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert body(out) == ["  role: planner",
                         "  native session (self-claimed, unverified): planner-proj-a",
                         "  bye"]


def test_a_card_without_the_key_renders_no_native_line():
    """Nothing is invented."""
    write_card("planner@proj-a", "role: planner\nI own the roadmap.")
    out = run(["whois", "planner@proj-a"])[1]
    assert "native" not in out.lower()


def test_only_the_first_claim_is_a_claim():
    """Tolerant without ambiguity: a card routes nothing, so a later matching line
    is kept as ordinary text rather than discarded."""
    write_card("planner@proj-a",
               "native-session:\nNative-Session:  first-one \nnative-session: second-one")
    code, out = run(["whois", "planner@proj-a"])
    assert code == 0
    assert body(out) == ["  native-session:",
                         "  native session (self-claimed, unverified): first-one",
                         "  native-session: second-one"]


def test_the_lookup_is_one_way():
    """A native label never resolves back to an identity: the argument is a comms
    identity, and comms never searches card contents."""
    write_card("planner@proj-a", "native-session: planner-proj-a")
    code, out = run(["whois", "planner-proj-a"])
    assert code == 0 and "no card" in out
    assert "planner@proj-a" not in out


# ------------------------------------------------------------------- structure and fences

PACKAGE = Path(cards.__file__).parent


def module_imports(path: Path) -> set[str]:
    """Every module the file imports, absolute-ified. Written here rather than copied:
    nothing in this suite walked an import graph before this test (the unlink-gate
    precedent was an argument about not adding an edge, not a test)."""
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    package = parts[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - node.level + 1] if node.level else []
            base = list(base) + ([node.module] if node.module else [])
            found.add(".".join(base))
            found.update(".".join(base + [a.name]) for a in node.names)
    return found


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return tree


def code_text(path: Path) -> str:
    """The module's CODE as text: comments are not in the AST at all and docstrings are
    dropped, so prose ABOUT a thing is never mistaken for a use of it."""
    return ast.unparse(_strip_docstrings(ast.parse(path.read_text())))


def function_code(path: Path, name: str) -> str:
    for node in ast.walk(_strip_docstrings(ast.parse(path.read_text()))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name} is not in {path}")


def package_modules() -> dict[str, Path]:
    return {".".join(p.relative_to(PACKAGE.parent).with_suffix("").parts): p
            for p in sorted(PACKAGE.rglob("*.py"))}


def test_the_import_graph_helper_sees_a_known_edge():
    """The helper is new machinery, so pin that it actually reads edges before trusting
    it to prove a negative."""
    edges = module_imports(PACKAGE / "cli.py")
    assert "moxi_comms.cards" in edges and "moxi_comms.glossary" in edges


def import_closure(name: str, modules: dict[str, Path]) -> set[str]:
    """Every package module reachable from `name` by following imports transitively."""
    seen, stack = set(), [name]
    while stack:
        current = stack.pop()
        for edge in module_imports(modules[current]):
            # `from .paths import cards_dir` yields both the module and the symbol; only
            # the ones that ARE modules matter.
            if edge in modules and edge not in seen:
                seen.add(edge)
                stack.append(edge)
    return seen


def test_only_the_cli_imports_the_card_reader():
    """The authority fence, structural rather than promised: no card content
    reaches routing, attribution or directors if the modules on those paths cannot import
    the reader.

    An import-CLOSURE test, not a direct-edge one: the claim is "never by the daemon, an
    adapter, attribution, or directors", which is about everything those modules can
    reach. The roster renderer lives in `roster_view`, which pulls nothing behind it, so
    `cli` stays a leaf and the closure claim is true as written.
    """
    modules = package_modules()
    importers = {name for name, path in modules.items()
                 if "moxi_comms.cards" in module_imports(path)} - {"moxi_comms.cards"}
    assert importers == {"moxi_comms.cli"}, "the direct edge"

    reachable = {name for name in modules
                 if "moxi_comms.cards" in import_closure(name, modules)}
    assert reachable == {"moxi_comms.cli"}, "and the whole closure, which is what the " \
                                            "fence claims"
    for module in ("moxi_comms.daemon", "moxi_comms.attribution", "moxi_comms.directors",
                   "moxi_comms.sender", "moxi_comms.directory", "moxi_comms.routing",
                   *(m for m in modules if m.startswith("moxi_comms.adapters."))):
        if module in modules:
            assert "moxi_comms.cards" not in import_closure(module, modules), module

    forbidden = ("daemon", "sender", "directors", "attribution", "routing", "client",
                 "gateway", "directory")
    reader_closure = import_closure("moxi_comms.cards", modules)
    assert not [e for e in reader_closure if e.split(".")[-1] in forbidden]
    # `moxi_comms.envelope` is in the closure: the indent, the scrub and the
    # closed-envelope rule serve cards and delivered message bodies alike, so they live in
    # one module both surfaces import. It is a leaf of leaves -- `unicodedata` and nothing
    # from the package -- so nothing on the routing, attribution or director paths can
    # reach card content through it. `moxi_comms.release` is in it because `paths`
    # names its layout by the module's name (the roots themselves are `moxi.paths`, outside
    # the package); it imports nothing from the package. Neither the config nor the
    # gateway behind it joins this closure.
    assert reader_closure == {"moxi_comms.constants", "moxi_comms.envelope",
                              "moxi_comms.paths", "moxi_comms.release",
                              "moxi_comms.scoping"}, "the reader's own closure"


def test_no_reverse_index_and_no_content_search():
    """The one-way rule, as what comms itself refuses to build. The rule binds
    comms surfaces and the agents this contract governs, not the filesystem: `cards/` is
    an ordinary directory and a grep over it is a reverse index. What is testable is that
    comms builds, caches and ships none."""
    modules = package_modules()
    for name, path in modules.items():
        if name == "moxi_comms.cards":
            continue
        assert "native-session" not in code_text(path), \
            f"{name} reads a native label; the map is carried in one place only"
    reader = code_text(PACKAGE / "cards.py")
    for searcher in ("glob(", "rglob(", "iterdir(", "scandir(", "listdir(", "walk(",
                     "read_text(", "readlines("):
        assert searcher not in reader, f"the reader must not {searcher} the card store"
    assert reader.count("open(") == reader.count("os.open(") == 1, \
        "one file opened, one only, and never by a name-following bare open"


def test_every_shipped_line_that_names_a_verb_says_moxi_comms():
    """The bare `comms` is an opt-in shortcut, so the text an agent reads names the verb
    as `moxi comms <verb>`: the context block, the glossary, the card notice, the stall
    report and the owed reminder."""
    import re

    from moxi_comms import cards, context, glossary, owed, stall

    debt = owed.Debt(message_id="1400000000001", author="planner@proj-a",
                     branch=owed.BRANCH_PEER, reply_to=None, posture="once", at=0.0,
                     session_id=None)
    two = owed.Debt(message_id="1400000000002", author="lead@proj-a",
                    branch=owed.BRANCH_PEER, reply_to=None, posture="once", at=0.0,
                    session_id=None)
    texts = [context.layer_one_block(["lead@proj-a"]), glossary.render_terms(),
             glossary.render_unknown("nope"), cards.LIVENESS, stall.STALL_REPORT,
             owed.reminder_text([debt], 60.0), owed.reminder_text([debt, two], 60.0),
             *(glossary.render_entry(e) for e in glossary.TERMS)]
    bare = re.compile(r"(?<![-\w/.\[])(?<!moxi )comms (send|reply|agents|help|explain|"
                      r"whois|whoami|history|cache|owner|read)\b")
    for text in texts:
        flat = " ".join(text.split())
        assert not bare.search(flat), flat
    assert "`moxi comms send <name> --posture" in context.CONTEXT_BLOCK
    assert "moxi comms reply 1400000000001" in owed.reminder_text([debt], 60.0)
