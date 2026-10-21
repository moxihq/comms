"""A launcher's window: the harness CLI the person sits in, run as the launcher's child.

`moxi-comms-opencode --window` runs one (`opencode_window.OpenCodeWindow`). The Codex
window is turns' now (its Codex host opens it); this is the lifecycle comms keeps for the
harness whose delivery is still its own.

One rule governs the window's lifecycle. **The launcher alone owns the window, and it
knows why every window ends.** Before it ends a window for a reason of its own (the
server connection is lost, or the launcher is stopping) it records that cause on the
window. A window that exits with no cause recorded, while the generation it was opened
on still answers, was closed by the person, and the launcher then stops, so the identity
goes offline. A window opens only on the current generation, once that generation is
ready. Every wait has a deadline, and every give-up exits loudly with its reason.

Everything here runs on the main thread. The kernel's parent-death signal follows the
thread that spawned a child, so the thread that spawns windows is the one that lives as
long as the launcher; and with one thread both recording causes and sending signals,
the cause is always recorded before the signal is sent.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from . import terminal
from .terminal import TERM_GRACE
from .terminal import die_with_parent as _die_with_parent
from .terminal import (ENTER_ALT_SCREEN, JOB_CONTROL_SIGNALS, LEAVE_ALT_SCREEN,
                       RESTORE_SCREEN, SHOW_CURSOR, in_terminal_foreground, terminal_fd)

log = logging.getLogger("comms.window")

#: How long a wait for a ready generation lasts before the launcher gives up. A priming
#: turn takes a few seconds; the deadline covers a slow first start with room to spare.
READY_WAIT = 180.0
#: How long the generation check waits for an answer before it reads the generation as
#: lost.
GENERATION_CHECK_WAIT = 5.0
#: The main thread's poll interval while a window runs and while it waits.
POLL = 0.1
#: Exit status when the launcher gives up waiting for a window it can show.
EXIT_GAVE_UP = 3

#: The causes the launcher records on a window it ends.
CAUSE_LOST = "generation lost"
CAUSE_SHUTDOWN = "shutdown"
CAUSE_HANGUP = "hangup"


class GaveUp(Exception):
    """No window could be shown before the deadline."""


class Terminal(terminal.Terminal):
    """The terminal a window borrows (`terminal.Terminal`), saying its trouble in the
    window's log."""

    log = log


class Window:
    """One window: its process, the generation it was opened on, and the cause the
    launcher recorded before ending it (None until then)."""

    def __init__(self, proc: subprocess.Popen, generation):
        self.proc = proc
        self.pgid = proc.pid
        self.generation = generation
        self.cause: str | None = None
        self.status: int | None = None        # the raw wait status, once it exited

    @property
    def exited(self) -> bool:
        return self.status is not None

    @property
    def exit_code(self) -> int | None:
        return None if self.status is None else os.waitstatus_to_exitcode(self.status)


class WindowSession:
    """The main thread's loop for `--window`: wait for a ready generation, show a window
    on it, and act on how the window ended.

    A launcher for another CLI subclasses it with its own window command (`argv`) and
    its own wording (the class attributes below); the lifecycle is the same."""

    #: What the launcher says to the terminal, each formatted with `prog` (the
    #: launcher's name), `identity`, `wait`, `status` and `log_path`.
    WAITING = ("{prog}: waiting for {identity}'s session to be ready (up to {wait:.0f}s; "
               "Ctrl-C stops)")
    LOST = ("{prog}: the server was lost; the window closed and reopens when the server "
            "is back")
    EXITED_LOST = ("{prog}: the window exited as its server went; reopening it when the "
                   "server is back")
    EXIT_STATUS = "{prog}: the window exited with status {status}; the log is {log_path}"
    OFFLINE = ("{prog}: {identity} is offline. `{prog} {identity} --window` launches it "
               "again, on a new session.")
    PROG = "moxi-comms"

    def __init__(self, adapter, *, binary: str, terminal: Terminal, log_path,
                 stopping: threading.Event, hangup: threading.Event,
                 ready_wait: float = READY_WAIT, say=None, env=None):
        self.adapter = adapter
        self.agent = adapter.agent
        self.binary = binary
        self.terminal = terminal
        self.log_path = log_path
        self.stopping = stopping
        self.hangup = hangup
        self.ready_wait = ready_wait
        self.say = say or (lambda text: print(text, file=sys.stderr, flush=True))
        self._env = env
        self.windows: list = []

    # ------------------------------------------------------------------ the loop
    def run(self) -> int:
        """Returns the launcher's exit status. Raises GaveUp at a deadline."""
        while True:
            generation = self.wait_ready()
            if generation is None:
                return 0
            outcome = self.show(generation)
            if outcome == "lost":
                continue
            return 0

    def wait_ready(self):
        """The current generation once it is ready, or None when the launcher is
        stopping. The deadline runs from the start of this wait."""
        deadline = time.monotonic() + self.ready_wait
        said = False
        while True:
            if self.stopping.is_set():
                return None
            generation = self.adapter.generation
            if (generation is not None and generation.ready.is_set()
                    and not generation.ended.is_set() and generation.thread_id):
                return generation
            if not said:
                self.say(self.text(self.WAITING, wait=self.ready_wait))
                said = True
            if time.monotonic() >= deadline:
                raise GaveUp(self.adapter.give_up_reason())
            self.stopping.wait(POLL)

    def show(self, generation) -> str:
        """One window on `generation`. Returns "lost" when it ended with its generation
        (a new one is waited for), "operator" when the operator closed it, and "stop"
        when the launcher is stopping."""
        window = self.spawn(generation)
        while True:
            if self.hangup.is_set():
                window.cause = CAUSE_HANGUP
                self.terminal.hung_up = True
                self.end(window)
                return "stop"
            if self.stopping.is_set():
                window.cause = CAUSE_SHUTDOWN
                self.end(window)
                self.restore(window)
                return "stop"
            if generation.ended.is_set():
                window.cause = CAUSE_LOST
                self.end(window)
                self.restore(window)
                self.say(self.text(self.LOST))
                return "lost"
            if not self.poll(window):
                time.sleep(POLL)
                continue
            # The window exited by itself.
            self.reap_group(window)
            if window.cause is None and not self.adapter.generation_answers(
                    generation, GENERATION_CHECK_WAIT):
                window.cause = CAUSE_LOST
                self.adapter.drop_generation(generation)
                self.restore(window)
                self.say(self.text(self.EXITED_LOST))
                return "lost"
            self.restore(window)
            if window.exit_code:
                self.say(self.text(self.EXIT_STATUS, status=window.exit_code))
            log.info("the operator closed the window for %s", self.agent.qualified)
            self.say(self.text(self.OFFLINE))
            return "operator"

    def text(self, template: str, **fields) -> str:
        return template.format(prog=self.PROG, identity=self.agent.qualified,
                               log_path=self.log_path, **fields)

    # ------------------------------------------------------------------ one window
    def argv(self, generation) -> list:
        """The window's command line, for `generation`."""
        raise NotImplementedError

    def spawn(self, generation) -> Window:
        """Spawn a window in its own process group, give it the terminal, and return."""
        argv = self.argv(generation)
        fd = self.terminal.fd
        self.terminal.save()
        die_with_parent = _die_with_parent(os.getpid())

        def preexec():
            os.setpgid(0, 0)
            # The launcher ignores SIGTTOU, and the child inherits that until exec, so it
            # can make its own group the foreground before the CLI touches the terminal.
            with contextlib.suppress(OSError):
                os.tcsetpgrp(fd, os.getpid())
            for sig in JOB_CONTROL_SIGNALS:
                signal.signal(sig, signal.SIG_DFL)
            if die_with_parent is not None:
                die_with_parent()

        env = self._env if self._env is not None else self.agent.env()
        proc = subprocess.Popen(argv, cwd=self.agent.cwd, env=env, stdin=fd, stdout=fd,
                                stderr=fd, preexec_fn=preexec, close_fds=True)
        with contextlib.suppress(OSError):
            os.setpgid(proc.pid, proc.pid)
        self.terminal.give(proc.pid)
        window = Window(proc, generation)
        self.windows.append(window)
        log.info("window pid %s opened on generation %d (thread %s)", proc.pid,
                 generation.number, generation.thread_id)
        return window

    def poll(self, window: Window) -> bool:
        """True once the window has exited. A window that stopped itself (a CLI such as Codex stops
        its own process group on Ctrl-Z) is continued, so it never sits stopped in front
        of a launcher that cannot be suspended."""
        if window.exited:
            return True
        try:
            pid, status = os.waitpid(window.proc.pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            window.status = 0
            window.proc.returncode = 0
            return True
        if pid == 0:
            return False
        if os.WIFSTOPPED(status):
            log.info("the window stopped itself (signal %d); continuing it: Ctrl-Z does "
                     "not suspend a window session, close the window to leave",
                     os.WSTOPSIG(status))
            self.terminal.give(window.pgid)
            with contextlib.suppress(OSError):
                os.killpg(window.pgid, signal.SIGCONT)
            return False
        window.status = status
        window.proc.returncode = os.waitstatus_to_exitcode(status)
        log.info("window pid %s exited with status %s", window.proc.pid,
                 window.proc.returncode)
        return True

    def end(self, window: Window) -> None:
        """SIGTERM to the window's group, then SIGKILL after the grace period. The cause
        is recorded before this is called."""
        log.info("ending window pid %s: %s", window.proc.pid, window.cause)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if window.exited and not self._group_alive(window):
                return
            with contextlib.suppress(OSError):
                os.killpg(window.pgid, sig)
            with contextlib.suppress(OSError):
                os.killpg(window.pgid, signal.SIGCONT)   # a stopped group takes it now
            deadline = time.monotonic() + TERM_GRACE
            while time.monotonic() < deadline:
                self.poll(window)
                if window.exited and not self._group_alive(window):
                    return
                time.sleep(0.02)

    def reap_group(self, window: Window) -> None:
        """After the window's leader exits, end anything left in its group."""
        if self._group_alive(window):
            self.end(window)

    @staticmethod
    def _group_alive(window: Window) -> bool:
        try:
            os.killpg(window.pgid, 0)
            return True
        except OSError:
            return False

    def restore(self, window: Window) -> None:
        """The terminal back to the launcher after a window ends, whatever ended it."""
        self.terminal.take_back()


__all__ = ["READY_WAIT", "GENERATION_CHECK_WAIT", "EXIT_GAVE_UP", "ENTER_ALT_SCREEN",
           "LEAVE_ALT_SCREEN", "SHOW_CURSOR", "RESTORE_SCREEN", "CAUSE_LOST", "CAUSE_SHUTDOWN", "CAUSE_HANGUP",
           "JOB_CONTROL_SIGNALS", "GaveUp", "Terminal", "Window", "WindowSession",
           "in_terminal_foreground", "terminal_fd"]
