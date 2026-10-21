"""The terminal a launcher's window borrows, and the children a launcher spawns, shared by
every launcher that opens a window.

A window is a CLI's own full-screen interface, run as the launcher's child in the
launcher's terminal. The launcher gives the window the terminal's foreground, takes it
back when the window ends, and puts the screen back the way a person expects it.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import signal
import sys
import termios

log = logging.getLogger("comms.terminal")

#: Written after every window end: enter the alternate screen, leave it, show the
#: cursor. A window ended by a signal gives the terminal mode back but leaves the
#: terminal on the alternate screen; a window that quit cleanly has already left it,
#: and a bare leave would then move the cursor back over its last lines. Entering
#: first makes the leave land on the alternate screen in both cases.
ENTER_ALT_SCREEN = b"\x1b[?1049h"
LEAVE_ALT_SCREEN = b"\x1b[?1049l"
SHOW_CURSOR = b"\x1b[?25h"
RESTORE_SCREEN = ENTER_ALT_SCREEN + LEAVE_ALT_SCREEN + SHOW_CURSOR

#: The signals a window launcher ignores in every phase, so Ctrl-Z never suspends it
#: and it can take the terminal's foreground back from a background group.
JOB_CONTROL_SIGNALS = (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU)


#: How long a child (a CLI server, a window) gets after SIGTERM before SIGKILL.
TERM_GRACE = 5.0
PR_SET_PDEATHSIG = 1                  # linux/prctl.h


def die_with_parent(parent_pid: int):
    """preexec_fn: ask the kernel to SIGTERM the child when the spawning thread exits,
    and close the race where it already did before we asked. Linux only, by name;
    elsewhere it is None and the child outlives a launcher that dies without stopping
    it."""
    if not sys.platform.startswith("linux"):
        return None

    def _pre():
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
        if os.getppid() != parent_pid:
            os._exit(0)
    return _pre


def terminal_fd():
    """The launcher's controlling terminal, or None when it has none."""
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            if os.isatty(fd):
                return fd
    with contextlib.suppress(OSError):
        return os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    return None


def in_terminal_foreground(fd) -> bool:
    """True when the launcher's process group is the terminal's foreground group."""
    if fd is None:
        return False
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False


class Terminal:
    """The terminal the window borrows: its foreground group and its mode."""

    #: Where a failure to take the terminal back is said; a launcher sets its own.
    log = log

    def __init__(self, fd):
        self.fd = fd
        self.saved = None
        self.hung_up = False

    def save(self) -> None:
        try:
            self.saved = termios.tcgetattr(self.fd)
        except termios.error:
            self.saved = None

    def give(self, pgid: int) -> None:
        with contextlib.suppress(OSError):
            os.tcsetpgrp(self.fd, pgid)

    def take_back(self) -> None:
        """Foreground and mode back to the launcher, then the main screen and a visible
        cursor. Skipped on a terminal that has hung up."""
        if self.hung_up:
            return
        try:
            os.tcsetpgrp(self.fd, os.getpgrp())
            if self.saved is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            os.write(self.fd, RESTORE_SCREEN)
        except (OSError, termios.error) as e:
            self.log.warning("could not take the terminal back: %s", e)


__all__ = ["ENTER_ALT_SCREEN", "LEAVE_ALT_SCREEN", "SHOW_CURSOR", "RESTORE_SCREEN",
           "JOB_CONTROL_SIGNALS", "PR_SET_PDEATHSIG", "TERM_GRACE", "Terminal",
           "die_with_parent", "in_terminal_foreground", "terminal_fd"]
