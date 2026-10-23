"""The local backend: the things only a SQLite record can get wrong.

Everything the seam shares with the other backend is covered by the parametrised
suites (conftest's `BOTH_BACKENDS`). What is here is local-only: the id counter surviving a
trim, two PROCESSES writing without collision, N subscriptions each seeing each id once,
retention, the two reads the daemon does in bursts staying cheap on a full record, and the
daemon refusing a `--gateway` flag that disagrees with the box.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from conftest import wait_until, write_config

from moxi_comms import config, daemon as daemon_mod, gateway as gateway_mod, paths
from moxi_comms.gateway import NO_MENTIONS, Forbidden, GatewayError
from moxi_comms.local_gateway import (
    ID_BASE,
    META_OPERATOR_TOKEN_HASH,
    LocalGateway,
    token_hash,
)


@pytest.fixture
def gw(tmp_path):
    """A record of its own, with the pump fast enough for a test to wait on it."""
    tokens: dict[str, str] = {}
    gateway = LocalGateway(tmp_path / "record.db", poll_interval=0.02,
                           secret_reader=lambda owner, sid: tokens.get(sid))
    gateway.minted = tokens
    try:
        yield gateway
    finally:
        gateway.close()


def webhook(gateway, channel_id, name="slot-1", secret_id="comms/slot-1/names"):
    cred = gateway.create_webhook(channel_id, name, secret_id)
    gateway.minted[secret_id] = cred.token
    return cred


def post(gateway, cred, text, username="planner"):
    return gateway.execute_webhook(cred, text, username=username,
                                   allowed_mentions=NO_MENTIONS)


# --------------------------------------------------------------------------- the file

def test_the_record_lives_in_state_beside_the_cursors_and_tokens():
    """STATE, per box, and not DATA."""
    assert paths.record_db() == paths.cursors_dir().parent / "record.db"
    assert paths.record_db().parent == paths.tokens_dir().parent


def test_the_file_is_wal_with_its_pragmas(gw):
    conn = gw._conn()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1        # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_the_schema_is_seven_tables_and_no_reply_column(gw):
    conn = gw._conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"channels", "webhooks", "principals", "denied", "messages", "meta"} <= tables
    columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    # `Message`'s seven fields and nothing else
    assert columns == ["id", "channel_id", "author_id", "author_kind", "author_display",
                       "content", "timestamp"]
    assert "reply_to" not in columns


# --------------------------------------------------------------------------- ids

def test_the_counter_outlives_a_trim_to_empty(gw):
    """The regression: `MAX(id)+1` over a table retention also empties
    hands out an id every reader's cursors already compare as past."""
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    first = post(gw, cred, "one")
    last = post(gw, cred, "two")
    assert gw.trim(retain_days=0) == 0, "0 disables trimming"
    gw.clock = lambda: time.time() + 100 * 86400        # a hundred days pass
    assert gw.trim() == 2
    assert gw.history(channel) == []
    after = post(gw, cred, "three")
    assert int(after.id) > int(last.id) > int(first.id)


def test_every_id_comes_from_one_sequence_above_every_older_id(gw):
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    principal = gw.register_principal("comms-reader")
    msg = post(gw, cred, "hello")
    assert channel.startswith("ch-") and cred.webhook_id.startswith("wh-")
    numbers = [int(channel.removeprefix("ch-")), int(cred.webhook_id.removeprefix("wh-")),
               int(principal.removeprefix("pr-")), int(msg.id)]
    assert numbers == sorted(numbers) and len(set(numbers)) == 4
    assert all(n > ID_BASE for n in numbers)
    # message ids ride the wire as bare digits, exactly as today
    assert msg.id.isdigit()


def test_two_processes_writing_never_collide_and_never_go_backwards(gw, tmp_path):
    """`BEGIN IMMEDIATE` serialises the writers on SQLite's own lock.

    A real second PROCESS, because that is the case the counter exists for: the CLI
    posting while the daemon posts a notice, two connections, one file.
    """
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_WRITER, str(tmp_path / "record.db"), channel,
         cred.secret_id, cred.token, "40"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    mine = [int(post(gw, cred, f"parent {i}").id) for i in range(40)]
    out, err = child.communicate(timeout=60)
    assert child.returncode == 0, err
    theirs = [int(line) for line in out.split()]

    assert mine == sorted(mine), "this process's own ids never went backwards"
    assert theirs == sorted(theirs), "the other process's ids never went backwards"
    assert not set(mine) & set(theirs), "no id was issued twice"
    everything = [int(m.id) for m in gw.history(channel)]
    assert everything == sorted(set(mine) | set(theirs))


CHILD_WRITER = """
import sys
from moxi_comms.gateway import NO_MENTIONS
from moxi_comms.local_gateway import LocalGateway

record, channel, secret_id, token, count = sys.argv[1:6]
gw = LocalGateway(record, poll_interval=60,
                  secret_reader=lambda owner, sid: token)
cred = gw.resolve_secret(secret_id)
for i in range(int(count)):
    print(gw.execute_webhook(cred, "child %d" % i, username="child",
                             allowed_mentions=NO_MENTIONS).id)
gw.close()
"""


# --------------------------------------------------------------------------- webhooks

def test_a_webhook_row_keeps_only_the_hash_and_the_compare_is_against_it(gw):
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    stored = gw._conn().execute("SELECT token_hash FROM webhooks WHERE webhook_id = ?",
                                (cred.webhook_id,)).fetchone()[0]
    assert stored == token_hash(cred.token)
    assert cred.token not in gw.path.read_bytes().decode("latin-1")
    assert post(gw, cred, "fine").content == "fine"

    from moxi_comms.gateway import Credential
    forged = Credential(cred.webhook_id, "not-the-token", secret_id=cred.secret_id)
    with pytest.raises(GatewayError, match="bad webhook credential"):
        post(gw, forged, "nope")
    assert len(gw.history(channel)) == 1, "a refused post writes nothing"


def test_resolve_secret_reads_the_holders_store_and_says_so_when_it_is_empty(gw):
    channel = gw.create_channel("#names")
    cred = gw.create_webhook(channel, "slot-1", "comms/slot-1/names")   # NOT stored
    with pytest.raises(GatewayError, match="token store"):
        gw.resolve_secret("comms/slot-1/names")
    gw.minted["comms/slot-1/names"] = cred.token
    again = gw.resolve_secret("comms/slot-1/names")
    assert (again.webhook_id, again.token) == (cred.webhook_id, cred.token)
    with pytest.raises(GatewayError, match="unknown secret"):
        gw.resolve_secret("comms/slot-9/names")


def test_resolve_secret_goes_through_the_stores_declared_read(tmp_path):
    """No injected lookup: the real `TokenStore.read_secret`,
    reading the key provision writes under the row's `secret_id`."""
    from moxi_comms import tokens

    store = tokens.TokenStore()
    owner = tokens.owner_for_slot(1)
    gateway = LocalGateway(tmp_path / "record.db", poll_interval=60)
    try:
        channel = gateway.create_channel("#names")
        cred = gateway.create_webhook(channel, "slot-1", "comms/slot-1/names")
        store.write_secret(owner, "comms/slot-1/names", cred.token)
        resolved = gateway.resolve_secret("comms/slot-1/names", owner=owner)
        assert (resolved.webhook_id, resolved.token) == (cred.webhook_id, cred.token)
        assert gateway.execute_webhook(resolved, "through the store", username="slot-1",
                                       allowed_mentions=NO_MENTIONS).content == \
            "through the store"
    finally:
        gateway.close()


def test_the_refusals_keep_the_mocks_order(gw):
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    # mentions first, so a post with a bad credential AND mentions says mentions
    from moxi_comms.gateway import Credential
    forged = Credential(cred.webhook_id, "wrong", secret_id=cred.secret_id)
    with pytest.raises(GatewayError, match="allowed_mentions"):
        gw.execute_webhook(forged, "x", username="a", allowed_mentions=None)
    # ...then the cap, then the credential
    with pytest.raises(GatewayError, match="cap on one message"):
        gw.execute_webhook(forged, "x" * 20_000, username="a",
                           allowed_mentions=NO_MENTIONS)
    # ...then the denial
    gw.deny_send(cred.webhook_id, channel)
    with pytest.raises(Forbidden):
        post(gw, cred, "denied")


def test_the_cap_is_sixteen_thousand(gw):
    """One cap on one message, 16,000 characters."""
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    assert gw.max_content_length == 16_000
    assert post(gw, cred, "x" * 16_000).content == "x" * 16_000
    with pytest.raises(GatewayError, match="cap on one message"):
        post(gw, cred, "x" * 16_001)
    assert len(gw.history(channel)) == 1


def test_the_reader_denial_is_a_real_check(gw):
    channel = gw.create_channel("#names")
    reader = gw.register_principal("comms-reader")
    assert gw.can_send(reader, channel)
    gw.deny_send(reader, channel)
    assert not gw.can_send(reader, channel)
    gw.deny_send(reader, channel)              # idempotent, as provision calls it


# --------------------------------------------------------------------------- operator

def test_the_operator_lane_compares_the_token_against_the_stored_hash(gw):
    """The lane needs a credential like every other lane. The SENDER side of the guard
    is the send verb's; the compare is this backend's."""
    channel = gw.create_channel("#names")
    assert gw.post_as_user("900001", channel, "before provisioning").author_kind == "user"
    gw.meta_set(META_OPERATOR_TOKEN_HASH, token_hash("the-operator-token"))
    assert gw.post_as_user("900001", channel, "with the token",
                           token="the-operator-token").content == "with the token"
    with pytest.raises(Forbidden, match="bad operator token"):
        gw.post_as_user("900001", channel, "with a guess", token="guessed")
    with pytest.raises(Forbidden, match="bad operator token"):
        # A stored hash makes the token REQUIRED: omitting it is not a pass.
        gw.post_as_user("900001", channel, "without one")
    assert len(gw.history(channel)) == 2


def test_a_registered_principal_wears_its_own_display(gw):
    channel = gw.create_channel("#names")
    operator = gw.register_principal("operator")
    assert gw.post_as_user(operator, channel, "hi").author_display == "operator"
    assert gw.post_as_user("900001", channel, "hi").author_display == "operator"


# --------------------------------------------------------------------------- the record

def test_history_with_a_limit_takes_the_tail(gw):
    channel = gw.create_channel("#names")
    other = gw.create_channel("#elsewhere")
    cred = webhook(gw, channel)
    elsewhere = webhook(gw, other, "elsewhere", "comms/slot-2/elsewhere")
    ids = [post(gw, cred, f"m{i}").id for i in range(10)]
    post(gw, elsewhere, "another channel")
    assert [m.id for m in gw.history(channel)] == ids
    assert [m.id for m in gw.history(channel, limit=3)] == ids[-3:]
    assert [m.id for m in gw.history(channel, after=ids[6])] == ids[7:]
    assert [m.id for m in gw.history(channel, after=ids[0], limit=2)] == ids[-2:]
    assert len(gw.history(other)) == 1, "a channel's history is its own"


def test_get_message_and_max_message_id(gw):
    channel = gw.create_channel("#names")
    other = gw.create_channel("#elsewhere")
    cred = webhook(gw, channel)
    assert gw.max_message_id() is None and gw.max_message_id(channel) is None
    msg = post(gw, cred, "one")
    assert gw.get_message(msg.id).content == "one"
    assert gw.get_message("4611686018427387000") is None
    assert gw.get_message("not-a-message-id") is None
    assert gw.max_message_id() == msg.id == gw.max_message_id(channel)
    assert gw.max_message_id(other) is None


# --------------------------------------------------------------------------- retention

def test_retention_drops_what_is_past_the_window_and_keeps_the_rest(gw):
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    now = time.time()
    gw.clock = lambda: now - 120 * 86400
    ancient = post(gw, cred, "a hundred and twenty days ago")
    gw.clock = lambda: now - 10 * 86400
    recent = post(gw, cred, "ten days ago")
    gw.clock = lambda: now

    assert gw.trim(retain_days=0) == 0
    assert len(gw.history(channel)) == 2
    assert gw.trim() == 1                       # the default is 90 days
    assert [m.id for m in gw.history(channel)] == [recent.id]
    assert gw.get_message(ancient.id) is None


def test_the_daemon_trims_at_boot_and_only_where_there_is_a_record(rig):
    """The daemon is the process that runs all day, so the daily trim is its job.

    On the mock there is nothing to trim, and `trim_record` has to be a no-op rather
    than an error: one daemon, three backends.
    """
    assert rig.daemon._last_trim > 0, "the boot trim ran, or was asked and declined"
    rig.daemon.trim_record()
    assert hasattr(rig.gateway, "trim") is (rig.backend == "local")


# --------------------------------------------------------------------------- fanout

def test_two_subscriptions_each_see_every_id_exactly_once(gw):
    """The pump iterates `_subs` per pass and each subscription's `last_id` moves
    under the gateway's own lock."""
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    first = gw.subscribe(from_id="0")
    second = gw.subscribe(from_id="0")
    assert first.next_event(timeout=1.0)["t"] == "READY"
    assert second.next_event(timeout=1.0)["t"] == "READY"

    posted = [post(gw, cred, f"m{i}").id for i in range(25)]
    for sub in (first, second):
        seen = []
        while len(seen) < len(posted):
            event = sub.next_event(timeout=2.0)
            assert event is not None, f"only saw {len(seen)} of {len(posted)}"
            assert event["t"] == "MESSAGE_CREATE"
            seen.append(event["d"]["id"])
        assert seen == posted, "in id order, each id once"
        assert sub.next_event(timeout=0.1) is None, "and nothing twice"
    gw.unsubscribe(first)
    gw.unsubscribe(second)


def test_a_subscription_says_ready_first_so_the_daemon_sees_the_shapes_it_knows(gw):
    sub = gw.subscribe()
    assert sub.next_event(timeout=1.0) == {"t": "READY", "resumed": True}
    gw.unsubscribe(sub)


def test_the_pump_finds_what_another_process_wrote(gw, tmp_path):
    """Layer 3, the safety net: no nudge arrives from another process (that is the
    daemon's socket op), and the poll finds the row anyway."""
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    sub = gw.subscribe(from_id="0")
    assert sub.next_event(timeout=1.0)["t"] == "READY"
    out = subprocess.run(
        [sys.executable, "-c", CHILD_WRITER, str(tmp_path / "record.db"), channel,
         cred.secret_id, cred.token, "1"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    event = sub.next_event(timeout=3.0)
    assert event["t"] == "MESSAGE_CREATE" and event["d"]["id"] == out.stdout.strip()
    gw.unsubscribe(sub)


def test_nudge_wakes_the_pump_without_waiting_out_the_interval(tmp_path):
    gateway = LocalGateway(tmp_path / "record.db", poll_interval=30,
                           secret_reader=lambda owner, sid: None)
    try:
        channel = gateway.create_channel("#names")
        cred = gateway.create_webhook(channel, "slot-1", "comms/slot-1/names")
        sub = gateway.subscribe(from_id="0")
        assert sub.next_event(timeout=1.0)["t"] == "READY"
        # a row written behind the gateway's back: only the pump can find it, and only
        # the nudge can make it look before the 30-second interval is up
        with gateway._write() as conn:
            gateway._insert(conn, channel_id=channel, author_id=cred.webhook_id,
                            author_kind="webhook", author_display="planner",
                            content="nudged")
        assert sub.next_event(timeout=0.3) is None
        gateway.nudge()
        event = sub.next_event(timeout=2.0)
        assert event is not None and event["d"]["content"] == "nudged"
    finally:
        gateway.close()


def test_a_reconnect_without_resume_makes_the_daemon_rescan(gw):
    channel = gw.create_channel("#names")
    cred = webhook(gw, channel)
    sub = gw.subscribe(from_id="0")
    assert sub.next_event(timeout=1.0)["t"] == "READY"
    gw.disconnect()
    post(gw, cred, "into the gap")
    assert sub.next_event(timeout=0.2) is None
    gw.reconnect(resume=False)
    assert sub.next_event(timeout=1.0) == {"t": "READY", "resumed": False}
    assert sub.next_event(timeout=0.2) is None, "a fresh IDENTIFY loses the gap"


# --------------------------------------------------------------------------- the reads

def test_the_daemons_two_hot_reads_stay_cheap_on_a_full_record(gw):
    """`max_message_id` is the register boundary read (taken in bursts at a restart)
    and `history(limit=RESCAN_LIMIT)` is the boot rescan. Both are index walks, so the
    target is under 5 ms on 100,000 rows.

    Two claims, because a wall clock alone cannot carry this inside a suite:

      * the PLAN, which is the statement that matters and cannot flake. Neither read may
        SCAN `messages`: both walk the primary key or `messages_channel`, which is what
        makes the cost O(log n) rather than O(n).
      * the COST, the 5 ms target, measured as CPU this thread spent
        (`time.thread_time`) and not as wall clock. Wall clock here measures the box,
        not the query: run after `test_guards.py`, which leaves a `Daemon` unstopped and
        its event loop spinning, the same reads take a thousand times longer by the wall
        and exactly as long by the clock that counts work. Best of fifty passes after a
        warm-up, so a scheduler hiccup cannot fail a test about a query plan.

    Nothing here reads the channel whole to show the ratio, deliberately: building
    100,000 `Message` objects inside a process the size of this suite spends its time in
    the garbage collector, and a fixture that slow is a test that looks hung.
    """
    channel = gw.create_channel("#names")
    fill(gw, channel, 100_000)
    assert gw._conn().execute("SELECT count(*) FROM messages").fetchone()[0] == 100_000

    for sql, params in (
            ("SELECT MAX(id) FROM messages", ()),
            ("SELECT MAX(id) FROM messages WHERE channel_id = ?", (channel,)),
            ("SELECT id FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
             (channel, 500)),
            ("SELECT id FROM messages WHERE channel_id = ? AND id > ? "
             "ORDER BY id DESC LIMIT ?", (channel, 0, 500))):
        plan = " ".join(row[3] for row in
                        gw._conn().execute("EXPLAIN QUERY PLAN " + sql, params))
        assert "SCAN" not in plan, f"{sql} reads the table, not an index: {plan}"

    def best(call, passes=50):
        for _ in range(3):
            call()
        return min(_timed(call) for _ in range(passes))

    assert best(lambda: gw.max_message_id(channel)) < 0.005
    assert best(lambda: gw.max_message_id()) < 0.005
    assert best(lambda: gw.history(channel, limit=500)) < 0.005
    assert len(gw.history(channel, limit=500)) == 500


def _timed(call) -> float:
    """CPU seconds THIS thread spent in `call`.

    Not `perf_counter`: a read that costs 0.6 ms of work reads as a second of wall clock
    when another thread in the process is spinning, which measures the suite and not the
    index. Every read here is served out of the page cache, so the CPU the thread spends
    is the whole of what the query costs.
    """
    started = time.thread_time()
    call()
    return time.thread_time() - started


def fill(gateway, channel_id, count):
    """`count` rows straight into the table: this is a fixture, not a posting path.

    One statement, generated inside SQLite by a recursive CTE, rather than 100,000 tuples
    built in Python and handed to `executemany`. The Python version takes seconds alone
    and minutes inside the full suite, because every row wants the GIL back and the rig's
    daemon threads are competing for it: a fixture that slow is a test that looks hung.
    """
    with gateway._write() as conn:
        start = gateway._next_id(conn)
        conn.execute(
            "WITH RECURSIVE seq(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM seq "
            "                          WHERE n + 1 < ?) "
            "INSERT INTO messages(id, channel_id, author_id, author_kind, author_display, "
            "                     content, timestamp) "
            "SELECT ? + n, ?, 'wh-filler', 'webhook', 'filler', 'line ' || n, ? FROM seq",
            (count, start, channel_id, "2026-09-12T00:00:00Z"))
        conn.execute("UPDATE meta SET value = ? WHERE key = 'next_id'",
                     (str(start + count - 1),))


# --------------------------------------------------------------------------- selection

def test_local_is_one_of_two_backends_and_open_gateway_returns_it(monkeypatch, tmp_path):
    assert gateway_mod.BACKENDS == ("local", "mock")
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "local")
    monkeypatch.setenv("MOXI_COMMS_LOCAL_RECORD", str(tmp_path / "elsewhere.db"))
    assert gateway_mod.backend_name() == "local"
    opened = gateway_mod.open_gateway(role="reader")
    try:
        assert isinstance(opened, LocalGateway)
        assert opened.path == tmp_path / "elsewhere.db"
        assert opened.role == "reader"           # accepted and ignored
        assert opened.retain_days == 90
    finally:
        opened.close()


def test_the_local_table_is_read_and_a_token_shaped_key_in_it_is_still_refused():
    write_config(None, 'backend = "local"\n\n[local]\nrecord = "/srv/record.db"\n'
                       'retain_days = 30\n')
    loaded = config.load()
    assert (loaded.backend, loaded.local.record, loaded.local.retain_days) == \
        ("local", "/srv/record.db", 30)
    env = dict(os.environ, MOXI_COMMS_LOCAL_RECORD="/srv/other.db")
    assert config.load(env=env).local.record == "/srv/other.db"

    write_config(None, '[local]\nrecord_token = "abc"\n')
    with pytest.raises(config.ConfigError, match="looks like a secret"):
        config.load()


@pytest.mark.parametrize("value", ['"forever"', "-1"])
def test_a_retain_days_that_is_not_a_count_of_days_is_a_refusal(value):
    write_config(None, f"[local]\nretain_days = {value}\n")
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert caught.value.key == "local.retain_days"


def test_the_retention_has_one_owner():
    """`[local] retain_days` is declared once, in the schema; the gateway's default is
    that declaration, not a second number."""
    from moxi_comms import local_gateway

    assert local_gateway.RETAIN_DAYS == config.LOCAL_RETAIN_DAYS == \
        config.schema().keys["local.retain_days"].default == 90


def test_the_cli_opens_the_local_record_in_process(monkeypatch, tmp_path):
    """Reads are direct WAL reads and writes are direct inserts, so the CLI works
    with the daemon down."""
    from moxi_comms import cli

    monkeypatch.setenv("MOXI_COMMS_LOCAL_RECORD", str(tmp_path / "record.db"))
    ctx = cli.Context(env={"MOXI_COMMS_GATEWAY": "local",
                           "MOXI_COMMS_LOCAL_RECORD": str(tmp_path / "record.db")})
    try:
        assert isinstance(ctx.gateway, LocalGateway)
    finally:
        ctx.gateway.close()


# ---------------------------------------------------------------------------

def test_the_daemon_refuses_a_gateway_flag_that_disagrees_with_the_box(monkeypatch):
    """Every adapter and the CLI resolve the backend with no flag, so a
    daemon holding a different record is a box that looks connected and delivers
    nothing."""
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "local")
    monkeypatch.setenv("MOXI_COMMS_MOCK_GATEWAY", "")
    with pytest.raises(SystemExit) as excinfo:
        daemon_mod.main(["--gateway", "mock"])
    assert "disagrees" in str(excinfo.value) and "local" in str(excinfo.value)


def test_the_daemon_takes_a_flag_that_agrees_and_mock_with_a_scratch_store(monkeypatch,
                                                                          tmp_path):
    """The exception: `mock` with a scratch store is the test and
    by-hand case, whatever the box is configured for."""
    reached: list = []

    def sentinel(*args, **kwargs):
        reached.append((args, kwargs))
        raise RuntimeError("open_gateway reached")

    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "local")
    monkeypatch.setenv("MOXI_COMMS_MOCK_GATEWAY", "")
    monkeypatch.setattr(gateway_mod, "open_gateway", sentinel)
    for argv in (["--gateway", "local"], ["--gateway", "mock", "--gateway-store",
                                          str(tmp_path / "store.json")], []):
        reached.clear()
        with pytest.raises(RuntimeError, match="open_gateway reached"):
            daemon_mod.main(argv)
        assert reached, f"{argv} should have got past the flag check"


def test_a_local_gateway_the_daemon_opens_delivers_through_the_seam(rig):
    """The whole point, said once on both backends: a post through the seam reaches the
    daemon's subscription and comes back out of the record."""
    reviewer = rig.shim(slot=2, name="reviewer", scope="proj-a")
    rig.post_from_slot(6, ["reviewer@proj-a"], "hello from the record")
    wait_until(lambda: len(reviewer.queue) >= 1)
    assert reviewer.queue[0]["message_id"] in [
        m.id for m in rig.gateway.history(rig.names_channel)]
