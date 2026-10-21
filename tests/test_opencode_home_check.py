"""`scripts/opencode-home-check.py`: it reads what an OpenCode session would load from a
HOME, and it must never remove a file of the person's own OpenCode. These pin its
cleanup and its refusal; the reads themselves need a real `opencode`."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "opencode-home-check.py"


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("opencode_home_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def touch(path: Path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_only_new_state_of_opencodes_own_written_during_the_run_is_removed(check,
                                                                          tmp_path):
    home = tmp_path / "home"
    share = home / ".local/share/opencode"
    kept_db = touch(share / "storage" / "his-session.json", "his")
    early = touch(share / "log" / "early.log")         # new, but written before the run
    old_ns = time.time_ns() - 10_000_000_000
    os.utime(early, ns=(old_ns, old_ns))
    before = check.snapshot(check.opencode_dirs(home))
    before.pop(str(early))                               # it is new to the check...
    started = time.time_ns()
    touch(share / "opencode.db")
    touch(share / "opencode.db-wal")
    touch(share / "log" / "2026-10-04T000000.log")
    touch(home / ".local/state/opencode/locks/lock-1")
    touch(home / ".opencode/.gitignore")
    stranger = touch(share / "storage" / "new-of-his.json")    # new, not OpenCode's own
    kept_db.write_text("his, changed")
    after = check.snapshot(check.opencode_dirs(home))
    remove, new_left, changed = check.plan_cleanup(before, after, started, home)
    names = {str(Path(p).relative_to(home)) for p in remove}
    assert names == {".local/share/opencode/opencode.db",
                     ".local/share/opencode/opencode.db-wal",
                     ".local/share/opencode/log/2026-10-04T000000.log",
                     ".local/state/opencode/locks", ".local/state/opencode/locks/lock-1",
                     ".opencode/.gitignore"}
    assert str(stranger) in new_left
    assert str(early) in new_left, "...but it was written before the run, so it stays"
    assert changed == [str(kept_db)]
    removed = check.remove_paths(remove, after)
    assert set(removed) == set(remove)
    assert kept_db.exists() and stranger.exists() and early.exists()


def test_it_refuses_while_another_opencode_runs(check, tmp_path, capsys):
    fake = tmp_path / "opencode"
    shutil.copy(shutil.which("sleep"), fake)
    proc = subprocess.Popen([str(fake), "30"], stdin=subprocess.DEVNULL)
    try:
        time_limit = time.monotonic() + 5
        while time.monotonic() < time_limit and not any(
                pid == proc.pid for pid, _ in check.other_opencode()):
            time.sleep(0.05)
        assert any(pid == proc.pid for pid, _ in check.other_opencode())
        assert check.main(["--home", str(tmp_path / "home"), "--opencode", str(fake)]) == 2
        assert "another opencode process is running" in capsys.readouterr().out
    finally:
        proc.kill()
        proc.wait()


def test_an_opencode_that_starts_during_the_check_means_nothing_is_removed(
        check, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    touch(home / ".claude" / "CLAUDE.md", "a line of instructions long enough to find\n")
    calls = iter([[], [(4242, "opencode tui")]])
    monkeypatch.setattr(check, "other_opencode", lambda exclude=(): next(calls))

    def read_once(binary, env, work, capture):
        touch(home / ".local/share/opencode/opencode.db")
        return {"skills": [], "sent": ""}

    monkeypatch.setattr(check, "read_once", read_once)
    check.main(["--home", str(home), "--opencode", "/bin/true"])
    out = capsys.readouterr().out
    assert "nothing was removed" in out
    assert (home / ".local/share/opencode/opencode.db").exists()
