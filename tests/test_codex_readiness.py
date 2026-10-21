"""Layer one and launch readiness at comms' side of turns' Codex host."""

from __future__ import annotations

from conftest import clock_reads_warm

from moxi_comms import codex_threads
from moxi_comms.adapters import codex as codex_mod
from moxi_comms.adapters.codex import CodexAdapter, layer_one_hash, layer_one_text, own_comms
from moxi_comms.adapters.codex_roster import CodexAgent
from moxi_comms.context import CONTEXT_BLOCK

from fake_turns import FakeSession, launcher


IDENTITY = "pilot@proj-a"
COMMS = "/opt/moxi/bin/moxi comms"


def row(tmp_path):
    return CodexAgent(qualified=IDENTITY, name="pilot", scope="proj-a", slot=3,
                      cwd=str(tmp_path), thread_file=codex_threads.path(IDENTITY))


def test_layer_one_is_the_block_then_the_comms_path_then_the_roster():
    text = layer_one_text({"bindings": [{"qualified": IDENTITY, "tier": "chosen",
                                         "slot": 3, "adapter": "codex"}],
                           "pending": {}}, COMMS)
    assert text.startswith(CONTEXT_BLOCK + "\n\n")
    assert f"Run every comms verb with {COMMS}" in text
    assert "Comms roster as of session start" in text and IDENTITY in text
    offline = layer_one_text(None, COMMS)
    assert offline.startswith(CONTEXT_BLOCK) and "offline at session start" in offline


def test_a_new_thread_is_born_with_layer_one_as_developer_instructions(
        rig, tmp_path, monkeypatch):
    clock_reads_warm(monkeypatch, "codex")
    fake = FakeSession()
    launch = launcher(fake)
    adapter = CodexAdapter(row(tmp_path), launch=launch, comms_path=COMMS,
                           autoreconnect=False, clock=rig.clock)
    try:
        adapter.start()
        call = launch.calls[0]
        assert call["instructions"].startswith(CONTEXT_BLOCK)
        assert COMMS in call["instructions"] and IDENTITY in call["instructions"]
        record = codex_threads.read_meta(IDENTITY)
        assert record["thread_id"] == fake.session_id
        assert record["layer_one"] == layer_one_hash(COMMS)
    finally:
        adapter.stop()


def test_layer_one_can_be_omitted_without_writing_its_hash(rig, tmp_path, monkeypatch):
    clock_reads_warm(monkeypatch, "codex")
    fake = FakeSession()
    launch = launcher(fake)
    adapter = CodexAdapter(row(tmp_path), launch=launch, layer_one=False,
                           autoreconnect=False, clock=rig.clock)
    try:
        adapter.start()
        assert launch.calls[0]["instructions"] is None
        assert "layer_one" not in codex_threads.read_meta(IDENTITY)
    finally:
        adapter.stop()


def test_layer_one_comes_from_the_shared_constant_not_a_copy():
    source = open(codex_mod.__file__, encoding="utf-8").read()
    assert "You can message other agents" not in source


def test_comms_is_this_environments_moxi_script_with_comms(tmp_path):
    seen = []

    def find(name):
        seen.append(name)
        return tmp_path / "venv" / "bin" / name

    assert own_comms(find) == f"{tmp_path / 'venv/bin/moxi'} comms"
    assert seen == ["moxi"]


def test_a_script_path_with_a_space_is_quoted_for_the_shell(tmp_path):
    assert own_comms(lambda name: tmp_path / "my env" / name) == \
        f"'{tmp_path / 'my env/moxi'}' comms"


def test_an_environment_with_no_moxi_script_finds_none():
    def missing(name):
        raise FileNotFoundError(name)

    assert own_comms(missing) is None


def test_the_default_reads_moxi_paths_script():
    from moxi import paths as moxi_paths

    try:
        expected = f"{moxi_paths.script('moxi')} comms"
    except FileNotFoundError:
        expected = None
    assert own_comms() == expected
