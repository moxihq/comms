"""The OpenCode window: `opencode attach <url> -s <session>`, the session the person sits in.

The launcher runs it as its child in its own terminal, with the lifecycle the Codex
window had in comms (`window.WindowSession`): it opens only on a ready generation (one
server process with the launch's session and its event stream reconciled), the launcher
records why it ends a window before it ends it, a window that exits by itself while its
server still answers was closed by the person, and the launcher then stops, so the
identity goes offline. A window whose server was lost closes and reopens on the server
the launcher starts in its place.

`attach` reads the server password from `OPENCODE_SERVER_PASSWORD`, which the window's
environment carries.
"""

from __future__ import annotations

from .window import EXIT_GAVE_UP, READY_WAIT, GaveUp, WindowSession


def window_argv(binary: str, url: str, session_id: str) -> list:
    return [binary, "attach", url, "-s", str(session_id)]


class OpenCodeWindow(WindowSession):
    PROG = "moxi-comms-opencode"
    WAITING = ("{prog}: waiting for {identity}'s OpenCode session to be ready (up to "
               "{wait:.0f}s; Ctrl-C stops)")
    LOST = ("{prog}: the OpenCode server was lost; the window closed and reopens when the "
            "server is back")
    EXITED_LOST = ("{prog}: the window exited as its OpenCode server went; reopening it "
                   "when the server is back")
    EXIT_STATUS = ("{prog}: the opencode window exited with status {status}; the log is "
                   "{log_path}")
    OFFLINE = ("{prog}: {identity} is offline. `{prog} {identity}` launches it again, on "
               "a new session.")

    def argv(self, generation) -> list:
        return window_argv(self.binary, generation.url, generation.thread_id)


__all__ = ["EXIT_GAVE_UP", "READY_WAIT", "GaveUp", "OpenCodeWindow", "window_argv"]
