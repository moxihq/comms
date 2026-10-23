"""What the scripts under `scripts/` promise about the box they run on."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def daemon_starting_scripts():
    """Every script that spawns a `moxi-comms-daemon` process of its own."""
    found = []
    for script in sorted(SCRIPTS.glob("*.py")):
        text = script.read_text()
        if re.search(r'BIN\s*/\s*"moxi-comms-daemon"', text):
            found.append((script.name, text))
    return found


def test_the_daemon_starting_scripts_are_found():
    names = {name for name, _ in daemon_starting_scripts()}
    assert {"codex-smoke.py", "smoke.py"} <= names


def test_no_script_passes_a_profile():
    """comms has no profile, so no script passes `--profile`."""
    for script in sorted(SCRIPTS.glob("*.py")):
        text = script.read_text()
        assert "--profile" not in text and "MOXI_COMMS_PROFILE" not in text, script.name


def test_every_script_runs_moxi_comms_not_a_bare_comms():
    """There is no `comms` script: a script runs this environment's `moxi` with
    the module's name."""
    for script in sorted(SCRIPTS.glob("*.py")):
        assert not re.search(r'BIN\s*/\s*"comms"', script.read_text()), script.name



def load_codex_smoke():
    import importlib.util

    spec = importlib.util.spec_from_file_location("codex_smoke", SCRIPTS / "codex-smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_live_codex_smoke_refuses_a_second_codex_without_a_home_of_its_own(
        tmp_path, monkeypatch):
    """A codex binary other than the one on PATH runs only with its own CODEX_HOME."""
    smoke = load_codex_smoke()
    on_path = tmp_path / "global" / "codex"
    other = tmp_path / "other" / "codex"
    for binary in (on_path, other):
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert smoke.own_home_refusal(str(on_path)) is None
    assert "CODEX_HOME" in smoke.own_home_refusal(str(other))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    assert "CODEX_HOME" in smoke.own_home_refusal(str(other))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "own-home"))
    assert smoke.own_home_refusal(str(other)) is None
    assert smoke.codex_home() == tmp_path / "own-home"
