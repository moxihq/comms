"""comms' locations: its own five directories under Moxi's roots, and the runtime refusal.

Root resolution itself is the core's (`moxi.paths`); here we pin the comms subpaths and
the guard on a socket path handed in from the environment.
"""

import os

import pytest

from moxi_comms import paths


def test_comms_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("MOXI_COMMS_HOME", str(tmp_path))
    monkeypatch.setenv("MOXI_COMMS_DEVICE", "testbox")
    for var in ("MOXI_COMMS_DATA_HOME", "MOXI_COMMS_STATE_HOME", "MOXI_COMMS_RUNTIME_DIR",
                "MOXI_COMMS_CONFIG_HOME", "MOXI_COMMS_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    p = paths
    assert p.directory_db() == tmp_path / "data" / "comms" / "directory.db"  # DATA: not device-keyed
    # the custody journal is DATA too, and beside it: a binding is a live-session fact,
    # but what the journal is FOR is interpreting the record, which is device-agnostic
    assert p.bindings_db() == tmp_path / "data" / "comms" / "bindings.db"
    # Cards are DATA and device-agnostic -- who someone IS does not depend on which box
    # is asking.
    assert p.cards_dir() == tmp_path / "data" / "comms" / "cards"
    assert p.cursors_dir() == tmp_path / "state" / "testbox" / "comms" / "cursors"
    # the local backend's record: STATE and per box, because DATA is the sync surface
    # and a live DB is never continuous-file-synced
    assert p.record_db() == tmp_path / "state" / "testbox" / "comms" / "record.db"
    assert p.tokens_dir() == tmp_path / "state" / "testbox" / "comms" / "tokens"
    # comms writes only under its own five directories, so its logs are in STATE/comms
    assert p.log_dir() == tmp_path / "state" / "testbox" / "comms" / "logs"
    # and its sockets in RUNTIME/comms
    assert p.daemon_socket() == tmp_path / "runtime" / "comms" / "moxi-comms-daemon.sock"
    assert not hasattr(p, "codex_socket"), "turns' host owns the app-server's socket"
    assert p.opencode_runtime_dir() == tmp_path / "runtime" / "comms" / "opencode"
    assert p.config_dir() == tmp_path / "config" / "comms"


def test_every_location_reads_a_given_environment(tmp_path):
    env = {"MOXI_COMMS_HOME": str(tmp_path), "MOXI_COMMS_DEVICE": "box2"}
    assert paths.record_db(env) == tmp_path / "state" / "box2" / "comms" / "record.db"
    assert paths.daemon_socket(env) == tmp_path / "runtime" / "comms" / "moxi-comms-daemon.sock"


def test_the_driver_anchor_directory_is_gone():
    """The `driver` kind is the fleet's add-on's (design §7.2): comms reads no anchor."""
    assert not hasattr(paths, "driver_sessions_dir")


def test_a_socket_under_the_runtime_directory_passes():
    sock = paths.runtime_dir() / "comms-claimsrv-x.sock"
    assert paths.refuse_outside_runtime(sock, "MOXI_COMMS_CLAIM_SOCKET") == sock
    assert paths.refuse_outside_runtime(str(paths.daemon_socket()),
                                        "MOXI_COMMS_SOCKET") == paths.daemon_socket()


@pytest.mark.parametrize("variable", ["MOXI_COMMS_CLAIM_SOCKET", "MOXI_COMMS_SOCKET"])
def test_a_socket_outside_the_runtime_directory_is_refused(tmp_path, variable):
    """A fleet session's stamp names a socket under another program's runtime root; a
    send inside it must not claim that identity against comms' daemon (design §10.2)."""
    other = tmp_path / "other-program" / "comms-daemon.sock"
    with pytest.raises(paths.OutsideRuntime) as caught:
        paths.refuse_outside_runtime(other, variable)
    text = str(caught.value)
    assert variable in text and str(paths.runtime_dir()) in text and str(other) in text


def test_the_runtime_directory_itself_and_its_root_are_refused():
    """Only a path inside RUNTIME/comms is comms' own: not the directory itself, and not
    another module's directory under the shared root."""
    for path in (paths.runtime_dir(), paths.LAYOUT.runtime_root() / "turns" / "x.sock",
                 paths.runtime_dir() / ".." / "turns" / "x.sock"):
        with pytest.raises(paths.OutsideRuntime):
            paths.refuse_outside_runtime(path, "MOXI_COMMS_CLAIM_SOCKET")


def test_a_link_inside_the_runtime_directory_that_leaves_it_is_refused(tmp_path):
    outside = tmp_path / "elsewhere.sock"
    outside.touch()
    runtime = paths.runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    link = runtime / "looks-local.sock"
    os.symlink(outside, link)
    with pytest.raises(paths.OutsideRuntime):
        paths.refuse_outside_runtime(link, "MOXI_COMMS_CLAIM_SOCKET")
