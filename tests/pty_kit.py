"""A launcher run on a pseudo-terminal, for the window suites."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import termios
import threading
import time

IDENTITY = "pilot@proj-a"
ENTER = b"\x1b[?1049h"
LEAVE = b"\x1b[?1049l"
SHOW = b"\x1b[?25h"
RESTORE = ENTER + LEAVE + SHOW


class PtyLauncher:
    """One `moxi-comms-codex --window` process on its own pty, as a terminal would run it."""

    def __init__(self, argv, env):
        master, slave = os.openpty()
        self.fd = master
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "moxi_comms.adapters.codex", *argv], env=env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.started = time.monotonic()
        self.out = bytearray()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self.out.extend(chunk)

    @property
    def output(self) -> bytes:
        with self._lock:
            return bytes(self.out)

    def text(self) -> str:
        return self.output.decode(errors="replace")

    def wait(self, timeout: float = 20.0):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def signal(self, sig) -> None:
        self.proc.send_signal(sig)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._reader.join(timeout=2)
        try:
            os.close(self.fd)
        except OSError:
            pass


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except OSError:
        return False
