"""The default backend, and a daemon's first-start provisioning.

With no flag, environment variable or config naming a backend, the backend is `local`;
`mock` is only ever chosen by name. There is one behaviour: no profile switches the
fallthrough to `mock`.

A daemon on `local` whose directory is not provisioned provisions it before it serves,
with what `moxi-comms-provision` makes when run with no flags: `[provision] slots` (6),
no standing identities, the operator `[provision] operator`. It never does so on `mock`,
and never before its single-instance probe has shown that no other daemon holds the
socket.

"Provisioned" has one definition (`provision.readiness`), shared by the daemon's check,
`moxi comms doctor`'s slot count and the clean-home test: the directory holds slots and
its declared writer, and on `local` every slot's token is in its holder's file and the
operator's stored token hashes to the hash the record keeps. A directory with slots whose
operator credential was never minted is an unfinished first start, which the daemon
finishes; one whose tokens were lost is refused, because re-provisioning cannot recover
a lost token and a daemon that started anyway would fail at the first send.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import write_config

from moxi_comms import cli, config, directory, gateway as gw, provision, tokens
from moxi_comms.daemon import Daemon
from moxi_comms.local_gateway import META_OPERATOR_TOKEN_HASH, LocalGateway
from moxi_comms.paths import daemon_socket, directory_db, record_db, tokens_dir


def env_of(**extra) -> dict:
    """The rig's own environment, minus anything that names a backend."""
    env = {k: v for k, v in os.environ.items() if k != "MOXI_COMMS_GATEWAY"}
    env.update(extra)
    return env


def operator_files(store) -> None:
    owner = tokens.owner_for_role(tokens.ROLE_OPERATOR)
    store.path_for(owner).unlink()


# --------------------------------------------------------------- the default backend

def test_with_nothing_named_the_backend_is_local():
    assert gw.backend_name(env=env_of()) == gw.LOCAL
    choice = gw.backend_choice(env=env_of())
    assert (choice.name, choice.source) == (gw.LOCAL, gw.SOURCE_DEFAULT)
    assert choice.describe() == "the default"
    assert gw.default_backend() == gw.LOCAL


def test_the_profile_is_gone_from_the_choice():
    """`MOXI_COMMS_PROFILE` is read by nothing, and the choice carries no profile."""
    assert gw.backend_name(env=env_of(MOXI_COMMS_PROFILE="x")) == gw.LOCAL
    assert not hasattr(gw.backend_choice(env=env_of()), "profile")


def test_a_named_backend_wins(tmp_path):
    assert gw.backend_choice("mock", env=env_of()).source == gw.SOURCE_FLAG
    assert gw.backend_choice("mock", env=env_of()).name == gw.MOCK
    env_set = env_of(MOXI_COMMS_GATEWAY="mock")
    assert (gw.backend_choice(env=env_set).name,
            gw.backend_choice(env=env_set).source) == (gw.MOCK, gw.SOURCE_ENV)
    write_config(None, 'backend = "mock"\n')
    assert (gw.backend_choice(env=env_of()).name,
            gw.backend_choice(env=env_of()).source) == (gw.MOCK, gw.SOURCE_CONFIG)


def test_the_most_local_choice_wins(tmp_path):
    """Flag over `MOXI_COMMS_GATEWAY` over the config file, with two sources set at
    once (one at a time cannot tell the order)."""
    write_config(None, 'backend = "local"\n')
    assert gw.backend_name("mock", env=env_of(MOXI_COMMS_GATEWAY="local")) == gw.MOCK
    choice = gw.backend_choice("local", env=env_of(MOXI_COMMS_GATEWAY="mock"))
    assert (choice.name, choice.source) == (gw.LOCAL, gw.SOURCE_FLAG)
    choice = gw.backend_choice(env=env_of(MOXI_COMMS_GATEWAY="mock"))
    assert (choice.name, choice.source) == (gw.MOCK, gw.SOURCE_ENV)


def test_the_help_text_no_longer_calls_mock_the_default():
    doc = gw.backend_name.__doc__ + gw.backend_choice.__doc__
    assert "mock (default)" not in doc and "> mock" not in doc
    for module in (cli, provision):
        assert "mock (default)" not in Path(module.__file__).read_text()
    from moxi_comms import daemon
    assert "mock (default)" not in Path(daemon.__file__).read_text()


@pytest.mark.parametrize("named", ["flag", "env", "config"])
def test_a_removed_backend_is_refused_wherever_it_is_named(named, tmp_path):
    """`discord` is no backend: a flag, `MOXI_COMMS_GATEWAY` or the config file naming
    it is refused, and the refusal lists the two there are."""
    env = env_of()
    if named == "config":
        write_config(None, 'backend = "discord"\n')
    if named == "env":
        env["MOXI_COMMS_GATEWAY"] = "discord"
    with pytest.raises(gw.GatewayError) as e:
        gw.backend_name("discord" if named == "flag" else None, env=env)
    said = str(e.value)
    if named == "flag":
        assert said == "unknown gateway backend 'discord'; expected one of local, mock"
    else:
        assert "'discord'" in said and "'local', 'mock'" in said
        assert ("MOXI_COMMS_GATEWAY" if named == "env" else "config.toml") in said


# --------------------------------------------------------------- one definition

def _local(tmp_path):
    return LocalGateway(poll_interval=0.02), tokens.TokenStore(tokens_dir())


def test_readiness_of_nothing_is_empty_and_writes_nothing(tmp_path):
    store = tokens.TokenStore(tokens_dir())
    found = provision.readiness(directory_db(), operator_hash=None, store=store)
    assert (found.state, found.slots) == (provision.EMPTY, 0)
    assert not directory_db().exists() and not tokens_dir().exists()


def test_readiness_after_the_cli_default_is_complete(tmp_path):
    gateway, store = _local(tmp_path)
    try:
        provision.provision(gateway, directory_db(), slots=provision.DEFAULT_SLOTS,
                            store=store)
        found = provision.readiness(directory_db(),
                                    operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH),
                                    store=store)
    finally:
        gateway.close()
    assert (found.state, found.slots) == (provision.COMPLETE, 6)
    assert found.provisioned


def test_slots_without_the_operator_credential_are_unfinished(tmp_path):
    """A first start that died after the directory committed and before the operator's
    credential was minted: slot rows exist, and they are not enough."""
    gateway, store = _local(tmp_path)
    try:
        provision.provision(gateway, directory_db(), slots=2, store=store)
        operator_files(store)
        with gateway._write() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (META_OPERATOR_TOKEN_HASH,))
        found = provision.readiness(directory_db(), operator_hash=None, store=store)
    finally:
        gateway.close()
    assert (found.state, found.slots) == (provision.UNFINISHED, 2)
    assert not found.provisioned


@pytest.mark.parametrize("loss", ["the whole store", "the operator's file", "one slot"])
def test_a_lost_token_store_is_lost_not_a_first_start(tmp_path, loss):
    gateway, store = _local(tmp_path)
    try:
        provision.provision(gateway, directory_db(), slots=3, store=store)
        if loss == "the whole store":
            for path in sorted(tokens_dir().rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
        elif loss == "the operator's file":
            operator_files(store)
        else:
            store.path_for(tokens.owner_for_slot(2)).unlink()
        found = provision.readiness(
            directory_db(), operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH),
            store=store)
    finally:
        gateway.close()
    assert (found.state, found.slots) == (provision.LOST, 3)
    assert not found.provisioned
    assert "token" in found.detail


# --------------------------------------------------------------- the daemon's first start

def _daemon(gateway, store, **kw):
    return Daemon(gateway, start_trim_thread=False,
                  first_start=provision.first_start(store), **kw)


def test_a_daemon_on_an_empty_directory_provisions_before_it_serves(tmp_path):
    gateway, store = _local(tmp_path)
    daemon = _daemon(gateway, store)
    try:
        daemon.start()
        conn = directory.connect(directory_db(), read_only=True)
        try:
            assert directory.slot_ids(conn) == [1, 2, 3, 4, 5, 6]
            assert [r["name"] for r in conn.execute("SELECT name FROM operators")] == \
                ["operator"]
            assert conn.execute("SELECT COUNT(*) FROM webhooks WHERE agent_name IS NOT "
                                "NULL").fetchone()[0] == 0, "no standing identities"
        finally:
            conn.close()
        found = provision.readiness(
            directory_db(), operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH),
            store=store)
        assert found.state == provision.COMPLETE
    finally:
        daemon.stop()
        gateway.close()


def test_a_complete_directory_is_left_alone(tmp_path):
    gateway, store = _local(tmp_path)
    provision.provision(gateway, directory_db(), slots=2, store=store)
    before = sorted(p.stat().st_mtime_ns for p in tokens_dir().rglob("*") if p.is_file())
    daemon = _daemon(gateway, store)
    try:
        daemon.start()
        conn = directory.connect(directory_db(), read_only=True)
        try:
            assert directory.slot_ids(conn) == [1, 2], "a user's own count is kept"
        finally:
            conn.close()
        after = sorted(p.stat().st_mtime_ns for p in tokens_dir().rglob("*") if p.is_file())
        assert after == before
    finally:
        daemon.stop()
        gateway.close()


def test_an_unfinished_first_start_is_finished_with_its_own_slot_count(tmp_path):
    gateway, store = _local(tmp_path)
    provision.provision(gateway, directory_db(), slots=2, store=store)
    operator_files(store)
    with gateway._write() as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (META_OPERATOR_TOKEN_HASH,))
    daemon = _daemon(gateway, store)
    try:
        daemon.start()
        found = provision.readiness(
            directory_db(), operator_hash=gateway.meta_get(META_OPERATOR_TOKEN_HASH),
            store=store)
        assert (found.state, found.slots) == (provision.COMPLETE, 2)
    finally:
        daemon.stop()
        gateway.close()


def test_a_lost_token_store_refuses_to_start(tmp_path):
    gateway, store = _local(tmp_path)
    provision.provision(gateway, directory_db(), slots=2, store=store)
    operator_files(store)
    daemon = _daemon(gateway, store)
    try:
        with pytest.raises(RuntimeError, match="token"):
            daemon.start()
        assert not daemon.bound and not daemon_socket().exists()
        assert store.read_secret(tokens.owner_for_role(tokens.ROLE_OPERATOR),
                                 tokens.OPERATOR_SECRET_ID) is None, "nothing re-minted"
    finally:
        daemon.stop()
        gateway.close()


def test_first_start_waits_for_the_single_instance_probe(tmp_path):
    """A second daemon refused by the probe has provisioned nothing."""
    gateway, store = _local(tmp_path)
    provision.provision(gateway, directory_db(), slots=1, store=store)
    first = Daemon(gateway, start_trim_thread=False).start()
    directory_db().unlink()                 # what a first-start provisioning would refill
    second = _daemon(gateway, store)
    try:
        with pytest.raises(RuntimeError, match="live comms daemon"):
            second.start()
        assert not directory_db().exists()
    finally:
        second.stop()
        first.stop()
        gateway.close()


# --------------------------------------------------------------- the real process

def _clean_env(tmp_path, **extra) -> dict:
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("MOXI_", "XDG_", "CLAUDE"))}
    keep.update(HOME=str(tmp_path / "u"), **extra)
    return keep


def _run_daemon(env, sock: Path, *, expect_socket=True):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; from moxi_comms.daemon import main; "
                               "sys.exit(main([]))"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 20
        while expect_socket and not sock.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert not expect_socket or sock.exists(), proc.stdout.read() if proc.poll() \
            is not None else "no socket"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
    return proc.returncode, out


def test_the_daemon_process_provisions_six_slots(tmp_path):
    home = tmp_path / "solo"
    env = _clean_env(tmp_path, MOXI_COMMS_HOME=str(home),
                     MOXI_COMMS_RUNTIME_DIR=str(tmp_path / "r"), MOXI_COMMS_DEVICE="t")
    code, out = _run_daemon(env, tmp_path / "r" / "comms" / "moxi-comms-daemon.sock")
    assert code == 0, out
    db = home / "data" / "comms" / "directory.db"
    conn = directory.connect(db, read_only=True)
    try:
        assert directory.slot_ids(conn) == [1, 2, 3, 4, 5, 6]
    finally:
        conn.close()


def test_a_daemon_process_on_mock_never_provisions(tmp_path):
    """`mock` is a test seam: a daemon on it with no directory starts and mints
    nothing."""
    home = tmp_path / "mocked"
    env = _clean_env(tmp_path, MOXI_COMMS_HOME=str(home),
                     MOXI_COMMS_RUNTIME_DIR=str(tmp_path / "r"), MOXI_COMMS_DEVICE="t",
                     MOXI_COMMS_GATEWAY="mock",
                     MOXI_COMMS_MOCK_GATEWAY=str(tmp_path / "mock.json"))
    code, out = _run_daemon(env, tmp_path / "r" / "comms" / "moxi-comms-daemon.sock")
    assert code == 0, out
    assert not (home / "data" / "comms" / "directory.db").exists()
    assert not (home / "state" / "t" / "comms" / "tokens").exists()


# --------------------------------------------------------------- moxi comms doctor

def doctor(env):
    out = io.StringIO()
    ctx = cli.Context(gateway=object(), env=env, out=out,
                      socket_path=Path("/nonexistent/comms.sock"))
    code = cli.main(["doctor"], ctx=ctx)
    return code, {line[:10].strip(): line[12:] for line in out.getvalue().splitlines()
                  if line[:1] != " "}


def test_doctor_says_where_the_backend_came_from_and_counts_slots(tmp_path):
    code, rows = doctor(env_of(PATH=""))
    assert code == 0
    assert rows["backend"] == "local (the default)"
    assert rows["slots"] == "none yet (the daemon provisions 6 on its first start)"
    assert not directory_db().exists()

    gateway, store = _local(tmp_path)
    try:
        provision.provision(gateway, directory_db(), slots=6, store=store)
    finally:
        gateway.close()
    _, rows = doctor(env_of(PATH=""))
    assert rows["slots"] == "6 provisioned"

    operator_files(store)
    _, rows = doctor(env_of(PATH=""))
    assert rows["slots"].startswith("6, but ")
    assert "token" in rows["slots"]

    _, rows = doctor(env_of(PATH="", MOXI_COMMS_GATEWAY="mock"))
    assert rows["backend"] == "mock (MOXI_COMMS_GATEWAY)"

    write_config(None, 'backend = "local"\n')
    _, rows = doctor(env_of(PATH=""))
    assert rows["backend"] == "local (the config file)"
