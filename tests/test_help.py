"""`moxi comms help [<term>]` -- local, read-only, static.

What these pin: the whole table resolves (an entry that exists but cannot be
reached is not tuition), the forms an agent will actually TYPE resolve (it types what
it saw on a frame line, punctuation and all), a miss reads as a miss and not as a
fault, the surface is Discord-free by construction, and the verb touches no daemon,
no record and no credential -- which is what "works offline" means here.
"""

import contextlib
import io

import pytest

from moxi_comms import cli, glossary

# The words this surface exists to keep away from agents (the Discord-free discipline,
# applied to layer 2). `channel` and `slot` are in the list
# because they are real comms internals with agent-facing lookalikes -- the entry for
# `re:` says "the message id shown in the frame line", never "the channel it was in".
FORBIDDEN = ("discord", "webhook", "snowflake", "slot", "channel")


def run(argv, *, socket_path="/nonexistent/comms-help-must-not-connect.sock"):
    """The verb with nothing behind it: a db path that is not a file and a socket path
    that no daemon is on. If `help` ever grows a round-trip, this stops passing."""
    out = io.StringIO()
    ctx = cli.Context(db_path="/nonexistent/comms-help-must-not-read.db",
                      socket_path=socket_path, env={}, out=out)
    return cli.main(argv, ctx=ctx), out.getvalue()


# ------------------------------------------------------------------- every term reaches

@pytest.mark.parametrize("entry", glossary.TERMS, ids=lambda e: e.term)
def test_every_ruled_term_resolves(entry):
    code, out = run(["help", entry.term])
    assert code == 0
    assert entry.text.split(".")[0][:40] in out.replace("\n  ", " ").replace("\n", " ")


@pytest.mark.parametrize("form,term", [
    # aliases, per the table
    ("catchup", "catch-up"), ("backlog", "catch-up"), ("replay", "catch-up"),
    ("director", "directive"), ("reply", "re"), ("thread", "re"),
    # the block's own pointer -- "`moxi comms help <term>` explains ... (tiers, marks,
    # catch-up)" -- and the primer's retired vocabulary
    ("tiers", "tier"), ("marks", "mark"), ("#names", "names"), ("room", "names"),
    # the punctuated forms an agent reads off a frame line
    ("(unverified)", "unverified"), ("[reply-once]", "posture"), ("re:", "re"),
    ("(ephemeral)", "ephemeral"), ("(standing)", "standing"),
    # the audience mark as the frame shows it and as line zero types it
    ("audience:", "audience"), ("audience: operator", "audience"),
    ("audience: unknown", "audience"), ("[audience:operator]", "audience"),
    # case, and the space form of a hyphenated term
    ("UNVERIFIED", "unverified"), ("Catch-Up", "catch-up"), ("catch up", "catch-up"),
])
def test_the_forms_an_agent_types_resolve(form, term):
    assert glossary.lookup(form) is glossary.lookup(term) is not None
    code, out = run(["help", form])
    assert code == 0
    assert out.startswith(f"{term} — ")


def test_help_with_no_argument_lists_every_term():
    code, out = run(["help"])
    assert code == 0
    for entry in glossary.TERMS:
        assert entry.term in out
        for alias in entry.aliases:
            assert alias in out
    assert "moxi comms help <term>" in out


def test_an_unknown_term_is_a_miss_not_a_fault():
    """A typo mid-turn must not read as "comms is broken" -- exit 2 (refused: the verb
    worked, the word did not exist), the miss said plainly, and the list right there."""
    code, out = run(["help", "verifried"])
    assert code == 2
    assert "no entry for 'verifried'" in out
    assert "Nothing is wrong with comms" in out
    for entry in glossary.TERMS:
        assert entry.term in out, "the miss shows the whole list"


# ------------------------------------------------------------------- the two properties

def test_the_glossary_is_discord_free():
    """Contract property, not style: an agent must never meet Discord vocabulary
    through comms. Greppable over the table itself, so a new entry cannot smuggle one
    in past review."""
    blob = "\n".join(f"{e.term} {' '.join(e.aliases)} {e.text}" for e in glossary.TERMS)
    blob += "\n" + glossary.render_terms()
    blob += "\n" + glossary.render_unknown("x")
    blob += "\n".join(glossary.render_entry(e) for e in glossary.TERMS)
    for word in FORBIDDEN:
        assert word not in blob.lower(), f"the glossary must not say {word!r}"


def test_the_verbs_own_usage_text_is_discord_free():
    """`moxi comms help -h` is part of the surface too: argparse prints it, so the flag
    descriptions are agent-facing text and live under the same discipline."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        cli.build_parser().parse_args(["help", "-h"])
    text = buf.getvalue().lower()
    assert "unverified" in text, "the usage text really was captured"
    for word in FORBIDDEN:
        assert word not in text


def test_the_verb_makes_no_socket_call(monkeypatch):
    """Offline is the point: the daemon being down has no bearing on what a word means.
    Both seams a verb could reach the world through are booby-trapped."""
    def boom(*a, **kw):
        raise AssertionError("moxi comms help must not connect to anything")

    monkeypatch.setattr(cli.Context, "daemon", boom)
    monkeypatch.setattr(cli.Context, "db", boom)
    monkeypatch.setattr(cli.Context, "gateway", property(boom))
    monkeypatch.setattr(cli.DaemonClient, "connect", boom)
    import socket
    monkeypatch.setattr(socket.socket, "connect", boom)

    assert run(["help"])[0] == 0
    assert run(["help", "(unverified)"])[0] == 0
    assert run(["help", "nonsense"])[0] == 2


def test_help_needs_no_claim_and_no_environment():
    """Every other verb refuses a process with no claim. This one answers: the reader
    who most needs the vocabulary is often the one whose environment is wrong."""
    code, out = run(["help", "standing"])
    assert code == 0 and "durable identity" in out
