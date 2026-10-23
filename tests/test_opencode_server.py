"""The opencode roster, the server's argv and log format, and the HTTP client's two
kinds of failure."""

from __future__ import annotations

import socket
import threading

import pytest
from conftest import write_config

from moxi_comms.adapters import opencode_roster as roster
from moxi_comms.adapters.opencode_server import (Http, NoAnswer, Unreachable,
                                                 parse_stream_line, serve_argv)
from moxi_comms.config import ConfigError

#: One request line exactly as OpenCode 1.18.34 wrote it with `--print-logs
#: --log-level INFO`. The line is a log format, not an API: a release that changes it
#: fails here before the model watch quietly stops seeing requests.
MEASURED = ("timestamp=2026-10-05T00:38:30.750Z level=INFO run=0a1b2c3d message=stream "
            "providerID=ollama modelID=qwen3.5-4b-b9-32k "
            "session.id=ses_0123456789abcdefABCDEFGHIJ small=true agent=title "
            "mode=primary\n")


def test_the_request_line_measured_on_1_18_34_parses():
    assert parse_stream_line(MEASURED) == {
        "provider": "ollama", "model": "qwen3.5-4b-b9-32k",
        "session": "ses_0123456789abcdefABCDEFGHIJ", "small": True, "agent": "title"}


@pytest.mark.parametrize("line", [
    "timestamp=x level=INFO message=\"opencode server listening on http://127.0.0.1:4096\"",
    "timestamp=x level=INFO message=streaming providerID=a modelID=b",
    "random text mentioning message=stream but no fields",
])
def test_other_lines_are_not_requests(line):
    assert parse_stream_line(line) is None


def test_the_server_listens_on_loopback_on_a_port_it_chooses_and_logs_requests():
    assert serve_argv("/x/opencode") == ["/x/opencode", "serve", "--hostname", "127.0.0.1",
                                         "--port", "0", "--print-logs", "--log-level",
                                         "INFO"]


def test_a_refused_connection_is_unreachable_and_a_dropped_answer_is_no_answer():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(Unreachable):
        Http(f"http://127.0.0.1:{port}", "pw", timeout=1).request("GET", "/x")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def swallow():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.close()                    # read the request, answer nothing

    threading.Thread(target=swallow, daemon=True).start()
    with pytest.raises(NoAnswer):
        Http(f"http://127.0.0.1:{listener.getsockname()[1]}", "pw", timeout=2).request(
            "POST", "/session/ses_x/prompt_async", {"messageID": "msg_x"})
    listener.close()


def write(tmp_path, text):
    """Write `text` as comms' config file, with each `[agents.` row under `[opencode]`.
    Returns None: `roster.load(None)` reads the file under this test's roots."""
    write_config(None, text.replace("[agents.", "[opencode.agents."))
    return None


def test_a_row_loads_with_the_default_model_and_its_session_file(tmp_path):
    path = write(tmp_path, '[agents."coder@proj-a"]\nslot = 6\ncwd = "/w"\n'
                           'directors = ["main"]\n')
    agent = roster.load(path).require("coder@proj-a")
    assert (agent.slot, agent.cwd, agent.models) == (6, "/w", list(roster.DEFAULT_MODELS))
    assert agent.session_file == roster.session_path("coder@proj-a")
    assert agent.directors == ["main"], "a bare director is the global identity"
    env = agent.env({"MOXI_COMMS_DIRECTORS": "boss@x", "PATH": "/bin"})
    assert (env["MOXI_COMMS_NAME"], env["MOXI_COMMS_SLOT"], env["MOXI_COMMS_SCOPE"]) == \
        ("coder", "6", "proj-a")
    assert env["MOXI_COMMS_DIRECTORS"] == "main", "the launcher's own grant never leaks"


def test_a_model_list_names_the_default_and_then_its_fallbacks(tmp_path):
    path = write(tmp_path, '[agents."coder@proj-a"]\nslot = 6\ncwd = "/w"\n'
                           'model = ["opencode/a", "opencode/b"]\n')
    assert roster.load(path).require("coder@proj-a").models == ["opencode/a", "opencode/b"]


@pytest.mark.parametrize("row,why", [
    ('cwd = "/w"\n', "no slot"),
    ('slot = 6\n', "no cwd"),
    ('slot = 6\ncwd = "/w"\nmodel = "nope"\n', "provider/model"),
    ('slot = 6\ncwd = "/w"\nport = 4096\n', "unknown key"),
    ('slot = 6\ncwd = "/w"\napi_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n', ""),
])
def test_a_bad_row_is_refused(tmp_path, row, why):
    path = write(tmp_path, f'[agents."coder@proj-a"]\n{row}')
    with pytest.raises(ConfigError) as caught:
        roster.load(path)
    assert why in str(caught.value)


def test_an_identity_the_roster_does_not_name_starts_nothing(tmp_path):
    with pytest.raises(ConfigError) as caught:
        roster.load(write(tmp_path, "")).require("ghost@proj-a")
    assert "registering an identity never starts one" in str(caught.value)


def test_a_row_takes_the_default_model_it_does_not_name(tmp_path):
    write(tmp_path, '[opencode.defaults]\nmodel = "opencode/d"\n'
                    '[agents."coder@proj-a"]\nslot = 6\ncwd = "/w"\n'
                    '[agents."other@proj-a"]\nslot = 7\ncwd = "/w"\nmodel = "opencode/o"\n')
    loaded = roster.load()
    assert loaded.require("coder@proj-a").models == ["opencode/d"]
    assert loaded.require("other@proj-a").models == ["opencode/o"]


@pytest.mark.parametrize("row,why", [
    ("slot = 6\n", "one identity's own"),
    ('session_file = "/tmp/s.json"\n', "one identity's own"),
    ("port = 4096\n", "unknown key"),
    # The defaults hold the model alone.
    ('directors = ["main"]\n', "one identity's own"),
    ('cwd = "/w"\n', "one identity's own"),
    ('binary = "/bin/oc"\n', "one identity's own"),
    ('model = "no-slash"\n', "provider/model"),
])
def test_defaults_refuse_a_key_of_one_identity_or_an_unknown_one(tmp_path, row, why):
    write(tmp_path, f"[opencode.defaults]\n{row}")
    with pytest.raises(ConfigError) as caught:
        roster.load()
    assert why in str(caught.value) and "[opencode.defaults]" in str(caught.value)


def test_the_binary_is_the_rows_then_path_then_the_installers_directory(tmp_path,
                                                                        monkeypatch):
    own = tmp_path / "own" / "opencode"
    own.parent.mkdir()
    own.write_text("#!/bin/sh\n")
    own.chmod(0o755)
    assert roster.resolve_binary(str(own)) == str(own)
    on_path = tmp_path / "bin" / "opencode"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    assert roster.resolve_binary(None, path=str(on_path.parent)) == str(on_path)
    installed = tmp_path / "home" / ".opencode" / "bin" / "opencode"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    monkeypatch.setattr(roster, "INSTALL_DIR", installed.parent)
    assert roster.resolve_binary(None, path=str(tmp_path / "empty")) == str(installed)
