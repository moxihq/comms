"""`moxi comms service install|uninstall|status`: the daemon as a systemd user service.

Linux and systemd first; a macOS launchd agent comes later.

`install` writes `~/.config/systemd/user/moxi-comms-daemon.service` (under
`$XDG_CONFIG_HOME` when that is set, which is where systemd looks) with the absolute
path of the `moxi-comms-daemon` in this Python environment (`moxi.paths.script`, never a
PATH lookup, where another program's daemon of a like name may stand first), then runs
`systemctl --user daemon-reload` and `enable --now`. It never enables lingering:
lingering changes the login policy for the whole machine, so `install` prints the
`loginctl` command and what it does, and leaves the choice with the person.

`install` is allowed on every box: there is one comms, and a box is a box with a config
file.


`uninstall` disables and stops the unit, removes the file and reloads. `status` reads:
the file, the `ExecStart=` it holds, and what `systemctl --user` says about the unit.

`moxi-comms-daemon` in the foreground stays the baseline. The service runs with the
user manager's environment, not the installing shell's, so a `MOXI_COMMS_*` variable that
moves comms' files in this shell is named at install: the service will not see it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from moxi import paths as moxi_paths

from . import release

ACTIONS = ("install", "uninstall", "status")

#: Exit codes, the CLI's: 0 done, 1 systemctl failed, 2 refused before anything ran.
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

#: The daemon's console script, which the unit runs.
DAEMON_SCRIPT = "moxi-comms-daemon"

#: Variables that move comms' files or override its settings in the installing shell.
SHELL_ONLY = ("MOXI_COMMS_HOME", "MOXI_COMMS_DATA_HOME", "MOXI_COMMS_STATE_HOME",
              "MOXI_COMMS_CACHE_HOME", "MOXI_COMMS_CONFIG_HOME", "MOXI_COMMS_RUNTIME_DIR",
              "MOXI_COMMS_DEVICE", "MOXI_COMMS_GATEWAY", "MOXI_COMMS_LOCAL_RECORD",
              "MOXI_COMMS_LOCAL_RETAIN_DAYS", "MOXI_COMMS_TOKEN_STORE")

LINGER = (
    "The daemon now starts when you log in and stops when your last session ends. To "
    "keep it running while you are logged out (a headless box, agents left working "
    "overnight), enable lingering for your user:\n"
    "    loginctl enable-linger {user}\n"
    "Lingering starts your user services at boot and keeps them after logout. It is a "
    "machine-wide login policy for your account, so comms does not turn it on for you.")


def unit_dir(env) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or
                                                      os.path.expanduser("~"), ".config")
    return Path(base) / "systemd" / "user"


def unit_path(env) -> Path:
    return unit_dir(env) / release.UNIT


def daemon_binary(env=None) -> Path | None:
    """The `moxi-comms-daemon` installed in this Python environment, by
    `moxi.paths.script`: the scripts directory a `uv tool`, `pipx` or venv install keeps
    it in. Never looked up on PATH. None when this environment has none."""
    try:
        found = moxi_paths.script(DAEMON_SCRIPT)
    except FileNotFoundError:
        return None
    return found.absolute() if os.access(found, os.X_OK) else None


def render_unit(daemon: Path) -> str:
    return (
        "[Unit]\n"
        f"Description={release.DISPLAY_NAME}: the comms delivery daemon\n"
        f"Documentation={release.REPO_URL}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={daemon}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n")


class Service:
    """One run of the verb. `run` is `subprocess.run`'s shape, injectable for tests."""

    def __init__(self, env=None, say=print, run=None, platform=None):
        self.env = os.environ if env is None else env
        self.say = say
        self.run = run or subprocess.run
        self.platform = platform or sys.platform

    def systemctl(self, *args) -> subprocess.CompletedProcess:
        binary = shutil.which("systemctl", path=self.env.get("PATH"))
        return self.run([binary, "--user", *args], capture_output=True, text=True,
                        env=dict(self.env))

    def _refuse_unless_systemd(self) -> int | None:
        if not self.platform.startswith("linux"):
            self.say(f"moxi comms service: this is {self.platform}. The service is a "
                     "systemd user unit on Linux; macOS's launchd agent comes in a later "
                     "release. Run moxi-comms-daemon in the foreground meanwhile.")
            return EXIT_REFUSED
        if shutil.which("systemctl", path=self.env.get("PATH")) is None:
            self.say("moxi comms service: no systemctl on PATH, so there is no systemd "
                     "user manager to hand the daemon to. Run moxi-comms-daemon in the "
                     "foreground.")
            return EXIT_REFUSED
        return None

    def _failed(self, step: str, proc) -> int:
        detail = (proc.stderr or proc.stdout or "").strip()
        self.say(f"moxi comms service: systemctl --user {step} failed "
                 f"(exit {proc.returncode})" + (f": {detail}" if detail else ""))
        return EXIT_FAILED

    # ------------------------------------------------------------------ install
    def install(self) -> int:
        refused = self._refuse_unless_systemd()
        if refused is not None:
            return refused
        daemon = daemon_binary(self.env)
        if daemon is None:
            self.say(f"moxi comms service: no {DAEMON_SCRIPT} in this Python environment. "
                     f"Reinstall the package (uv tool install {release.DISTRIBUTION}).")
            return EXIT_REFUSED

        path = unit_path(self.env)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(render_unit(daemon))
        os.chmod(path, 0o644)
        self.say(f"{'rewrote' if existed else 'wrote'} {path}")
        self.say(f"  ExecStart={daemon}")
        for step in (("daemon-reload",), ("enable", "--now", release.UNIT)):
            proc = self.systemctl(*step)
            if proc.returncode != 0:
                return self._failed(" ".join(step), proc)
            self.say(f"systemctl --user {' '.join(step)}: done")
        moved = [v for v in SHELL_ONLY if self.env.get(v)]
        if moved:
            self.say("NOTE: set in this shell and not seen by the service, which runs "
                     f"with your user manager's environment: {', '.join(moved)}. Put "
                     "them in the unit with `systemctl --user edit "
                     f"{release.UNIT}` if the daemon needs them.")
        user = self.env.get("USER") or self.env.get("LOGNAME") or "$USER"
        self.say(LINGER.format(user=user))
        return EXIT_OK

    # ------------------------------------------------------------------ uninstall
    def uninstall(self) -> int:
        refused = self._refuse_unless_systemd()
        if refused is not None:
            return refused
        path = unit_path(self.env)
        if not path.exists():
            self.say(f"moxi comms service: not installed ({path} does not exist)")
            return EXIT_OK
        proc = self.systemctl("disable", "--now", release.UNIT)
        if proc.returncode != 0:
            return self._failed(f"disable --now {release.UNIT}", proc)
        self.say(f"systemctl --user disable --now {release.UNIT}: done")
        path.unlink()
        self.say(f"removed {path}")
        proc = self.systemctl("daemon-reload")
        if proc.returncode != 0:
            return self._failed("daemon-reload", proc)
        self.say("systemctl --user daemon-reload: done")
        return EXIT_OK

    # ------------------------------------------------------------------ status
    def status(self) -> int:
        """Read-only. Exit 0 whatever it finds; the lines say what that is."""
        path = unit_path(self.env)
        if not path.exists():
            self.say(f"unit        not installed ({path} does not exist)")
        else:
            exec_start = next((line.partition("=")[2] for line in
                               path.read_text().splitlines()
                               if line.startswith("ExecStart=")), "(none)")
            self.say(f"unit        {path}")
            self.say(f"  exec      {exec_start}")
        refused = self._refuse_unless_systemd()
        if refused is not None:
            return EXIT_OK
        for question in ("is-enabled", "is-active"):
            proc = self.systemctl(question, release.UNIT)
            answer = (proc.stdout or proc.stderr or "").strip() or f"exit {proc.returncode}"
            self.say(f"{question:<10}  {answer}")
        return EXIT_OK


def run(action: str, *, env=None, say=print, run=None, platform=None) -> int:
    svc = Service(env=env, say=say, run=run, platform=platform)
    return getattr(svc, action)()


__all__ = ["ACTIONS", "Service", "run", "render_unit", "unit_path", "unit_dir",
           "daemon_binary", "LINGER", "SHELL_ONLY"]
