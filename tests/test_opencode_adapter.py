"""The `opencode` port, end to end against a fake OpenCode server: real daemon, real
sockets, the fake speaking the semantics measured on OpenCode 1.18.34
(`tests/fake_opencode.py`). Nothing here runs a model or touches the network."""

from __future__ import annotations

import itertools
import json
import os
import time

import pytest
from conftest import clock_reads_warm, cursor_of, frame_kinds, wait_until

from moxi_comms.adapters import opencode as oc
from moxi_comms.adapters import opencode_roster
from moxi_comms.adapters.opencode import OpenCodeAdapter
from moxi_comms.adapters.opencode_roster import OpenCodeAgent
from fake_opencode import FakeOpenCode


@pytest.fixture(autouse=True)
def _gate_reads_warm(monkeypatch):
    """This suite tests the opencode adapter's own mechanics, past the daemon's cold-cache
    gate. Under the cold-send rule the gate refuses every live message to an opencode agent whose cache it cannot read (the fake writes no record for the clock); the
    gate still runs here, on a clock that reads warm. `test_cold_every_sender.py` tests
    what it does with the real reading."""
    clock_reads_warm(monkeypatch, "opencode")

IDENTITY = "coder@proj-a"
PASSWORD = "test-password"


class Handle:
    """The fake as the adapter's server: already listening, never respawned."""

    def __init__(self, fake):
        self.fake = fake
        self.alive = True
        self.returncode = None

    def wait_listening(self, timeout=None):
        return self.fake.url

    def stop(self):
        pass


@pytest.fixture
def fake():
    server = FakeOpenCode(password=PASSWORD, default_model="opencode/minimax-m3.1-flash-free",
                          heartbeat=0.5).start()
    yield server
    server.stop()


@pytest.fixture
def launcher(rig, fake, tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "FLUSH_RETRY", 0.05)
    made: list = []

    def build(*, models=("opencode/minimax-m3.1-flash-free",), start=True, silence=None,
              directors=()):
        agent = OpenCodeAgent(qualified=IDENTITY, name="coder", scope="proj-a", slot=5,
                              cwd=str(tmp_path), session_file=opencode_roster.session_path(
                                  IDENTITY), models=list(models), directors=list(directors))
        statuses: list = []
        adapter = OpenCodeAdapter(agent, spawn=False, server=Handle(fake), password=PASSWORD,
                                  autoreconnect=True, backoff=itertools.repeat(0.05),
                                  clock=rig.clock, silence=silence,
                                  status_sink=lambda text: (statuses.append(text),
                                                            adapter._status(text)))
        adapter.statuses = statuses
        fake.on_log = adapter.on_log_line
        made.append(adapter)
        if start:
            adapter.start()
        return adapter

    yield build
    for adapter in made:
        adapter.stop()


def send(rig, text, *, to=IDENTITY, slot=1, name="main", scope="proj-a"):
    code, out = rig.cli(["send", "--posture", "none", to, text], slot=slot, name=name, scope=scope)
    assert code == 0, out


def posts(fake):
    return [(path, body) for method, path, body in fake.requests
            if method == "POST" and path.endswith("/prompt_async")]


def session_of(adapter):
    return adapter.session_id


# --------------------------------------------------------------------------- binding

def test_it_registers_with_the_pinned_slot_and_the_opencode_kind(rig, launcher):
    adapter = launcher()
    binding = rig.daemon.bindings[IDENTITY]
    assert (binding.adapter, binding.slot_id) == ("opencode", 5)
    assert adapter.identity == IDENTITY


def creates(fake) -> list:
    return [r for r in fake.requests if r[:2] == ("POST", "/session")]


def test_a_first_launch_records_its_session_and_a_relaunch_creates_a_new_one(
        rig, launcher, fake):
    """An OpenCode launch is a new session, as a Claude launch is."""
    first = launcher()
    sid = first.session_id
    assert sid.startswith("ses")
    record = oc.read_record(opencode_roster.session_path(IDENTITY))
    assert record["session_id"] == sid and record["launch"]
    first.stop()
    assert wait_until(lambda: IDENTITY not in rig.daemon.bindings)
    second = launcher()
    assert second.session_id != sid
    assert len(creates(fake)) == 2, "two launches, two sessions"
    assert ("GET", f"/session/{sid}") not in [r[:2] for r in fake.requests], \
        "the relaunch never asked the server for the recorded session"
    again = oc.read_record(opencode_roster.session_path(IDENTITY))
    assert again["session_id"] == second.session_id
    assert again["launch"] and again["launch"] != record["launch"]


@pytest.mark.parametrize("stale", ["ses_ef0000000000000000000000000", "garbage"])
def test_a_stale_session_file_is_never_read_for_an_id(rig, launcher, fake, monkeypatch,
                                                       stale, capsys):
    """The record rule: with the file naming a gone `ses_` id, or garbage, the launch
    asks nothing about it, creates its own session, and never puts the stale id in a
    request path, a status line, the register report, the plan or a window argv. Its
    first write clears the stale id before its own `POST /session`."""
    from types import SimpleNamespace

    from moxi_comms.adapters.opencode_window import window_argv

    path = opencode_roster.session_path(IDENTITY)
    oc.update_record(path, session_id=stale, launch="1@earlier")
    adapter = launcher(start=False)
    reports: list = []
    real_report = adapter.session_report
    adapter.session_report = lambda *, register: (
        lambda r: reports.append(r) or r)(real_report(register=register))
    at_create: list = []
    real_load = adapter.load_session
    adapter.load_session = lambda: at_create.append(oc.read_record(path)) or real_load()
    oc.print_plan(adapter.agent, SimpleNamespace(source="roster.toml"), binary="/b/oc",
                  version="0", comms_path="/b/comms")
    adapter.start()
    plan = capsys.readouterr().out
    sid = adapter.session_id
    assert sid != stale and len(creates(fake)) == 1
    assert at_create[0].get("session_id") is None and at_create[0].get("launch") is None, \
        "the launch's first write cleared the earlier session before its own was made"
    assert not any(stale in path for _, path, _ in fake.requests)
    argv = window_argv("/b/oc", adapter.generation.url, adapter.generation.thread_id)
    seen = [*adapter.statuses, plan, " ".join(argv), repr(reports),
            json.dumps(oc.status_lines(path))]
    assert not [line for line in seen if stale in line]
    assert reports[0]["new_session"] is True and "session_id" not in reports[0]
    assert "(a new session at this launch)" in plan
    assert oc.read_record(path)["session_id"] == sid


def restart_server(adapter, fake, monkeypatch) -> None:
    """The launch's server dies and the supervisor starts it again (on the same fake,
    which keeps its sessions as OpenCode's database would)."""

    class Restarted(Handle):
        def start(self):
            pass

    monkeypatch.setattr(adapter, "_new_server", lambda model: Restarted(fake))
    adapter.spawn = True
    adapter.server.alive = False


def test_a_server_restart_keeps_the_launchs_session(rig, launcher, fake, monkeypatch):
    adapter = launcher()
    sid = adapter.session_id
    first = adapter.generation
    restart_server(adapter, fake, monkeypatch)
    assert wait_until(lambda: adapter.generation is not first and adapter.ready, 5.0)
    assert adapter.session_id == sid and adapter.generation.thread_id == sid
    assert len(creates(fake)) == 1, "no second session"
    send(rig, "after the restart")
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)


def test_a_session_gone_after_a_server_restart_closes_the_launchs_posts(
        rig, launcher, fake, monkeypatch):
    """Inside a launch, the launch's session gone is loud and never replaced."""
    adapter = launcher()
    sid = adapter.session_id
    with fake.lock:
        fake.sessions.pop(sid)
    restart_server(adapter, fake, monkeypatch)
    assert wait_until(lambda: adapter.fatal, 5.0)
    assert "with 404 (this launch's session is gone)" in adapter.fatal
    assert "this launch's session is no longer on the server" in adapter.fatal
    assert any("this launch's session is gone" in s for s in adapter.statuses)
    assert not adapter.can_inject()
    assert len(creates(fake)) == 1, "no replacement session"
    assert adapter.session_id == sid


# --------------------------------------------------------------------------- delivery

def test_mail_is_posted_under_comms_id_and_the_cursor_moves_on_the_idle_after_stop(
        rig, launcher, fake):
    adapter = launcher()
    send(rig, "hello coder")
    assert wait_until(lambda: posts(fake), 5.0)
    path, body = posts(fake)[0]
    assert path == f"/session/{adapter.session_id}/prompt_async"
    assert set(body) == {"messageID", "parts"}, "a post names no model, ever"
    assert body["messageID"].startswith("msg_") and len(body["messageID"]) == 30
    assert "hello coder" in body["parts"][0]["text"]
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)
    assert adapter.can_inject()


def test_the_post_follows_an_idle_status_read_and_a_busy_session_gets_no_post(
        rig, launcher, fake):
    """The status is read immediately before each post, and only an idle session is
    posted into. A session running a turn of the person's own gets nothing until it is
    idle again."""
    fake.auto = False
    adapter = launcher()
    sid = adapter.session_id
    fake.prompt(sid, oc.mint_message_id(), text="his own prompt")     # he is mid-turn
    assert wait_until(lambda: adapter.busy)
    send(rig, "while he works")
    time.sleep(0.4)
    assert posts(fake) == []
    fake.finish(sid)
    assert wait_until(lambda: posts(fake), 5.0)
    reads = [i for i, r in enumerate(fake.requests) if r[1] == "/session/status"]
    post_at = next(i for i, r in enumerate(fake.requests) if r[1].endswith("prompt_async"))
    assert reads and max(i for i in reads if i < post_at) == post_at - 1, \
        "the status read is the request just before the post"


@pytest.mark.parametrize("status,raw", [(500, "{}"), (200, ""), (200, "not json"),
                                        (200, "[]"), (401, "")])
def test_anything_but_a_valid_idle_200_is_read_as_not_idle(rig, launcher, fake, status, raw):
    adapter = launcher()
    fake.status_answers = [(status, raw)]
    send(rig, "one")
    assert wait_until(lambda: posts(fake), 5.0), "the next read was idle, and it went"
    assert fake.status_answers == []
    statuses = [r for r in fake.requests if r[1] == "/session/status"]
    assert len(statuses) >= 3, "the first read was refused as not idle, then read again"


def test_one_digest_per_turn_comms_starts(rig, launcher, fake):
    """From comms' own post until that turn ends, nothing more is posted; mail that
    arrives meanwhile goes as the next digest."""
    fake.auto = False
    adapter = launcher()
    send(rig, "first")
    assert wait_until(lambda: len(posts(fake)) == 1, 5.0)
    send(rig, "second")
    send(rig, "third")
    time.sleep(0.4)
    assert len(posts(fake)) == 1
    assert not adapter.can_inject()
    fake.finish(adapter.session_id)
    assert wait_until(lambda: len(posts(fake)) == 2, 5.0)
    text = posts(fake)[1][1]["parts"][0]["text"]
    assert "second" in text and "third" in text


def test_his_prompt_into_a_digests_turn_still_completes_the_digest(rig, launcher, fake):
    """He submits while the digest's turn runs: his prompt takes the turn over, the
    digest's assistant message ends at tool-calls with no reply of its own, the stop is
    on the reply to him, and the digest completes on that idle."""
    fake.auto = False
    adapter = launcher()
    send(rig, "digest")
    assert wait_until(lambda: posts(fake), 5.0)
    assert wait_until(lambda: any(p.seen for p in adapter.pending.values()))
    fake.prompt(adapter.session_id, oc.mint_message_id(), text="his own")
    fake.finish(adapter.session_id)
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)


def test_a_turn_that_ends_in_an_error_moves_no_cursor_and_releases_the_session(
        rig, launcher, fake, caplog):
    fake.auto = False
    adapter = launcher()
    send(rig, "doomed")
    assert wait_until(lambda: posts(fake), 5.0)
    fake.error(adapter.session_id)
    assert wait_until(lambda: adapter.can_inject(), 5.0)
    assert cursor_of(IDENTITY) is None
    assert "no cursor advance" in caplog.text


def test_an_idle_before_the_model_saw_the_digest_moves_no_cursor(rig, launcher, fake):
    """The safe corner: a digest whose turn never made an assistant message for it is
    in the model's context, but nothing counts it as seen, so it replays."""
    fake.auto = False
    adapter = launcher()
    sid = adapter.session_id
    original = fake.prompt

    def admit_only(sid_, mid, body=None, **kw):
        fake._message(sid_, {"id": mid, "role": "user", "sessionID": sid_,
                             "time": {"created": int(time.time() * 1000)}}, "x")
        fake.go_idle(sid_)

    fake.prompt = admit_only
    send(rig, "unseen")
    assert wait_until(lambda: posts(fake), 5.0)
    assert wait_until(lambda: adapter.can_inject(), 5.0)
    assert cursor_of(IDENTITY) is None
    fake.prompt = original
    assert sid



def test_a_posted_digest_is_not_posted_again_after_a_daemon_restart(rig, launcher, fake):
    """The digest is posted and its turn runs when the daemon
    restarts. The reconnect lists its id as `holding`, so the replay does not queue it
    again, and once the turn ends and the cursor moves it has been posted once."""
    from moxi_comms import daemon as daemon_mod
    fake.auto = False
    adapter = launcher()
    send(rig, "M2-OC")
    assert wait_until(lambda: len(posts(fake)) == 1, 5.0)

    rig.daemon.stop()
    assert wait_until(lambda: adapter.offline, 5.0)
    rig.daemon = daemon_mod.Daemon(rig.gateway, clock=rig.clock,
                                   start_trim_thread=False).start()
    assert wait_until(lambda: not adapter.offline and not adapter.replaying, 5.0)
    time.sleep(0.3)
    assert adapter.queue == [], "the replay handed nothing the adapter holds"

    fake.finish(adapter.session_id)                 # the digest's turn ends well
    high = max(int(m.id) for m in rig.gateway.history(rig.names_channel))
    assert wait_until(lambda: cursor_of(IDENTITY) == str(high), 5.0)
    time.sleep(0.5)
    carried = [b["parts"][0]["text"].count("M2-OC") for _, b in posts(fake)]
    assert carried == [1], f"posted once, not {carried}"
    assert not adapter.held_ids


def test_a_post_lost_with_its_server_replays_at_the_next_registration(
        rig, launcher, fake, monkeypatch):
    """On OpenCode, the server dies under a running post. The restarted
    server's reconcile finds the session idle and the turn not ended well, settles the
    post as not completed and lets its ids go, so the next registration hands the
    message again."""
    from moxi_comms import daemon as daemon_mod
    fake.auto = False
    adapter = launcher()
    send(rig, "CRASH-OC")
    assert wait_until(lambda: len(posts(fake)) == 1, 5.0)
    assert wait_until(lambda: adapter.held_ids, 5.0)
    with fake.lock:
        fake.sessions[adapter.session_id]["status"] = "idle"   # the process is gone
    first = adapter.generation
    restart_server(adapter, fake, monkeypatch)
    assert wait_until(lambda: adapter.generation is not first and adapter.ready, 5.0)
    assert wait_until(lambda: not adapter.pending and not adapter.held_ids, 5.0)
    assert cursor_of(IDENTITY) is None

    rig.daemon.stop()
    assert wait_until(lambda: adapter.offline, 5.0)
    rig.daemon = daemon_mod.Daemon(rig.gateway, clock=rig.clock,
                                   start_trim_thread=False).start()
    assert wait_until(lambda: len(posts(fake)) == 2, 5.0), "the lost message was posted again"
    assert "CRASH-OC" in posts(fake)[-1][1]["parts"][0]["text"]

# --------------------------------------------------------------------------- refusals

@pytest.mark.parametrize("code,meaning", [
    (400, "a request the server refuses"), (401, "the server password did not match"),
    (403, "forbidden"), (404, "this launch's session is gone"),
    (409, "a refusal a retry cannot fix"), (413, "a refusal a retry cannot fix"),
    (302, "a refusal a retry cannot fix")])
def test_a_refusal_a_retry_cannot_fix_closes_the_session_for_the_launch_loudly(
        rig, launcher, fake, code, meaning):
    adapter = launcher()
    fake.post_answers = [(code, {"name": "Refused", "data": {"message": "nope"}})]
    send(rig, "refused")
    assert wait_until(lambda: adapter.fatal, 5.0)
    assert not adapter.can_inject()
    said = [s for s in adapter.statuses if "refused a post" in s]
    assert len(said) == 1 and f"with {code} ({meaning})" in said[0]
    assert ("refused again after a relaunch" in said[0]) == (code == 400)
    send(rig, "more")
    time.sleep(0.4)
    assert len(posts(fake)) == 1, "no retry after a refusal"
    assert cursor_of(IDENTITY) is None
    assert oc.status_lines(opencode_roster.session_path(IDENTITY))[-1]["text"] == said[0]


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_a_failure_a_retry_can_fix_is_retried_and_a_run_of_them_is_said_once(
        rig, launcher, fake, code):
    adapter = launcher()
    fake.post_answers = [(code, {})] * (oc.FAILURE_STREAK + 2)
    send(rig, "persistent")
    assert wait_until(lambda: len(posts(fake)) == oc.FAILURE_STREAK + 3, 10.0)
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)
    said = [s for s in adapter.statuses if "posts in a row" in s]
    assert len(said) == 1 and f"({code})" in said[0]
    assert adapter.fatal is None


def test_a_post_that_cannot_connect_is_put_back(launcher):
    adapter = launcher(start=False)
    adapter.http = oc.Http("http://127.0.0.1:9", PASSWORD, timeout=1.0)
    adapter.session_id = "ses_x"
    assert adapter.deliver("t", 5, IDENTITY, 1) is oc.REQUEUE


def test_a_post_with_no_answer_is_a_receipt_and_the_messages_decide(rig, launcher, fake,
                                                                    monkeypatch):
    adapter = launcher()
    real = adapter.http.request

    def lost_answer(method, path, body=None, timeout=None):
        if path.endswith("/prompt_async"):
            real(method, path, body, timeout)          # admitted...
            raise oc.NoAnswer("timed out")               # ...but the answer is lost
        return real(method, path, body, timeout)

    monkeypatch.setattr(adapter.http, "request", lost_answer)
    send(rig, "answer lost")
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)
    assert len(posts(fake)) == 1, "an admitted id is never posted again"


# --------------------------------------------------------------------------- the stream

def test_a_silent_stream_counts_as_cut_and_is_reconnected_and_reconciled(
        rig, launcher, fake):
    adapter = launcher(silence=0.6)
    first = adapter.stream
    closed_ready: list = []
    real_close = adapter._on_stream_close

    def on_close(reason):
        real_close(reason)
        closed_ready.append((reason, adapter.ready, adapter.can_inject()))

    adapter._on_stream_close = on_close
    fake.stalled = True
    assert wait_until(lambda: first.closed.is_set(), 3.0)
    assert first.reason == "no event for 0.6s"
    assert closed_ready[0][1:] == (False, False), "not ready from the detection on"
    fake.stalled = False
    assert wait_until(lambda: adapter.ready and adapter.stream is not first, 5.0)
    send(rig, "after the stall")
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)


def test_a_turn_that_ends_while_the_stream_is_cut_is_settled_by_the_reconcile(
        rig, launcher, fake):
    fake.auto = False
    adapter = launcher()
    send(rig, "cut")
    assert wait_until(lambda: any(p.seen for p in adapter.pending.values()), 5.0)
    fake.stalled = True                    # nothing reaches the launcher...
    fake.finish(adapter.session_id)        # ...while the turn ends
    fake.stalled = False
    fake.close_streams()
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)
    assert adapter.can_inject()


# --------------------------------------------------------------------------- the model

def test_a_request_on_another_model_is_logged_and_shown_and_nothing_more(
        rig, launcher, fake):
    adapter = launcher()
    sid = adapter.session_id
    fake.sessions[sid]["model"] = "opencode/big-pickle"          # his pick in the window
    send(rig, "on his model")
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)
    said = [s for s in adapter.statuses if "big-pickle" in s]
    assert len(said) == 1, "said once per model and session"
    assert "not on the launch default opencode/minimax-m3.1-flash-free" in said[0]
    assert "(small=false)" in said[0], "the request line, read first, says which slot"
    assert adapter.fatal is None and adapter.can_inject()
    assert "big-pickle" in oc.status_lines(opencode_roster.session_path(IDENTITY))[-1]["text"]
    send(rig, "again")
    assert wait_until(lambda: len(posts(fake)) == 2, 5.0)
    assert len([s for s in adapter.statuses if "big-pickle" in s]) == 1


def test_a_side_request_on_the_default_says_nothing(launcher, fake):
    adapter = launcher()
    fake.create_session(None)          # an untitled session: its title is a side request
    time.sleep(0.2)
    assert not [s for s in adapter.statuses if "ran on" in s]


def test_an_answer_with_no_request_line_before_it_is_said(rig, launcher, fake):
    adapter = launcher()
    fake.on_log = lambda line: None        # the log shows no request
    send(rig, "unseen request")
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)
    said = [s for s in adapter.statuses if "no request line" in s]
    assert len(said) == 1, "one note for one assistant message, though its final " \
                           "update arrives twice"


def test_the_watch_counts_one_request_per_assistant_message(launcher):
    adapter = launcher(start=False)
    adapter.default_model = "opencode/m"
    adapter.agent.qualified = IDENTITY
    line = ("timestamp=x level=INFO run=r message=stream providerID=opencode modelID=m "
            "session.id=ses_1 small=false agent=build mode=primary\n")
    now = int(time.time() * 1000) + 10

    def done(mid):
        return {"role": "assistant", "id": mid, "sessionID": "ses_1",
                "providerID": "opencode", "modelID": "m",
                "time": {"created": now, "completed": now + 1}}

    adapter.on_log_line(line)
    adapter._watch_message(done("msg_a"))
    adapter._watch_message(done("msg_a"))           # the final update, sent twice
    adapter.on_log_line(line)
    adapter._watch_message(done("msg_b"))           # a second step: its own request
    assert adapter.model_notes == []
    old = dict(done("msg_old"), time={"created": 1, "completed": 2})
    adapter._watch_message(old)                     # re-read from before the launch
    assert adapter.model_notes == []


def test_with_no_default_every_model_is_shown(launcher):
    adapter = launcher(start=False)
    adapter.default_model = None
    adapter.on_log_line("timestamp=x level=INFO message=stream providerID=p modelID=q "
                        "session.id=ses_9 small=true agent=title mode=primary\n")
    assert adapter.model_notes == ["a request in session ses_9 ran on p/q (small=true); "
                                   "no launch default is set"]


# --------------------------------------------------------------------------- the launch

def test_the_launch_default_is_the_rosters_model_when_it_is_offered_free():
    assert oc.choose_default(["opencode/a"], ["opencode/a", "opencode/b"]) == \
        ("opencode/a", None)


def test_a_missing_default_falls_to_the_rosters_next_then_to_the_first_free_zen_model():
    model, notice = oc.choose_default(["opencode/a", "local/x"], ["local/x", "opencode/b"])
    assert model == "local/x" and "opencode/a is not offered" in notice
    model, notice = oc.choose_default(["opencode/a"], ["local/y", "opencode/b"])
    assert model == "opencode/b", "an automatic fallback is a Zen model, never another"
    assert "not the zero-retention one" in notice


def test_with_no_free_model_offered_none_is_set_and_it_is_said():
    assert oc.choose_default(["opencode/a"], ["local/y"]) == (None, oc.NO_FREE_MODEL)
    assert oc.NO_FREE_MODEL == ("no free model offered: OpenCode will use its own "
                                "default, which may be a paid model if your own "
                                "credentials allow one")


def test_only_models_whose_every_price_is_zero_are_free():
    listing = {"providers": [{"id": "opencode", "models": {
        "free": {"cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}},
        "paid": {"cost": {"input": 1, "output": 3, "cache": {"read": 0, "write": 0}}},
        "cached": {"cost": {"input": 0, "output": 0, "cache": {"read": 0.1, "write": 0}}},
        "unknown": {}}}]}
    assert oc.free_models(listing) == ["opencode/free"]


SECRET_WORDS = ("key", "token", "secret", "password", "credential", "auth", "apikey")


def keys_of(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{prefix}{k}"
            yield from keys_of(v, f"{prefix}{k}.")
    elif isinstance(value, list):
        for v in value:
            yield from keys_of(v, prefix)


@pytest.mark.parametrize("model", ["opencode/minimax-m3.1-flash-free", None])
def test_the_launch_config_names_only_a_free_model_and_carries_no_credential(model):
    """Comms never selects or enables a paid model and adds no credential, key or
    provider entry: the launch config names the chosen free model on both slots, or
    none, layer one, and the two permission answers, and nothing else."""
    config = oc.launch_config(model, "/run/x/layer-one.md")
    keys = set(keys_of(config))
    assert not any(word in k.lower() for k in keys for word in SECRET_WORDS)
    assert "provider" not in config and "enabled_providers" not in config
    expected = {"permission", "instructions"} | ({"model", "small_model"} if model else set())
    assert set(config) == expected
    if model:
        assert config["model"] == config["small_model"] == model


def test_every_model_comms_names_is_one_the_server_offers_free(rig, launcher, fake):
    """The chosen default comes only from the free listing, the server's environment
    adds the password and the launch config only, and no post names a model."""
    fake.free = ["opencode/big-pickle"]
    fake.paid = ["opencode/minimax-m3.1-flash-free"]      # named by the roster, but not free
    adapter = launcher()
    assert adapter.default_model == "opencode/big-pickle"
    env = adapter.server_env(adapter.default_model)
    inherited = {k for k in os.environ if k.startswith("OPENCODE_")}
    added = {k for k in env if k.startswith("OPENCODE_")} - inherited
    assert added == {"OPENCODE_SERVER_PASSWORD", "OPENCODE_CONFIG_CONTENT"}
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] in fake.free
    send(rig, "post")
    assert wait_until(lambda: posts(fake), 5.0)
    assert all("model" not in body for _, body in posts(fake))


def test_the_launch_notice_is_printed_and_held_before_the_window(launcher, fake, capsys):
    fake.free = ["opencode/big-pickle"]
    adapter = launcher()
    started = time.monotonic()
    oc.show_notice(adapter, 0.3)
    assert time.monotonic() - started >= 0.3
    err = capsys.readouterr().err
    assert "opencode/minimax-m3.1-flash-free is not offered" in err
    assert "the window opens in 0.3s" in err
    assert any("is not offered" in s for s in adapter.statuses)


def test_a_launch_on_the_default_prints_no_notice(launcher, capsys):
    adapter = launcher()
    oc.show_notice(adapter, 0.3)
    assert capsys.readouterr().err == ""


def test_the_status_line_is_kept_in_the_session_file_and_explain_shows_it(rig, launcher):
    adapter = launcher()
    adapter.status_sink("coder@proj-a: a line he should see")
    code, out = rig.cli(["explain", IDENTITY], slot=1, name="main", scope="proj-a")
    assert code == 0, out
    assert "moxi-comms-opencode status" in out
    assert "a line he should see" in out


def test_the_status_lines_survive_a_relaunch_while_the_session_is_replaced(
        rig, launcher):
    path = opencode_roster.session_path(IDENTITY)
    first = launcher()
    first.status_sink("coder@proj-a: said in the first launch")
    sid = first.session_id
    first.stop()
    assert wait_until(lambda: IDENTITY not in rig.daemon.bindings)
    second = launcher()
    assert oc.read_record(path)["session_id"] == second.session_id != sid
    assert any("said in the first launch" in row["text"] for row in oc.status_lines(path))


# ---------------------------------------------------------- what the harness reports

def turn_frames(frames):
    return frame_kinds(frames, "turn")


def test_an_operator_turn_sends_activity_at_busy_and_one_turn_at_its_idle(
        rig, launcher, fake, frames):
    """Busy is the turn start (no-kind activity), the idle after it the turn end,
    whoever prompted; the server's two idle events make one frame."""
    adapter = launcher()
    before = len(frames)
    fake.control("type", {"session": adapter.session_id, "text": "his own"})
    assert wait_until(lambda: turn_frames(frames[before:]), 5.0)
    time.sleep(0.3)
    ours = frames[before:]
    assert "kind" not in ours[0], "busy: a no-kind activity first"
    turns = turn_frames(ours)
    assert len(turns) == 1, "one turn frame per idle"
    assert turns[0]["handled"] == [] and turns[0]["unhandled"] == []
    assert turns[0]["session_id"] == adapter.session_id
    assert cursor_of(IDENTITY) is None


def test_a_digests_turn_is_handled_on_the_frame_after_the_cursor_write(
        rig, launcher, fake, frames):
    adapter = launcher()
    send(rig, "handle me")
    assert wait_until(lambda: turn_frames(frames) and cursor_of(IDENTITY), 5.0)
    high = max(int(m.id) for m in rig.gateway.history(rig.names_channel))
    turn = turn_frames(frames)[-1]
    assert turn["handled"] == [str(high)] and turn["unhandled"] == []
    assert turn["cursor_then"] == str(high), "the cursor is written before the frame"
    assert not adapter.held_ids


def test_a_digests_turn_that_errs_is_unhandled_and_moves_no_cursor(
        rig, launcher, fake, frames):
    fake.auto = False
    adapter = launcher()
    send(rig, "doomed")
    assert wait_until(lambda: posts(fake), 5.0)
    held = set(adapter.held_ids)
    fake.error(adapter.session_id)
    assert wait_until(lambda: turn_frames(frames), 5.0)
    turn = turn_frames(frames)[-1]
    assert turn["handled"] == [] and set(turn["unhandled"]) == held
    assert cursor_of(IDENTITY) is None
    assert wait_until(lambda: not adapter.held_ids, 2.0), "let go once the frame is sent"


def test_an_aborted_turn_sends_no_turn_and_its_ids_go_unhandled_on_the_next(
        rig, launcher, fake, frames):
    """The window's Esc mirrors Claude Code, whose Stop does not run on a user
    interrupt: no `turn` at the abort's idle; the digest's ids stay held and are
    `unhandled` on the next turn frame."""
    fake.auto = False
    adapter = launcher()
    send(rig, "abort me")
    assert wait_until(lambda: any(p.seen for p in adapter.pending.values()), 5.0)
    held = set(adapter.held_ids)
    fake.abort(adapter.session_id)
    assert wait_until(lambda: not adapter.pending, 5.0)
    time.sleep(0.3)
    assert turn_frames(frames) == []
    assert set(adapter.unhandled_owed) == held and adapter.held_ids == held
    fake.control("type", {"session": adapter.session_id, "text": "next"})
    fake.finish(adapter.session_id)
    assert wait_until(lambda: turn_frames(frames), 5.0)
    assert set(turn_frames(frames)[0]["unhandled"]) == held
    assert wait_until(lambda: not adapter.held_ids, 2.0)
    assert cursor_of(IDENTITY) is None


def test_compaction_sends_ready_compact_and_leaves_busy(rig, launcher, fake, frames):
    """An automatic compaction runs inside the busy loop."""
    fake.auto = False
    adapter = launcher()
    fake.control("type", {"session": adapter.session_id, "text": "long turn"})
    assert wait_until(lambda: adapter.busy, 5.0)
    fake.compacted(adapter.session_id)
    assert wait_until(lambda: [f for f in frame_kinds(frames, "ready")
                               if f.get("source") == "compact"], 5.0)
    assert adapter.busy, "busy untouched"
    assert turn_frames(frames) == [], "a compaction is not a turn end"


def test_ready_startup_once_and_none_on_a_server_restart(
        rig, launcher, fake, monkeypatch, frames):
    """The launch's own session is `ready source=startup`; a server restart's
    report refreshes are no-kind activity, never `ready`."""
    adapter = launcher()
    ready = frame_kinds(frames, "ready")
    assert [f["source"] for f in ready] == ["startup"]
    assert ready[0]["session_id"] == adapter.session_id
    first = adapter.generation
    n = len(frames)
    restart_server(adapter, fake, monkeypatch)
    assert wait_until(lambda: adapter.generation is not first and adapter.ready, 5.0)
    assert wait_until(lambda: len(frames) >= n + 2, 5.0)
    assert frame_kinds(frames, "ready") == ready, "no ready on the restart"


def test_a_window_new_is_followed_and_a_child_session_is_not(
        rig, launcher, fake, frames):
    """A top-level `session.created` the launcher did not make is the
    window's /new or /clear: rebind, session file, then `ready source=clear` naming the
    new session; mail goes there. A child session is not followed."""
    adapter = launcher()
    old = adapter.session_id
    fake.control("new-session", {"parent": old})              # a subagent's child
    time.sleep(0.3)
    assert adapter.session_id == old
    created = fake.control("new-session", {})["session"]
    assert wait_until(lambda: adapter.session_id == created, 5.0)
    clear = [f for f in frame_kinds(frames, "ready") if f.get("source") == "clear"]
    assert len(clear) == 1 and clear[0]["session_id"] == created
    record = oc.read_record(opencode_roster.session_path(IDENTITY))
    assert record["session_id"] == created
    assert adapter.generation.thread_id == created
    send(rig, "after the clear")
    assert wait_until(lambda: posts(fake), 5.0)
    assert posts(fake)[-1][0] == f"/session/{created}/prompt_async"
    assert wait_until(lambda: cursor_of(IDENTITY) is not None, 5.0)


def test_a_post_pending_at_the_follow_goes_again_into_the_new_session(
        rig, launcher, fake, frames):
    """The old session's pending post goes back in the queue, its ids still held: a
    re-push of its id is dropped, and it is posted again into the new session."""
    fake.auto = False
    adapter = launcher()
    old = adapter.session_id
    send(rig, "carry me")
    assert wait_until(lambda: adapter.pending, 5.0)
    held = set(adapter.held_ids)
    mid = next(iter(held))
    created = fake.control("new-session", {})["session"]
    assert wait_until(lambda: len(posts(fake)) == 2, 5.0)
    assert posts(fake)[0][0] == f"/session/{old}/prompt_async"
    assert posts(fake)[1][0] == f"/session/{created}/prompt_async"
    assert "carry me" in posts(fake)[1][1]["parts"][0]["text"]
    assert adapter.held_ids == held, "still held across the follow"
    adapter._on_push({"op": "deliver", "payload": {"message_id": mid, "text": "again"}})
    assert adapter.queue == [], "a re-push of a held id is dropped"
    fake.finish(adapter.session_id)
    assert wait_until(lambda: cursor_of(IDENTITY) == max(held, key=int), 5.0)
    assert set(turn_frames(frames)[-1]["handled"]) == held


def test_a_compaction_while_the_daemon_is_away_is_reported_after_the_register(
        rig, launcher, fake, frames):
    """The `ready` is owed and sent once, right after the next register's
    answer."""
    from moxi_comms import daemon as daemon_mod
    adapter = launcher()
    rig.daemon.stop()
    assert wait_until(lambda: adapter.offline, 5.0)
    fake.compacted(adapter.session_id)
    time.sleep(0.3)
    assert not [f for f in frame_kinds(frames, "ready") if f.get("source") == "compact"]
    rig.daemon = daemon_mod.Daemon(rig.gateway, clock=rig.clock,
                                   start_trim_thread=False).start()
    assert wait_until(lambda: [f for f in frame_kinds(frames, "ready")
                               if f.get("source") == "compact"], 5.0)
    time.sleep(0.3)
    assert len([f for f in frame_kinds(frames, "ready") if f.get("source") == "compact"]) == 1
