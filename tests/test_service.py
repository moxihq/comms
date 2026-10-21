"""`moxi comms service install|uninstall|status`.

Every test runs against a temp HOME and a fake `systemctl` first on PATH, which records
its argv and answers like systemd. Nothing here may touch the real
`~/.config/systemd/user` or the real user manager; the first test checks that.
"""

from __future__ import annotations

import io
import os
import sysconfig
from pathlib import Path

import pytest

from moxi_comms import cli, release, service

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
exit {code}
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A temp HOME, a fake systemctl, and the rig's standalone roots."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"

    def fake(code=0):
        script = bin_dir / "systemctl"
        script.write_text(FAKE_SYSTEMCTL.format(log=log, code=code))
        script.chmod(0o755)

    fake()
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env.update(HOME=str(home), PATH=str(bin_dir), USER="tester")

    class Box:
        pass

    b = Box()
    b.home, b.env, b.log, b.fake = home, env, log, fake
    b.unit = home / ".config" / "systemd" / "user" / "moxi-comms-daemon.service"
    b.calls = lambda: log.read_text().splitlines() if log.exists() else []
    return b


def comms(box, *argv):
    out = io.StringIO()
    ctx = cli.Context(gateway=object(), env=box.env, out=out,
                      db_path=Path("/nonexistent/db"))
    code = cli.main(["service", *argv], ctx=ctx)
    return code, out.getvalue()


def snapshot(path: Path):
    return sorted((str(p), p.stat().st_mtime_ns) for p in path.rglob("*")) \
        if path.exists() else None


def test_install_writes_the_unit_reloads_and_enables_it(box):
    real_units = Path(os.path.expanduser("~")) / ".config" / "systemd" / "user"
    before = snapshot(real_units)
    code, out = comms(box, "install")
    assert code == 0, out
    assert box.unit.is_file()
    text = box.unit.read_text()
    daemon = Path(sysconfig.get_path("scripts")) / "moxi-comms-daemon"
    assert f"ExecStart={daemon.absolute()}\n" in text
    assert Path(text.split("ExecStart=")[1].split("\n")[0]).is_absolute()
    assert "Description=Moxi comms: the comms delivery daemon" in text
    assert "WantedBy=default.target" in text
    assert box.calls() == ["--user daemon-reload",
                           f"--user enable --now {release.UNIT}"]
    assert snapshot(real_units) == before, "the real unit directory is never touched"


def test_install_prints_linger_but_never_runs_it(box):
    _, out = comms(box, "install")
    assert "loginctl enable-linger tester" in out
    assert "machine-wide login policy" in out
    assert not any("linger" in call for call in box.calls())


def test_install_honours_xdg_config_home(box, tmp_path):
    box.env["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    assert comms(box, "install")[0] == 0
    assert (tmp_path / "cfg" / "systemd" / "user" / release.UNIT).is_file()
    assert not box.unit.exists()


def test_install_names_the_shell_variables_the_service_will_not_see(box):
    _, out = comms(box, "install")
    assert "NOTE: set in this shell and not seen by the service" in out
    assert "MOXI_COMMS_HOME" in out and "MOXI_COMMS_DEVICE" in out
    assert "MOXI_COMMS_PROFILE" not in out


def test_install_never_takes_a_daemon_from_path(box, monkeypatch, tmp_path):
    """The unit runs this environment's daemon (`moxi.paths.script`): one of the same
    name first on PATH, another program's, is never what it names."""
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(tmp_path / "none"))
    daemon = Path(box.env["PATH"]) / "moxi-comms-daemon"
    daemon.write_text("#!/bin/sh\n")
    daemon.chmod(0o755)
    code, out = comms(box, "install")
    assert code == service.EXIT_REFUSED
    assert "no moxi-comms-daemon in this Python environment" in out
    assert not box.unit.exists() and box.calls() == []


def test_a_failing_systemctl_is_reported_with_its_words(box):
    box.fake(code=1)
    code, out = comms(box, "install")
    assert code == service.EXIT_FAILED
    assert "systemctl --user daemon-reload failed (exit 1)" in out


def test_install_is_allowed_on_every_box(box, monkeypatch):
    """No profile refuses it: a box that sets a config file installs like any other."""
    config_home = Path(box.env["MOXI_COMMS_HOME"]) / "config" / "comms"
    config_home.mkdir(parents=True)
    (config_home / "config.toml").write_text('backend = "local"\n')
    code, out = comms(box, "install")
    assert code == 0, out
    assert box.unit.is_file()



def test_install_refuses_off_linux(box):
    out = []
    code = service.run("install", env=box.env, say=out.append, platform="darwin")
    assert code == service.EXIT_REFUSED and "launchd" in out[0]
    assert box.calls() == []


def test_install_refuses_without_systemctl(box):
    os.remove(Path(box.env["PATH"]) / "systemctl")
    code, out = comms(box, "install")
    assert code == service.EXIT_REFUSED and "no systemctl" in out
    assert not box.unit.exists()


def test_uninstall_disables_removes_and_reloads(box):
    comms(box, "install")
    box.log.unlink()
    code, out = comms(box, "uninstall")
    assert code == 0, out
    assert not box.unit.exists()
    assert box.calls() == [f"--user disable --now {release.UNIT}",
                           "--user daemon-reload"]


def test_uninstall_with_nothing_installed_is_a_quiet_exit_0(box):
    code, out = comms(box, "uninstall")
    assert code == 0 and "not installed" in out and box.calls() == []


def test_status_reads_the_file_and_asks_systemd(box):
    code, out = comms(box, "status")
    assert code == 0 and "not installed" in out
    comms(box, "install")
    box.log.unlink()
    code, out = comms(box, "status")
    assert code == 0
    assert f"unit        {box.unit}" in out
    assert "is-enabled  enabled" in out and "is-active   active" in out
    assert box.calls() == [f"--user is-enabled {release.UNIT}",
                           f"--user is-active {release.UNIT}"]
