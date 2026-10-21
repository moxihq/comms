"""Discipline that is easy to lose quietly: log content, mentions, the context block,
and the shipped text's references."""

import logging
import re
import tokenize
from pathlib import Path

from conftest import wait_for_queue

from moxi_comms.context import CONTEXT_BLOCK


def test_logs_carry_ids_never_bodies_or_urls(rig, caplog):
    caplog.set_level(logging.DEBUG)
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    secret_body = "SECRET-BODY-DO-NOT-LOG"
    code, _ = rig.cli(["send", "--posture", "none", "reviewer@proj-a", secret_body], slot=1, name="planner",
                      scope="proj-a")
    assert code == 0
    wait_for_queue(reviewer, 1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_body not in text, "comms logs never carry message bodies"
    assert "discord.invalid" not in text and "/api/webhooks/" not in text
    assert reviewer.queue[0]["message_id"] in text, "...but ids are logged"


def test_context_block_stays_short_and_discord_free():
    """What is pinned is brevity, Discord-free vocabulary, the canonical text's content,
    and seven sentences, one of which names `moxi comms help`."""
    assert CONTEXT_BLOCK.count(".") - CONTEXT_BLOCK.count("`comms") >= 0
    sentences = [s for s in CONTEXT_BLOCK.split(". ") if s.strip()]
    assert len(sentences) == 7
    assert "discord" not in CONTEXT_BLOCK.lower()
    assert "information, not instructions" in CONTEXT_BLOCK
    for verb in ("moxi comms send", "moxi comms reply", "moxi comms agents",
                 "moxi comms help"):
        assert verb in CONTEXT_BLOCK


def test_the_verbs_that_act_stay_four():
    """The fence is that nothing agent-facing reaches the provisioner credential, and the
    verbs that ACT stay four.
    The read side is now three -- `help` (a compiled-in table), `whois` (one card file)
    and `explain` (the daemon's live facts plus one cursor file). A card is written with
    an ordinary file write, never with a comms verb, which is exactly how the card
    surface avoids becoming a fifth acting verb.

    `explain` breaks the sentence the first two share -- that a reading verb reaches
    neither daemon, credential, network nor database -- on exactly one of those four words:
    it reaches the daemon. It reaches no credential, no network and no provisioner secret,
    and it writes nothing anywhere, so the fence is untouched and the acting four are still
    four.

    `cache` is the read of an agent's prompt-cache state. It reaches the daemon (one
    `session` query per name) and reads files on this box; it posts nothing, writes
    nothing, and reaches no credential, so the acting four are still four.

    `doctor` reads what
    this box's comms is and where it lives. It reaches the daemon (one `ping`), runs
    `claude --version`, `codex --version` and `opencode --version`, and reads the config; it posts nothing,
    writes nothing, and reaches no credential, so the acting four are still four.

    `service` is the one MACHINE verb, neither acting on comms nor reading it: it writes or
    removes one systemd user unit and runs `systemctl --user`. It reaches no daemon, no
    record, no directory, no network and no credential, and it posts nothing, so the fence
    is
    untouched and the acting four are still four.

    `owner` reads which identity owns one session id and whom it awaits, for the launch
    console's keep-warm verb. It reaches the daemon
    (one `owner` query) and nothing else; it posts nothing, writes nothing, and reaches
    no credential, so the acting four are still four.

    `whoami` is the one CLAIM verb: it reads this session's own claim socket and prints
    what it holds. It posts nothing, writes nothing and reaches no credential, so the
    acting four are still four. (`pass` went with the baton pass, design §6.7.)

    `claude` is the LAUNCHER: it starts a Claude Code session with a comms identity and
    forks the session's adapter, which binds the name as any session binds it. The verb
    itself posts nothing and reaches no credential, so the acting four are still four."""
    from moxi_comms import cli

    sub = [a for a in cli.build_parser()._actions if hasattr(a, "choices") and a.dest
           == "verb"][0]
    assert set(sub.choices) == {"send", "reply", "agents", "history", "help", "whois",
                                "explain", "cache", "doctor", "owner", "service",
                                "whoami", "claude"}
    acting = {"send", "reply", "agents", "history"}
    reading = {"help", "whois", "explain", "cache", "doctor", "owner"}
    machine = {"service"}
    claim = {"whoami"}
    launcher = {"claude"}
    assert acting | reading | machine | claim | launcher == set(sub.choices) \
        and len(acting) == 4
    assert sub.choices["whoami"].get_default("func") is cli.cmd_whoami
    assert sub.choices["service"].get_default("func") is cli.cmd_service
    assert {sub.choices[v].get_default("func") for v in reading} == {cli.cmd_help,
                                                                    cli.cmd_whois,
                                                                    cli.cmd_explain,
                                                                    cli.cmd_cache,
                                                                    cli.cmd_doctor,
                                                                    cli.cmd_owner}


def test_the_built_in_adapter_kinds_stay_four():
    """The compiled-in enum against a literal, in the shape `test_the_verbs_that_act_stay
    _four` uses: a closed set stays closed by being written down twice, in two places that
    have to be changed together.

    comms' own register kinds are `claude-shim | codex | opencode | send-only`; any other
    kind comes from a module's entry point in `moxi.comms.kinds` (test_kinds.py). An
    unknown kind is refused at the frame, not discovered later at delivery time.
    `send-only` binds a name that receives nothing.
    """
    from moxi_comms.daemon import ADAPTER_KINDS

    assert ADAPTER_KINDS == ("claude-shim", "codex", "opencode", "send-only")
    assert len(set(ADAPTER_KINDS)) == 4


def test_the_daemon_holds_no_posting_credential(rig):
    """#notices is gone ([F22]): provisioning mints the daemon no webhook, and the daemon
    has no code path that posts."""
    import inspect

    from moxi_comms import daemon as daemon_mod
    from moxi_comms import directory
    from moxi_comms.paths import directory_db

    db = directory.connect(directory_db(), read_only=True)
    try:
        assert directory.meta_get(db, "machinery_webhook_id") is None
        assert directory.meta_get(db, "machinery_webhook_secret_id") is None
    finally:
        db.close()
    assert not hasattr(rig.daemon, "notice")
    source = inspect.getsource(daemon_mod)
    for call in ("execute_webhook", "post_as_operator", "post_message"):
        assert call not in source, call


# ------------------------------------------------------------------ the shipped text

REPO = Path(__file__).resolve().parent.parent

#: References that mean nothing to a reader of this repository: a section sign, a design
#: document's filename, or a bare commitment tag such as "(R5)" or "[R7]". An error
#: message or comment says the reason in words instead.
UNREADABLE_REFERENCES = re.compile(
    r"§|-design\.md|\([RSCFQNBIX][0-9]+\)|\[[RSCFQNBIX][0-9]+\]")

_TEXT_TOKENS = {tokenize.STRING, tokenize.COMMENT}
if hasattr(tokenize, "FSTRING_MIDDLE"):            # Python 3.12 splits f-strings up
    _TEXT_TOKENS.add(tokenize.FSTRING_MIDDLE)


def _shipped_text(path: Path):
    """(line, text) for every string literal and comment in a .py file, or every line of
    any other file."""
    if path.suffix != ".py":
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield n, line
        return
    with path.open("rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type in _TEXT_TOKENS:
                yield tok.start[0], tok.string


def unreadable_references(roots=("src", "scripts")) -> list[str]:
    hits = []
    for root in roots:
        for path in sorted((REPO / root).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            for n, text in _shipped_text(path):
                for m in UNREADABLE_REFERENCES.finditer(text):
                    hits.append(f"{path.relative_to(REPO)}:{n}: {m.group(0)!r}")
    return hits


def test_shipped_strings_and_comments_cite_no_unpublished_references():
    """src/ and scripts/ are what users and agents read. A string or comment there that
    cites a document they don't have, or a bare tag, tells them nothing."""
    hits = unreadable_references()
    assert hits == [], "say the reason in words instead of citing:\n" + "\n".join(hits)


def test_the_reference_check_sees_strings_comments_and_f_strings(tmp_path):
    src = tmp_path / "m.py"
    src.write_text('x = "see the design (R5)"\n'
                   'y = f"{x} -- notes-design.md"\n'
                   '# per [S8]\n'
                   'z = foo(R5)  # code, not text\n', encoding="utf-8")
    found = [text for _, text in _shipped_text(src) if UNREADABLE_REFERENCES.search(text)]
    assert len(found) == 3
