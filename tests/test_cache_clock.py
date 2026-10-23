"""The cache clock for codex and opencode bindings, at comms' daemon, verb and launchers.

The readers themselves (a Codex thread's rollout, an OpenCode session's rows, the lifetime
table, the drift record) are the cache module's, and their pure reads are tested in its
suite. What stays here is what drives comms: the daemon's gate and its
`session` answer over each verdict, the hold and `moxi comms cache` lines, a lowering the
daemon's learning store writes and the words comms logs about it, and the launchers'
reports (the evidence cache's read takes). comms reaches cache only through its seam; a
test reaches it only through `conftest.cache_api()`.

The daemon and verb cases run
on both backends (BOTH_BACKENDS). Fixture rollouts are cut from the shapes of the probe
rollouts of 2026-10-05; the fixture db is built from OpenCode 1.18.34's schema, not copied
from a live db. Every read here is against tmp paths. The lifetime table is cache's
shipped one: gpt-5.6-sol on its measured row (5400 s, floor 1800 s), the other GPT-5.6
and later models on 30 minutes, Anthropic on 5 minutes.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import cache_api, record_rows, wait_until, write_transcript

from moxi_comms import cache, cli, codex_threads, paths
from moxi_comms.cache import CacheState
from moxi_comms.client import DaemonClient
from moxi_comms.paths import daemon_socket

NOW = 1_791_300_000.0                    # a fixed "now" for the fixtures
TID = "01a10dd9-838a-7000-a304-0c5e7fa618ca"
TID_B = "01a10dd9-838a-7000-a304-0c5e7fa618cb"
OTHER = "01a10dd9-0000-7000-a304-0c5e7fa61800"
SID = "ses_ef224518dffeZCQ7Clh1XRluXp"
SID_B = "ses_ef224518dffeZCQ7Clh1XRluXq"
CLAUDE_SID = "aaaaaaaa-1111-4111-8111-111111111111"

#: cache's shipped measured row for codex openai/gpt-5.6-sol, and its floor.
MEASURED_SECONDS = 5400
MEASURED_FLOOR = 1800
MEASURED_CHECKED = "2026-10-06"
#: cache's rule for a lowering: 80% of the gap, down to the minute, never below the floor.
DRIFT_LOWER_TO = 0.8


def measured_seconds() -> int:
    return MEASURED_SECONDS


def lower_to(gap: float, floor: int = MEASURED_FLOOR) -> int:
    return max(int(floor), int(gap * DRIFT_LOWER_TO) // 60 * 60)


def drift_record_path(rig) -> Path:
    """cache's drift record (STATE/cache/drift.json under the rig's cache home), as the
    daemon's learning store names it."""
    return Path(rig.daemon.cache_store.path)


# =================================================================== rollout fixtures

def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def meta_payload(tid=TID, **over) -> dict:
    payload = {"session_id": tid, "id": tid, "timestamp": iso(NOW - 86400),
               "cwd": "/tmp/work", "originator": "moxi-comms-codex", "cli_version": "0.153.0",
               "source": "vscode", "thread_source": "user", "model_provider": "openai",
               "base_instructions": {"text": "You are Codex."},
               "history_mode": "paginated", "multi_agent_version": 1,
               "context_window": 258400}
    payload.update(over)
    return {k: v for k, v in payload.items() if v is not ...}


class Rollout:
    """A rollout, line by line, in Codex 0.153.0's shapes."""

    def __init__(self, tid=TID, **meta):
        self.tid = tid
        self.lines = [{"timestamp": iso(NOW - 86400), "ordinal": 0, "type": "session_meta",
                       "payload": meta_payload(tid, **meta)}]
        self.raw_tail = None

    def add(self, kind, t, payload):
        self.lines.append({"timestamp": iso(t), "ordinal": len(self.lines), "type": kind,
                           "payload": payload})
        return self

    def event(self, sub, t, **payload):
        return self.add("event_msg", t, {"type": sub, **payload})

    def item(self, sub, t, **payload):
        return self.add("response_item", t, {"type": sub, **payload})

    def start(self, turn, t):
        return self.event("task_started", t, turn_id=turn, started_at=int(t))

    def context(self, turn, t, model="gpt-5.6-terra"):
        return self.add("turn_context", t, {"turn_id": turn, "model": model,
                                            "cwd": "/tmp/work"})

    def tsa(self, t, model="gpt-5.6-terra", provider="openai", thread=...):
        thread = self.tid if thread is ... else thread
        payload = {"type": "thread_settings_applied",
                   "thread_settings": {"model": model, "model_provider_id": provider}}
        if thread is not None:
            payload["thread_id"] = thread
        return self.add("event_msg", t, payload)

    def record(self, turn, t, *, inp=20_000, cached=19_500, session=..., thread=...,
               rid=None):
        return self.add("token_usage_record", t, {
            "thread_id": self.tid if thread is ... else thread, "turn_id": turn,
            "session_id": self.tid if session is ... else session,
            "response_id": rid or f"resp_{turn}_{int(t)}",
            "usage": {"input_tokens": inp, "cached_input_tokens": cached,
                      "output_tokens": 5, "total_tokens": inp + 5}})

    def turn(self, turn, t, *, model="gpt-5.6-terra", length=5.0, inp=20_000,
             cached=19_500, records=1):
        """A whole turn: start, context, the user message, `records` requests, done."""
        self.start(turn, t).context(turn, t + 0.01, model)
        self.event("item_completed", t + 0.02, item={"type": "UserMessage", "id": "u"})
        self.item("message", t + 0.02, role="user", content=[])
        for n in range(records):
            stamp = t + length * (n + 1) / records
            self.item("reasoning", stamp - 0.01, summary=[])
            self.record(turn, stamp, inp=inp, cached=cached if n == 0 else inp - 100)
        self.item("message", t + length + 0.01, role="assistant", content=[])
        return self.event("task_complete", t + length + 0.02, turn_id=turn)

    def text(self) -> str:
        body = "".join(json.dumps(line) + "\n" for line in self.lines)
        if self.raw_tail is not None:
            body += self.raw_tail
        return body

    def write(self, root: Path, *, suffix="", name_tid=None, day="2026/10/05",
              zst=False) -> Path:
        directory = root / day
        directory.mkdir(parents=True, exist_ok=True)
        name = f"rollout-2026-10-05T15-55-21-{name_tid or self.tid}{suffix}.jsonl"
        if zst:
            name += ".zst"
        path = directory / name
        path.write_text(self.text())
        return path


def sessions_root(tmp_path) -> Path:
    root = tmp_path / "codex-home" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root




# ================================================================ opencode fixtures

#: OpenCode 1.18.34's schema for the three tables comms reads, as read on 2026-10-05,
#: and the two it must never read.
SCHEMA = """
CREATE TABLE `session` (
  `id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text, `parent_id` text,
  `slug` text NOT NULL, `directory` text NOT NULL, `path` text, `title` text NOT NULL,
  `version` text NOT NULL, `share_url` text, `summary_additions` integer,
  `summary_deletions` integer, `summary_files` integer, `summary_diffs` text,
  `metadata` text, `cost` real DEFAULT 0 NOT NULL,
  `tokens_input` integer DEFAULT 0 NOT NULL, `tokens_output` integer DEFAULT 0 NOT NULL,
  `tokens_reasoning` integer DEFAULT 0 NOT NULL,
  `tokens_cache_read` integer DEFAULT 0 NOT NULL,
  `tokens_cache_write` integer DEFAULT 0 NOT NULL, `revert` text, `permission` text,
  `agent` text, `model` text, `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL, `time_compacting` integer, `time_archived` integer);
CREATE TABLE `message` (
  `id` text PRIMARY KEY, `session_id` text NOT NULL, `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL, `data` text NOT NULL);
CREATE TABLE `part` (
  `id` text PRIMARY KEY, `message_id` text NOT NULL, `session_id` text NOT NULL,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL);
CREATE INDEX `message_session_time_created_id_idx` ON `message` (`session_id`,`time_created`,`id`);
CREATE INDEX `part_message_id_id_idx` ON `part` (`message_id`,`id`);
CREATE INDEX `part_session_idx` ON `part` (`session_id`);
CREATE TABLE `account` (`id` text PRIMARY KEY, `email` text);
CREATE TABLE `credential` (`id` text PRIMARY KEY, `secret` text);
INSERT INTO `account` VALUES ('acc', 'someone@example.invalid');
INSERT INTO `credential` VALUES ('cred', 'SECRET-DO-NOT-READ');
"""

BUILD_NULL = {"agent": "build", "model": None}
FREE_AGENT = {"agent": "build", "model": "opencode/big-pickle"}


class OcDb:
    """A fixture `opencode.db` in the 1.18.34 schema."""

    def __init__(self, path: Path, schema: str = SCHEMA):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.n = 0
        with self.conn() as c:
            c.executescript(schema)

    def conn(self):
        c = sqlite3.connect(self.path)
        c.isolation_level = None
        return c

    def mint(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n:026d}"

    def session(self, sid=SID, *, model=("opencode", "big-pickle"), version="1.18.34",
                totals=0, revert=None, parent=None, raw_model=...):
        if raw_model is ...:
            raw_model = None if model is None else json.dumps(
                {"id": model[1], "providerID": model[0], "variant": "default"})
        with self.conn() as c:
            c.execute("INSERT INTO session (id, project_id, parent_id, slug, directory, "
                      "title, version, cost, tokens_input, tokens_output, "
                      "tokens_reasoning, tokens_cache_read, tokens_cache_write, revert, "
                      "model, time_created, time_updated) VALUES "
                      "(?, 'global', ?, 's', '/tmp', 't', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                      (sid, parent, version, totals, totals, totals, totals, totals, totals,
                       revert, raw_model))

    def message(self, sid, created_s, data, *, raw=None):
        mid = self.mint("msg")
        with self.conn() as c:
            c.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                      (mid, sid, int(created_s * 1000), int(created_s * 1000),
                       raw if raw is not None else json.dumps(data)))
        return mid

    def part(self, sid, mid, created_s, data):
        with self.conn() as c:
            c.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                      (self.mint("prt"), mid, sid, int(created_s * 1000),
                       int(created_s * 1000), json.dumps(data)))

    def user(self, sid, t, *, model=("opencode", "big-pickle"), raw_model=...):
        data = {"role": "user", "time": {"created": int(t * 1000)}, "agent": "build",
                "summary": {"diffs": []}}
        if raw_model is not ...:
            data["model"] = raw_model
        elif model is not None:
            data["model"] = {"providerID": model[0], "modelID": model[1]}
        mid = self.message(sid, t, data)
        self.part(sid, mid, t + 0.01, {"type": "text", "text": "hello"})
        return mid

    def request(self, sid, t, provider, model, *, steps=((0.0, 3.0),), agent="build",
                summary=None, created=None, user=True):
        """A user message and its assistant reply whose first step starts at `t`, each
        step `(start offset, end offset)` from `t`. The message is created at
        `created` (default `t`)."""
        if user:
            self.user(sid, (created or t) - 0.5, model=(provider, model))
        data = {"role": "assistant", "agent": agent, "mode": agent, "providerID": provider,
                "modelID": model, "time": {"created": int((created or t) * 1000)},
                "finish": "stop"}
        if summary is not None:
            data["summary"] = summary
        mid = self.message(sid, created or t, data)
        for start, end in steps:
            self.part(sid, mid, t + start, {"type": "step-start"})
            if end is not None:
                self.part(sid, mid, t + end, {"type": "step-finish", "reason": "stop"})
        return mid


def oc_db(tmp_path, **session) -> OcDb:
    db = OcDb(tmp_path / "opencode" / "opencode.db")
    db.session(**session)
    return db




def free_db(tmp_path, *, last=("opencode", "big-pickle"), age=600, model=...,
            **session) -> OcDb:
    db = oc_db(tmp_path, model=("opencode", "big-pickle") if model is ... else model,
               **session)
    if last is not None:
        db.request(SID, NOW - age, *last)
    return db



def entry(lifetime, *, seconds=None, checked=None, model="gpt-5.6-sol", **extra):
    """A drift record entry for the measured row, in cache's record format."""
    return {"harness": "codex", "provider": "openai", "model": model,
            "lifetime": lifetime,
            "row_seconds": MEASURED_SECONDS if seconds is None else seconds,
            "row_checked": MEASURED_CHECKED if checked is None else checked, **extra}




# =========================================================== the daemon and the verb

from test_cold_every_sender import (AS_AGENT, Pushes, held_of, post,  # noqa: E402
                                    send)
from test_cold_every_sender import adapters  # noqa: E402,F401  (a fixture)
from test_cold_sends import reboot  # noqa: E402,F401  (a fixture)


def logged_about(caplog, text):
    """The daemon log's warnings that carry `text`: where cache-clock lines go."""
    return [r.getMessage() for r in caplog.records
            if r.name == "comms.daemon" and r.levelno >= logging.WARNING
            and text in r.getMessage()]

CODER_Q = "coder@proj-a"


def real_rollout(root, age, model="gpt-5.6-terra", tid=TID, **meta) -> Path:
    now = time.time()
    return Rollout(tid, **meta).turn("t1", now - age - 5, model=model).write(root)


def codex_bind(adapters, root, *, tid=TID, turned=True, new_session=False, **extra):
    report = {"new_session": new_session, **extra}
    if root is not None:
        report["cache_source"] = str(root)
    if tid:
        report.update(session_id=tid, turned=turned)
    return adapters(slot=5, name="coder", scope="proj-a", adapter="codex", report=report)


def oc_bind(adapters, db, *, sid=SID, turned=True, new_session=False,
            agent_model=BUILD_NULL, **extra):
    report = {"new_session": new_session, "agent_model": agent_model, **extra}
    if db is not None:
        report["cache_source"] = str(db.path)
    if sid:
        report.update(session_id=sid, turned=turned)
    return adapters(slot=5, name="coder", scope="proj-a", adapter="opencode",
                    report=report)


def real_oc(tmp_path, age, provider="anthropic", model="claude-A") -> OcDb:
    db = oc_db(tmp_path, model=(provider, model))
    db.request(SID, time.time() - age, provider, model)
    return db


def delivered(pushes, msg):
    return wait_until(lambda: msg.id in pushes.ids())


def queued(rig, msg):
    """The daemon could not hand `msg` to coder live: it waits in coder's queue (the
    daemon never refuses a posted message)."""
    return wait_until(lambda: msg.id in held_of(rig, CODER_Q))


def test_warm_codex_and_opencode_get_live_mail(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    real_rollout(root, 60)
    coder = codex_bind(adapters, root)
    msg = post(rig, [CODER_Q])
    assert delivered(coder, msg)
    coder.client.close()
    wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    db = real_oc(tmp_path, 60)
    oc = oc_bind(adapters, db)
    msg = post(rig, [CODER_Q])
    assert delivered(oc, msg)


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_cold_ones_are_queued_with_a_record_row(rig, adapters, tmp_path, kind):
    if kind == "codex":
        root = sessions_root(tmp_path)
        real_rollout(root, 4000)
        coder = codex_bind(adapters, root)
    else:
        coder = oc_bind(adapters, real_oc(tmp_path, 400))
    msg = post(rig, [CODER_Q])
    assert queued(rig, msg)
    time.sleep(0.1)
    assert coder.ids() == []
    assert wait_until(lambda: msg.id in record_rows(rig.gateway, CODER_Q))


def test_a_new_codex_identity_takes_its_deferred_mail(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    msg = post(rig, [CODER_Q], "while you were away")
    wait_until(lambda: msg.id in held_of(rig, CODER_Q))
    coder = codex_bind(adapters, root, tid=None, new_session=True)
    assert delivered(coder, msg)


@pytest.mark.parametrize("case", ["cold", "not-under-root", "unreadable-record"])
@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_a_resumed_identity_on_a_cold_or_unread_session_queues_its_backlog(
        rig, adapters, tmp_path, reboot, case, kind):
    msg = post(rig, [CODER_Q], "while you were away")
    wait_until(lambda: msg.id in held_of(rig, CODER_Q))
    if case == "not-under-root":
        reboot(rig)
    if kind == "codex":
        root = sessions_root(tmp_path)
        if case == "cold":
            real_rollout(root, 4000)
        if case == "unreadable-record":
            coder = codex_bind(adapters, root, tid=None, new_session=False)
        else:
            coder = codex_bind(adapters, root)
    else:
        db = real_oc(tmp_path, 400) if case == "cold" else oc_db(tmp_path)
        if case == "not-under-root":
            coder = oc_bind(adapters, db, sid=SID_B)
        elif case == "unreadable-record":
            coder = oc_bind(adapters, db, sid=None, new_session=False)
        else:
            coder = oc_bind(adapters, db)
    assert queued(rig, msg)
    assert coder.ids() == []


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_a_launcher_that_reports_none_of_the_new_fields_is_queued(rig, adapters,
                                                                      kind):
    coder = adapters(slot=5, name="coder", scope="proj-a", adapter=kind)
    msg = post(rig, [CODER_Q])
    assert queued(rig, msg)
    assert coder.ids() == []


def test_the_fields_come_from_the_bindings_own_connection(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    real_rollout(root, 60)
    coder = codex_bind(adapters, None)               # no root: unknown
    forger = DaemonClient(daemon_socket()).connect()
    try:
        forger.notify("activity", identity=CODER_Q, cache_source=str(root),
                      session_id=TID, turned=True)
        time.sleep(0.2)
        assert rig.daemon.bindings[CODER_Q].cache_source is None
    finally:
        forger.close()
    coder.client.notify("activity", identity=CODER_Q, kind="ready", cache_source=str(root))
    wait_until(lambda: rig.daemon.bindings[CODER_Q].cache_source == str(root))
    msg = post(rig, [CODER_Q])
    assert delivered(coder, msg)
    # A thread id another live binding holds is refused.
    other = adapters(slot=6, name="other", scope="proj-a", adapter="codex",
                     report={"session_id": TID, "cache_source": str(root), "turned": True})
    assert rig.daemon.bindings["other@proj-a"].session_id is None
    assert other is not None


def test_the_session_answer_carries_the_lifetime_and_the_key(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    real_rollout(root, 60)
    codex_bind(adapters, root)
    c = DaemonClient(daemon_socket()).connect()
    try:
        answer = c.query("session", identity=CODER_Q)
        unbound = c.query("session", identity="nobody@proj-a")
    finally:
        c.close()
    assert answer["cache_kind"] == "warm" and answer["cache_ttl"] == 1800
    assert answer["cache_key"] == "codex openai/gpt-5.6-terra"
    assert answer["cache_source"] == str(root)
    assert {unbound[k] for k in ("cache_ttl", "cache_key", "cache_source")} == {None}


def test_the_hold_and_comms_cache_lines(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    real_rollout(root, 1800 + 12 * 60)
    codex_bind(adapters, root)
    code, out = send(rig, CODER_Q)
    assert code == 0, out                  # a cold codex agent is held like any
    assert out.splitlines()[0].startswith("sent ")
    assert out.splitlines()[-1].startswith("Sent and held: coder@proj-a reads this after")
    first = out.splitlines()[1]
    assert first.startswith("held: coder@proj-a's cache went cold 12m ago (last request ")
    assert first.endswith("; on gpt-5.6-terra comms counts a cache warm for 30 minutes "
                          "after its last request, the minimum OpenAI documents).")
    code, out = rig.cli(["cache", CODER_Q], env={"MOXI_COMMS_OPERATOR": "900001"})
    assert out.strip() == (
        "coder@proj-a: cache expired 12 minutes ago (gpt-5.6-terra, 30 minutes after a "
        "request). No agent can reach it now; only the operator, in its session.")


def test_an_opencode_model_with_no_row_is_sent_live(rig, adapters, tmp_path):
    """A model with no lifetime row reads `no-cache`: the send
    delivers live and holds nothing."""
    db = oc_db(tmp_path, model=("openai", "gpt-5.5"))
    db.request(SID, time.time() - 180, "openai", "gpt-5.5")
    oc = oc_bind(adapters, db)
    code, out = send(rig, CODER_Q)
    assert code == 0, out
    assert out.splitlines()[0].startswith("sent ")
    assert not any(line.startswith("held:") for line in out.splitlines()), out
    sent = rig.gateway.history(rig.names_channel)[-1]
    assert delivered(oc, sent)
    code, out = rig.cli(["cache", CODER_Q], env={"MOXI_COMMS_OPERATOR": "900001"})
    assert out.strip() == (
        "coder@proj-a: no documented cache lifetime is known for openai/gpt-5.5, so "
        "comms does not count its cache, and agents' messages reach it.")


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_an_exception_inside_a_reader_queues(rig, adapters, tmp_path, monkeypatch,
                                                  kind):
    # cache's read never raises (an exception inside it is `unknown`, `read-failed`);
    # one that escaped it anyway is caught by the seam, and the mail waits either way.
    monkeypatch.setattr(cache_api(), "read", lambda *a, **k: 1 / 0)
    if kind == "codex":
        coder = codex_bind(adapters, sessions_root(tmp_path))
    else:
        coder = oc_bind(adapters, oc_db(tmp_path), agent_model=FREE_AGENT)
    msg = post(rig, [CODER_Q])
    assert queued(rig, msg)
    assert coder.ids() == []


def test_went_cold_is_the_age_less_the_lifetime_for_claude_too(rig):
    write_transcript(CLAUDE_SID, 2 * 3600 + 600)
    rig.shim(slot=2, name="reviewer", scope="proj-a", session_id=CLAUDE_SID)
    code, out = send(rig, "reviewer@proj-a")
    assert code == 0, out                  # cold-hold: posted and held
    assert out.splitlines()[1].startswith("held: reviewer@proj-a's cache went cold 1h10m "
                                          "ago (last turn ")


#: cache's reason for a Codex rollout compressed after a week idle (a `cold` with no age).
COMPRESSED = "its rollout has been idle at least 7 days"


def test_a_cold_read_with_no_lifetime_prints_no_duration_and_describe_agrees():
    from moxi_comms.daemon import Daemon
    changed = cache.Clock("cold", age_seconds=180, code="changed",
                          reason="the model changed since the last request")
    assert Daemon._describe(changed) == ("cold (the model changed since the last request; "
                                         "last request 3m ago)")
    zst = cache.Clock("cold", reason=COMPRESSED, code="compressed")
    assert Daemon._describe(zst) == f"cold ({COMPRESSED})"
    timed = cache.Clock("cold", age_seconds=42 * 60, ttl_seconds=1800)
    assert Daemon._describe(timed) == "cold for 12m (last request 42m ago)"
    state = CacheState(target="x", kind="cold", age_seconds=42 * 60, ttl_seconds=1800,
                       key="codex openai/gpt-5.6-terra", adapter="codex")
    assert "went cold 12m ago" in cache.hold_line(state)
    for clock_state_ in (CacheState(target="x", kind="cold", age_seconds=180,
                                    reason="no row", adapter="opencode"),
                         CacheState(target="x", kind="cold", reason=COMPRESSED,
                                    adapter="codex")):
        line = cache.hold_line(clock_state_)
        assert "went cold" not in line and "counted as cold" in line


def test_a_retry_with_a_recorded_id_queues_the_mail_handed_at_the_first_replay(
        rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    msg = post(rig, [CODER_Q], "while you were away")
    wait_until(lambda: msg.id in held_of(rig, CODER_Q))
    first = codex_bind(adapters, root, tid=None, new_session=True)
    assert delivered(first, msg), "a new thread takes its deferred mail at replay"
    first.client.close()                             # the launcher drops it and re-registers
    wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    real_rollout(root, 4000)                          # the record appeared: an old thread
    second = codex_bind(adapters, root, turned=True)
    assert queued(rig, msg)
    time.sleep(0.1)
    assert second.ids() == []


# ======================================================== free models at the daemon

def test_a_free_opencode_agent_is_delivered_sent_to_and_spoken_of(rig, adapters,
                                                                      tmp_path):
    db = free_db(tmp_path, age=40 * 86400)
    oc = oc_bind(adapters, db)
    msg = post(rig, [CODER_Q])
    assert delivered(oc, msg)
    code, out = send(rig, CODER_Q, "hi")
    assert code == 0, out
    code, out = rig.cli(["cache", CODER_Q], env={"MOXI_COMMS_OPERATOR": "900001"})
    assert out.strip() == ("coder@proj-a: on a free model (opencode/big-pickle), so comms "
                           "does not count its cache, and agents' messages reach it.")


# =========================================================== drift at the daemon

def lowering_rollout(root, *, gap=None, age=60):
    """A measured-row thread whose newest turn found its cache cold `gap` seconds after
    the request before, its last request `age` seconds ago."""
    seconds = measured_seconds()
    gap = seconds - 1200 if gap is None else gap
    end = time.time() - age
    start = end - 5
    prev = start - gap
    r = Rollout().start("t1", prev - 30).context("t1", prev - 29, "gpt-5.6-sol")
    r.record("t1", prev, inp=48_210, cached=48_000)
    r.event("task_complete", prev + 0.1, turn_id="t1")
    r.start("t2", start).context("t2", start + 0.01, "gpt-5.6-sol")
    r.record("t2", end, inp=48_700, cached=7_424)
    r.write(root)
    return prev, gap


def test_a_lowering_is_written_logged_shown_and_survives_a_restart(
        rig, adapters, tmp_path, reboot, caplog):
    caplog.set_level(logging.WARNING, logger="comms.daemon")
    root = sessions_root(tmp_path)
    seconds = measured_seconds()
    prev, gap = lowering_rollout(root, age=60)
    rig.clock.t = prev - 100                          # registered before R_prev
    codex_bind(adapters, root)
    c = DaemonClient(daemon_socket()).connect()
    try:
        answer = c.query("session", identity=CODER_Q)
    finally:
        c.close()
    lowered = lower_to(gap)
    assert answer["cache_kind"] == "warm" and answer["cache_ttl"] == lowered
    assert "lowered from" in answer["cache_key"]
    record = json.loads(drift_record_path(rig).read_text())
    assert [e["lifetime"] for e in record["entries"]] == [lowered]
    lines = wait_until(lambda: logged_about(caplog, "cache clock: gpt-5.6-sol on codex now "
                                                    "counts"))
    assert len(lines) == 1
    assert (f"now counts {cache.span(lowered)}, down from {cache.span(seconds)}" in lines[0]
            and "coder@proj-a's request at" in lines[0] and TID in lines[0]
            and "cached 7,424 of 48,210 tokens" in lines[0]
            and "Raising it takes a new probe or the operator's word" in lines[0])
    code, out = rig.cli(["cache", CODER_Q], env={"MOXI_COMMS_OPERATOR": "900001"})
    assert "measured on this box, lowered from" in out, out
    # A read that lowers nothing logs nothing more.
    c = DaemonClient(daemon_socket()).connect()
    try:
        c.query("session", identity=CODER_Q)
    finally:
        c.close()
    time.sleep(0.1)
    assert len(logged_about(caplog, "now counts")) == 1
    # The lowering survives a restart.
    reboot(rig)
    codex_bind(adapters, root)                        # bound again on the new daemon
    c = DaemonClient(daemon_socket()).connect()
    try:
        assert c.query("session", identity=CODER_Q)["cache_ttl"] == lowered
    finally:
        c.close()


def test_the_hold_names_the_lowering(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    seconds = measured_seconds()
    gap = seconds - 1200
    lowered = lower_to(gap)
    prev, gap = lowering_rollout(root, gap=gap, age=lowered + 600)
    rig.clock.t = prev - 100
    codex_bind(adapters, root)
    code, out = send(rig, CODER_Q)
    assert code == 0, out
    assert (f"on gpt-5.6-sol comms counts a cache warm for {cache.spoken_span(lowered)} "
            f"after its last request, measured on this box and lowered from "
            f"{cache.span(seconds)} when a cache went cold sooner)") in out, out
    assert "went cold 10m ago" in out


def test_a_write_that_fails_holds_the_model_until_one_succeeds(rig, adapters, tmp_path):
    root = sessions_root(tmp_path)
    prev, _gap = lowering_rollout(root)
    # cache's state directory, where the learning store writes the record by atomic
    # replace: unwritable, so the lowering cannot be written.
    blocked = drift_record_path(rig).parent
    blocked.chmod(0o500)
    try:
        rig.clock.t = prev - 100
        coder = codex_bind(adapters, root)
        msg = post(rig, [CODER_Q])
        assert queued(rig, msg)
        assert not drift_record_path(rig).exists()
    finally:
        blocked.chmod(0o700)
    msg2 = post(rig, [CODER_Q])
    assert delivered(coder, msg2), "the retry at the next read wrote it"
    assert json.loads(drift_record_path(rig).read_text())["entries"]


@pytest.mark.parametrize("bad", ["above", "unparseable"])
def test_a_rejected_or_unreadable_record_is_held_and_said_once(rig, adapters, tmp_path,
                                                                  reboot, bad, caplog):
    caplog.set_level(logging.WARNING, logger="comms.daemon")
    path = drift_record_path(rig)
    path.parent.mkdir(parents=True, exist_ok=True)
    if bad == "unparseable":
        path.write_text("{nope")
    else:
        path.write_text(json.dumps({"entries": [entry(measured_seconds() + 60)]}))
    reboot(rig)
    root = sessions_root(tmp_path)
    real_rollout(root, 60, model="gpt-5.6-sol")
    coder = codex_bind(adapters, root)
    msg = post(rig, [CODER_Q])
    assert queued(rig, msg)
    post(rig, [CODER_Q])
    time.sleep(0.2)
    said = logged_about(caplog, "cache clock:")
    assert len(said) == 1 and str(path) in said[0]
    assert coder.ids() == []


# ======================================================================= the launchers

from types import SimpleNamespace  # noqa: E402

from moxi_comms.adapters import opencode as oc  # noqa: E402
from moxi_comms.adapters import opencode_roster  # noqa: E402
from moxi_comms.adapters.codex import CodexAdapter  # noqa: E402
from moxi_comms.adapters.codex_roster import CodexAgent  # noqa: E402
from fake_opencode import FakeOpenCode, install  # noqa: E402
from fake_turns import FakeSession, launcher as fake_turns_launcher  # noqa: E402


def watch_reports(adapter):
    """Every session report the launcher sends, with whether it rode a register."""
    sent = []
    real = adapter.session_report

    def report(*, register):
        out = real(register=register)
        sent.append((register, dict(out)))
        return out

    adapter.session_report = report
    return sent


@pytest.fixture
def codex_launcher(rig, tmp_path):
    made = []
    numbers = __import__("itertools").count(1)

    def build(*, session=None, start=True, autoreconnect=False):
        session = session or FakeSession(f"thread-{next(numbers)}")
        agent = CodexAgent(qualified=CODER_Q, name="coder", scope="proj-a", slot=5,
                           cwd=str(tmp_path), thread_file=codex_threads.path(CODER_Q))
        adapter = CodexAdapter(
            agent, launch=fake_turns_launcher(session), autoreconnect=autoreconnect,
            backoff=__import__("itertools").repeat(0.05), clock=rig.clock,
            status_sink=lambda text: None)
        adapter.fake = session
        adapter.reports = watch_reports(adapter)
        made.append(adapter)
        if start:
            adapter.start()
        return adapter

    yield build
    for adapter in made:
        adapter.stop()


def binding(rig):
    return wait_until(lambda: rig.daemon.bindings.get(CODER_Q))


def test_cache_source_is_the_sessions_directory_under_codex_home(
        rig, codex_launcher, tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = codex_launcher()
    assert binding(rig).cache_source == str(home / "sessions")
    assert all(report.get("cache_source") == str(home / "sessions")
               for _register, report in adapter.reports)


def test_a_new_thread_after_no_record(rig, codex_launcher):
    adapter = codex_launcher()
    assert [report for register, report in adapter.reports if register][0] == {
        "new_session": True, "cache_source": adapter.cache_source()}
    assert [report for register, report in adapter.reports if not register][0] == {
        "session_id": adapter.thread_id, "turned": False,
        "cache_source": adapter.cache_source()}
    assert binding(rig).session_id == adapter.thread_id
    assert binding(rig).turned is False


def test_every_launch_registers_a_new_session_and_starts_unturned(
        rig, codex_launcher):
    first = codex_launcher()
    first.fake.start_turn()
    first.fake.end_turn("answer")
    assert wait_until(lambda: binding(rig).turned)
    old = first.thread_id
    first.stop()
    assert wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    second = codex_launcher()
    assert second.thread_id != old
    assert [report for register, report in second.reports if register][0][
        "new_session"] is True
    assert binding(rig).session_id == second.thread_id
    assert binding(rig).turned is False


def test_an_unreadable_record_is_not_read(rig, codex_launcher):
    path = codex_threads.path(CODER_Q)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    adapter = codex_launcher()
    assert [report for register, report in adapter.reports if register][0][
        "new_session"] is True
    assert codex_threads.read(CODER_Q) == adapter.thread_id


def never_read(monkeypatch, kind_cls, outcome_module, outcome_name):
    from moxi_comms.adapters import harness

    def never(*args, **kwargs):
        raise AssertionError("a launch read its record for an id")

    monkeypatch.setattr(kind_cls, "record_read", never)
    monkeypatch.setattr(harness.HarnessAdapter, "read_for_register", never)
    monkeypatch.setattr(harness.HarnessAdapter, "read_for_attempt", never)
    monkeypatch.setattr(outcome_module, outcome_name, never)


def test_a_codex_launch_never_reads_the_record_for_an_id(
        rig, codex_launcher, monkeypatch):
    never_read(monkeypatch, CodexAdapter, codex_threads, "read_outcome")
    monkeypatch.setattr(codex_threads, "read", lambda qualified: (_ for _ in ()).throw(
        AssertionError("a launch read the record's id")))
    codex_threads.write(CODER_Q, "old-launch")
    adapter = codex_launcher()
    assert adapter.thread_id != "old-launch"


def test_a_reregister_inside_a_launch_reports_the_held_thread(
        rig, codex_launcher):
    adapter = codex_launcher()
    adapter.reregister()
    reports = [report for register, report in adapter.reports if register]
    assert reports[-1]["new_session"] is False
    assert reports[-1]["session_id"] == adapter.thread_id
    assert reports[-1]["turned"] is False
    adapter.fake.start_turn()
    adapter.fake.end_turn("answer")
    assert wait_until(lambda: binding(rig).turned)
    adapter.reregister()
    assert [report for register, report in adapter.reports if register][-1][
        "turned"] is True


@pytest.mark.parametrize("config,expected", [
    ({"agent": {}}, {"agent": "build", "model": None}),
    ({"agent": {"build": {"model": "anthropic/claude-x"}}},
     {"agent": "build", "model": "anthropic/claude-x"}),
    ({"default_agent": "mine", "agent": {"mine": {"model": "opencode/big-pickle"}}},
     {"agent": "mine", "model": "opencode/big-pickle"}),
    ({"default_agent": "plan", "agent": {}}, {"agent": "plan", "model": None}),
    ({"agent": {"build": {"disable": True}}}, None),
    ({"agent": {"build": {"hidden": True}}}, None),
    ({"agent": {"build": {"name": "builder"}}}, None),
    ({"agent": {"build": {"mode": "subagent"}}}, None),
    ({"default_agent": "general", "agent": {}}, None),
    ({"default_agent": "mine", "agent": {"mine": {"mode": "subagent"}}}, None),
    ({"agent": {"build": {"model": 7}}}, None),
    ("not an object", None),
])
def test_the_default_agent_is_named_only_by_opencodes_rule(config, expected):
    assert oc.default_agent_model(config) == expected


@pytest.fixture
def oc_launcher(rig, tmp_path, monkeypatch):
    fake = FakeOpenCode(password="pw", default_model="opencode/minimax-m3.1-flash-free",
                        heartbeat=0.5).start()
    stub = install(tmp_path / "bin")
    monkeypatch.setenv("FAKE_OPENCODE_DIR", str(tmp_path / "fake-events"))
    made = []

    class Handle:
        alive, returncode = True, None

        def wait_listening(self, timeout=None):
            return fake.url

        def stop(self):
            pass

    def build(*, start=True):
        agent = opencode_roster.OpenCodeAgent(
            qualified=CODER_Q, name="coder", scope="proj-a", slot=5,
            cwd=str(tmp_path / "work"), session_file=opencode_roster.session_path(CODER_Q),
            models=["opencode/minimax-m3.1-flash-free"])
        (tmp_path / "work").mkdir(exist_ok=True)
        adapter = oc.OpenCodeAdapter(agent, binary=str(stub), spawn=False, server=Handle(),
                                     password="pw", autoreconnect=False,
                                     backoff=__import__("itertools").repeat(0.05), clock=rig.clock,
                                     status_sink=lambda text: None, layer_one=False)
        adapter.reports = watch_reports(adapter)
        fake.on_log = adapter.on_log_line
        made.append(adapter)
        if start:
            adapter.start()
        return adapter

    build.fake = fake
    build.events = lambda: [json.loads(x) for x in
                            (tmp_path / "fake-events" / "events.jsonl").read_text()
                            .splitlines()] if (tmp_path / "fake-events" /
                                               "events.jsonl").exists() else []
    build.cwd = str(tmp_path / "work")
    yield build
    for adapter in made:
        adapter.stop()
    fake.stop()


def test_register_carries_the_db_and_the_agent_model_from_the_servers_env(
        rig, oc_launcher, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_OPENCODE_DB", str(tmp_path / "data" / "opencode.db"))
    monkeypatch.setenv("FAKE_OPENCODE_CONFIG",
                       json.dumps({"agent": {"build": {"model": "anthropic/claude-x"}}}))
    oc_launcher()
    b = binding(rig)
    assert b.cache_source == str(tmp_path / "data" / "opencode.db")
    assert b.agent_model == {"agent": "build", "model": "anthropic/claude-x"}
    runs = [e for e in oc_launcher.events() if e["event"] in ("db path", "debug config")]
    assert runs and all(e["password"] and e["cwd"] == oc_launcher.cwd for e in runs), \
        "both ran under the server's environment, in the agent's working directory"


def test_commands_that_fail_report_nothing(rig, oc_launcher, monkeypatch):
    monkeypatch.delenv("FAKE_OPENCODE_DB", raising=False)
    monkeypatch.setenv("FAKE_OPENCODE_CONFIG", "fail")
    adapter = oc_launcher()
    b = binding(rig)
    assert b.cache_source is None and b.agent_model is cache.MISSING
    assert [r for reg, r in adapter.reports if reg][0] == {"agent_model": None,
                                                            "new_session": True}


def test_a_new_session_then_its_first_turn(rig, oc_launcher):
    adapter = oc_launcher()
    sid = adapter.session_id
    ready = [r for reg, r in adapter.reports if not reg]
    assert ready[0]["session_id"] == sid and ready[0]["turned"] is False
    assert binding(rig).turned is False
    oc_launcher.fake.prompt(sid, oc.mint_message_id(), text="typed", source="tui")
    oc_launcher.fake.finish(sid)
    wait_until(lambda: binding(rig).turned)
    assert [r for reg, r in adapter.reports if not reg][-1]["turned"] is True


def test_every_launch_registers_a_new_session(rig, oc_launcher):
    """A second launch, and one over an unreadable file, each register
    `new_session: true`, read no session file, and start their session unturned."""
    first = oc_launcher()
    sid = first.session_id
    first.stop()
    wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    second = oc_launcher()
    reg = [r for reg, r in second.reports if reg][0]
    assert reg["new_session"] is True and "session_id" not in reg
    assert second.session_id != sid
    assert [r for reg, r in second.reports if not reg][0]["turned"] is False
    second.stop()
    wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    opencode_roster.session_path(CODER_Q).write_text("[not, an, object")
    third = oc_launcher()
    assert [r for reg, r in third.reports if reg][0]["new_session"] is True
    assert [r for reg, r in third.reports if not reg][0]["turned"] is False
    assert third.session_id not in (sid, second.session_id)


def test_a_re_register_inside_a_launch_reports_the_held_session(rig, oc_launcher):
    adapter = oc_launcher()
    sid = adapter.session_id
    adapter.reregister()
    reg = [r for reg, r in adapter.reports if reg][-1]
    assert (reg["new_session"], reg["session_id"], reg["turned"]) == (False, sid, False)
    oc_launcher.fake.prompt(sid, oc.mint_message_id(), text="typed", source="tui")
    oc_launcher.fake.finish(sid)                      # the launch's first turn
    assert wait_until(lambda: binding(rig).turned)
    adapter.reregister()
    reg = [r for reg, r in adapter.reports if reg][-1]
    assert (reg["new_session"], reg["session_id"], reg["turned"]) == (False, sid, True)


def restart_oc_server(adapter, oc_launcher, monkeypatch) -> None:
    """The launch's server dies and `supervise_port` starts it again on the same fake,
    which keeps its sessions as OpenCode's database would."""

    class Restarted:
        alive, returncode = True, None

        def start(self):
            pass

        def wait_listening(self, timeout=None):
            return oc_launcher.fake.url

        def stop(self):
            pass

    monkeypatch.setattr(adapter, "_new_server", lambda model: Restarted())
    adapter.spawn = True
    adapter.server.alive = False                      # the server died
    assert adapter.supervise_port()                   # a server restart


def test_a_server_restart_after_the_first_turn_keeps_the_session_turned(
        rig, oc_launcher, monkeypatch):
    """No restart inside a launch makes its session fresh again: after the first turn
    ends, the reports a server restart sends, and a `register` after it, still say
    `turned: true`."""
    adapter = oc_launcher()
    sid = adapter.session_id
    idles = []
    real_idle = adapter._on_idle
    adapter._on_idle = lambda: (real_idle(), idles.append(sid))
    oc_launcher.fake.auto = False                     # the test ends the turn itself
    oc_launcher.fake.prompt(sid, oc.mint_message_id(), text="typed", source="tui")
    oc_launcher.fake.finish(sid)                      # the launch's first turn
    # The turn's end is two events (`session.status` idle, then `session.idle`); both
    # are handled before the restart, so no late idle raises `turned` after it.
    assert wait_until(lambda: len(idles) == 2)
    assert adapter._turned(sid) and binding(rig).turned
    first = adapter.generation
    mark = len(adapter.reports)
    restart_oc_server(adapter, oc_launcher, monkeypatch)
    assert adapter.generation is not first and adapter.session_id == sid
    adapter.reregister()
    after = adapter.reports[mark:]
    assert len(after) >= 3, "the restart reported, and so did the register"
    assert [(r["session_id"], r["turned"]) for _reg, r in after] == [(sid, True)] * len(
        after)
    assert after[-1][0] and after[-1][1]["new_session"] is False
    assert [r[:2] for r in oc_launcher.fake.requests].count(("POST", "/session")) == 1


def test_an_opencode_launch_never_reads_the_session_file_for_an_id(
        rig, oc_launcher, monkeypatch):
    """Start, a first turn, a server restart and a window reopen, with every record
    read patched to raise."""
    from moxi_comms.adapters.opencode_window import OpenCodeWindow

    never_read(monkeypatch, oc.OpenCodeAdapter, oc, "read_outcome")
    oc.update_record(opencode_roster.session_path(CODER_Q),
                     session_id="ses_ef0000000000000000000000000")
    adapter = oc_launcher()
    sid = adapter.session_id
    assert sid != "ses_ef0000000000000000000000000"
    oc_launcher.fake.prompt(sid, oc.mint_message_id(), text="typed", source="tui")
    oc_launcher.fake.finish(sid)                      # a first turn
    wait_until(lambda: binding(rig).turned)
    first = adapter.generation
    restart_oc_server(adapter, oc_launcher, monkeypatch)
    assert adapter.generation is not first and adapter.session_id == sid
    argv = OpenCodeWindow.argv(SimpleNamespace(binary="/b/oc"), adapter.generation)
    assert argv[-2:] == ["-s", sid], "the reopened window attaches the launch's session"
    assert [r[:2] for r in oc_launcher.fake.requests].count(("POST", "/session")) == 1


def test_new_session_comes_from_register_only_and_clears_with_an_id(rig, adapters,
                                                                      tmp_path):
    root = sessions_root(tmp_path)
    coder = codex_bind(adapters, root, tid=None, new_session=False)
    coder.client.notify("activity", identity=CODER_Q, kind="ready", new_session=True)
    time.sleep(0.2)
    assert rig.daemon.bindings[CODER_Q].new_session is False, "an activity cannot set it"
    coder.client.close()
    wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    coder = codex_bind(adapters, root, tid=None, new_session=True)
    assert rig.daemon.bindings[CODER_Q].new_session is True
    coder.client.notify("activity", identity=CODER_Q, kind="ready", session_id=TID,
                        turned=False)
    wait_until(lambda: rig.daemon.bindings[CODER_Q].session_id == TID)
    assert rig.daemon.bindings[CODER_Q].new_session is False
    coder.client.close()
    wait_until(lambda: CODER_Q not in rig.daemon.bindings)
    codex_bind(adapters, root, tid=TID, new_session=True)
    assert rig.daemon.bindings[CODER_Q].new_session is False, "ignored once an id is held"


def test_a_null_agent_model_withdraws_the_report(rig, adapters, tmp_path):
    db = free_db(tmp_path)
    oc = oc_bind(adapters, db, agent_model=FREE_AGENT)
    assert rig.daemon.bindings[CODER_Q].agent_model == FREE_AGENT
    oc.client.notify("activity", identity=CODER_Q, kind="ready")
    time.sleep(0.2)
    assert rig.daemon.bindings[CODER_Q].agent_model == FREE_AGENT, \
        "a frame without it keeps it"
    oc.client.notify("activity", identity=CODER_Q, kind="ready", agent_model=None)
    wait_until(lambda: rig.daemon.bindings[CODER_Q].agent_model is cache.MISSING)
    msg = post(rig, [CODER_Q])
    assert queued(rig, msg), "with no agent model the next pair is unknown"
