"""`moxi comms send --check` and `moxi comms reply --check`.

`--check` runs `send`'s own code to the point of posting and stops there: the ordered
checks (`cli._send_checks`, with `_reply_checks` inside it), the composed bodies
(`_bodies`) and the posting credential. A check that passes prints `would send <N>
message(s) to <names>` and the `held:` lines the post would print, exit 0, and posts,
nudges and records nothing. A check that fails prints `send`'s own refusal with `send`'s
own exit code.

The refusal table below is checked against the source: every refusal `return` in
`_send_checks`, `_reply_checks` and `_bodies` must be claimed by a row, and every row must
reach the site it claims, traced, under `--check` and under the plain send alike. A
refusal added to any of them later fails `test_every_refusal_site_has_a_row` until it has
a row.

Every rig test runs on the mock and on the local backend (`BOTH_BACKENDS`).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import stat
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import wait_until, write_config, write_transcript

from moxi_comms import cli, provision, sending
from moxi_comms.client import DaemonClient, DaemonOffline
from moxi_comms.daemon import Refusal
from moxi_comms.gateway import GatewayError

# The cold-cache fixtures (the autouse tmp-path guard among them), so a held send can be
# shown.
from test_cold_every_sender import adapters, only_tmp_paths  # noqa: F401

PLANNER = dict(slot=1, name="planner")
REVIEWER = dict(slot=2, name="reviewer")
P, R = "planner", "reviewer"
HOUR = 3600


# --------------------------------------------------------------------------- the sites

def _own_returns(func_node):
    """Every `return` in a function, not counting the functions nested in it."""
    found = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                found.append(child)
            walk(child)

    walk(func_node)
    return sorted(found, key=lambda r: r.lineno)


def _is_checks_refusal(node) -> bool:
    """`return <code>, None` in `_send_checks`."""
    v = node.value
    return (isinstance(v, ast.Tuple) and len(v.elts) == 2
            and isinstance(v.elts[1], ast.Constant) and v.elts[1].value is None)


def _is_reply_refusal(node) -> bool:
    """`return <code>` in `_reply_checks` (its pass returns a 4-tuple)."""
    return node.value is not None and not isinstance(node.value, ast.Tuple)


def _is_bodies_refusal(node) -> bool:
    """`return None` in `_bodies`."""
    return isinstance(node.value, ast.Constant) and node.value.value is None


#: The functions whose refusals the table must cover: name -> (the function, which of its
#: returns are refusals in the source, which return values are refusals when traced).
GUARDED = {
    "_send_checks": (cli._send_checks, _is_checks_refusal,
                     lambda v: isinstance(v, tuple) and len(v) == 2 and v[1] is None
                     and v[0] is not None),
    "_reply_checks": (cli._reply_checks, _is_reply_refusal, lambda v: isinstance(v, int)),
    "_bodies": (cli._bodies, _is_bodies_refusal, lambda v: v is None),
}


def refusal_sites() -> dict:
    """{(function, line): the source from the return before it to this one}, for every
    refusal `return` in the guarded functions."""
    sites = {}
    for name, (fn, is_refusal, _traced) in GUARDED.items():
        lines, start = inspect.getsourcelines(fn)
        tree = ast.parse(textwrap.dedent("".join(lines)))
        returns = _own_returns(tree.body[0])
        prev = 0
        for node in returns:
            end = node.end_lineno
            if is_refusal(node):
                segment = "".join(lines[prev:end])
                sites[(name, start + node.lineno - 1)] = segment
            prev = end
    return sites


class Tracer:
    """Which guarded refusal `return`s ran, as (function, line).

    The line is the last `return` statement the frame ran before it returned a refusal:
    a `return` inside `try` runs its `finally` after, so the frame's own line at the
    return event can be the `finally`'s."""

    def __init__(self):
        self.codes = {fn.__code__: (name, traced)
                      for name, (fn, _src, traced) in GUARDED.items()}
        lines = {}
        for name, (fn, _src, _traced) in GUARDED.items():
            src, start = inspect.getsourcelines(fn)
            tree = ast.parse(textwrap.dedent("".join(src)))
            lines[name] = {start + r.lineno - 1 for r in _own_returns(tree.body[0])}
        self.returns = lines
        self.last: dict = {}
        self.hits: set = set()

    def __enter__(self):
        self._old = sys.gettrace()
        sys.settrace(self._global)
        return self.hits

    def __exit__(self, *exc):
        sys.settrace(self._old)

    def _global(self, frame, event, arg):
        if event == "call" and frame.f_code in self.codes:
            return self._local
        return None

    def _local(self, frame, event, arg):
        name, traced = self.codes[frame.f_code]
        if event == "line" and frame.f_lineno in self.returns[name]:
            self.last[id(frame)] = frame.f_lineno
        elif event == "return":
            line = self.last.pop(id(frame), None)
            if traced(arg):
                self.hits.add((name, line))
        return self._local


def site_of(func: str, anchor: str) -> tuple:
    """The one refusal site of `func` whose source holds `anchor`."""
    matches = [key for key, seg in refusal_sites().items()
               if key[0] == func and anchor in seg]
    assert len(matches) == 1, (func, anchor, matches)
    return matches[0]


# --------------------------------------------------------------------------- the state

def _home_files() -> dict:
    """path -> sha256 of every regular file under the test's homes."""
    out = {}
    for var in ("MOXI_COMMS_HOME", "MOXI_COMMS_HOME"):
        root = os.environ.get(var)
        if not root or not Path(root).exists():
            continue
        for path in Path(root).rglob("*"):
            try:
                st = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode) or path.name.endswith("-shm"):
                continue
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def snapshot(rig) -> dict:
    """Everything a send writes or moves: the record, every room; the daemon's queued
    and delivered ids; each shim's queue; every file under the homes."""
    rooms = {name: [(m.id, m.content) for m in rig.gateway.history(cid)]
             for name, cid in rig.prov.rooms.items()}
    with rig.daemon._lock:
        daemon = dict(
            queue={k: sorted(v) for k, v in rig.daemon.queue.items() if v},
            delivered={q: sorted(b.delivered) for q, b in rig.daemon.bindings.items()})
    queues = [list(s.queue) for s in rig.shims]
    return dict(rooms=rooms, daemon=daemon, queues=queues, files=_home_files())


@pytest.fixture
def doors(monkeypatch):
    """Every post through either door, every call of the nudge (`_nudge_daemon`), and
    every frame handed to `notify` (where the local backend's nudge goes)."""
    seen = dict(posts=[], notify=[], nudges=[])
    real_post, real_op = sending.post, sending.post_as_operator
    real_notify, real_nudge = DaemonClient.notify, sending._nudge_daemon

    def post(*a, **kw):
        seen["posts"].append(a[2] if len(a) > 2 else kw.get("content"))
        return real_post(*a, **kw)

    def post_as_operator(*a, **kw):
        seen["posts"].append(a[3] if len(a) > 3 else kw.get("content"))
        return real_op(*a, **kw)

    def notify(self, op, **kw):
        seen["notify"].append(op)
        return real_notify(self, op, **kw)

    def nudge(ctx):
        seen["nudges"].append(ctx)
        return real_nudge(ctx)

    monkeypatch.setattr(sending, "post", post)
    monkeypatch.setattr(sending, "_nudge_daemon", nudge)
    monkeypatch.setattr(sending, "post_as_operator", post_as_operator)
    monkeypatch.setattr(DaemonClient, "notify", notify)
    return seen


def with_check(argv: list) -> list:
    """`send --check ...`: the flag right after the verb, where a send-only console puts it."""
    assert argv[0] == "send"
    return ["send", "--check", *argv[1:]]


def settle():
    """Long enough for the daemon's 20 ms pump to have moved anything there was."""
    wait_until(lambda: False, timeout=0.3)


def sent_ids(out: str) -> list:
    return [ln.split()[1] for ln in out.splitlines() if ln.startswith("sent ")]


def ok(rig, who, argv) -> str:
    """Run a send that must post; return its first message id."""
    code, out = rig.cli(argv, **who)
    assert code == 0, out
    return sent_ids(out)[0]


# --------------------------------------------------------------------------- the table

@dataclass
class Case:
    argv: list
    who: dict
    code: int
    clock: object = None         # the verb's clock, where its words read the time


@dataclass
class Row:
    """One refusal: how to reach it, the exit code send gives, and the guarded sites it
    runs through (function, a string in the site's source)."""

    id: str
    setup: object
    code: int
    sites: tuple = ()
    extra: dict = field(default_factory=dict)


def pair(rig):
    return rig.shim(**PLANNER), rig.shim(**REVIEWER)


# ---- 1. the flag checks

def s_reply_to_digits(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--reply-to", "abc"], PLANNER, 2)


def s_audience_shape(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--posture", "none", "--audience", "a b"],
                PLANNER, 2)


def s_posture_required(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x"], PLANNER, 2)


def s_new_with_reply_to(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--new", "--reply-to", "123"], PLANNER, 2)


def s_cap_both(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--posture", "open", "--cap", "3", "--no-cap"],
                PLANNER, 2)


def s_cap_shape(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--posture", "open", "--cap", "0"], PLANNER, 2)


def s_block(rig, mp):
    pair(rig)
    write_config(None, "block = true\n")

    return Case(["send", "reviewer", "x", "--posture", "none"], PLANNER, 3)


# ---- 2. the daemon block

def s_daemon_offline(rig, mp):
    pair(rig)

    def offline(self):
        raise DaemonOffline("comms daemon is not running")

    mp.setattr(cli.Context, "daemon", offline)
    return Case(["send", "reviewer", "x", "--posture", "none"], PLANNER, 4)


def s_verb_protocol(rig, mp):
    """comms/refine §3.10: a daemon speaking another verb protocol is offline, exit 4."""
    pair(rig)
    mp.setattr("moxi_comms.daemon.CLI_PROTOCOL", 0)
    return Case(["send", "reviewer", "x", "--posture", "none"], PLANNER, 4)


def s_resolve_refused(rig, mp):
    pair(rig)
    return Case(["send", "agent-99", "x", "--posture", "none"], PLANNER, 2)


def s_send_only(rig, mp):
    pair(rig)
    lines = []
    assert provision.send_only_main(["console@acme"], gateway=rig.gateway,
                                    store=rig.store, out=lines.append) == 0, lines
    return Case(["send", "console@acme", "x", "--posture", "none"], PLANNER, 2)


def s_standing(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--posture", "none", "--standing"], PLANNER, 2)


def s_never_bound(rig, mp):
    """comms/refine §7.6: the daemon says the journal has never seen the name bound
    (`ever_bound` false; the daemon's half is the names slice's), and no `--new-name`."""
    pair(rig)
    real = rig.daemon._resolve_query

    def resolve(frame):
        answer = real(frame)
        answer["ever_bound"] = answer["bound"]
        return answer

    mp.setattr(rig.daemon, "_resolve_query", resolve)
    return Case(["send", "newbie", "x", "--posture", "none"], PLANNER, 2)


def s_bound_elsewhere(rig, mp):
    """The adapter holds the claim offline and another connection binds the name."""
    pair(rig)
    real = DaemonClient.query

    def agents(self, kind, **kw):
        if kind == "agents":
            return {"bindings": [{"qualified": P}]}
        return real(self, kind, **kw)

    mp.setattr(DaemonClient, "query", agents)
    real_claim = sending._claim_sender

    def offline(ctx, path):
        got = real_claim(ctx, path)
        if isinstance(got, int):
            return got
        sender, _state = got
        return sender, cli.claimfile.OFFLINE

    mp.setattr(sending, "_claim_sender", offline)
    return Case(["send", "reviewer", "x", "--posture", "none"], PLANNER, 2)


# ---- 3. the single-reply refusal

def s_single_reply(rig, mp):
    pair(rig)
    ok(rig, PLANNER, ["send", "reviewer", "which branch?", "--posture", "once"])
    return Case(["send", "planner", "on main", "--posture", "none"], REVIEWER, 2,
                clock=rig.clock)


# ---- 4. the reply checks

def s_cap_no_thread_new(rig, mp):
    pair(rig)
    return Case(["send", "reviewer", "x", "--posture", "once", "--cap", "3"], PLANNER, 2)


def _unreadable(exc):
    def setup(rig, mp):
        pair(rig)
        ask = ok(rig, PLANNER, ["send", "reviewer", "q?", "--posture", "once"])

        def broken(message_id):
            raise exc

        mp.setattr(rig.gateway, "get_message", broken)
        return Case(["send", "planner", "a", "--reply-to", ask], REVIEWER, 0)
    return setup


def s_no_reply(rig, mp):
    pair(rig)
    note = ok(rig, PLANNER, ["send", "reviewer", "fyi", "--posture", "none"])
    return Case(["send", "planner", "thanks", "--reply-to", note], REVIEWER, 2)


def s_once_not_open(rig, mp):
    pair(rig)
    ask = ok(rig, PLANNER, ["send", "reviewer", "q?", "--posture", "once"])
    return Case(["send", "planner", "a", "--reply-to", ask, "--posture", "open"],
                REVIEWER, 2)


def s_closed_by_cap(rig, mp):
    pair(rig)
    root = ok(rig, PLANNER, ["send", "reviewer", "shall we?", "--posture", "open",
                             "--cap", "1"])
    last = ok(rig, REVIEWER, ["reply", root, "yes"])
    return Case(["send", "reviewer", "more", "--reply-to", last], PLANNER, 2)


def s_closed_by_none(rig, mp):
    pair(rig)
    root = ok(rig, PLANNER, ["send", "reviewer", "shall we?", "--posture", "open"])
    ok(rig, REVIEWER, ["reply", root, "done here", "--posture", "none"])
    return Case(["send", "reviewer", "more", "--reply-to", root], PLANNER, 2)


def s_cap_no_thread_reply(rig, mp):
    pair(rig)
    ask = ok(rig, PLANNER, ["send", "reviewer", "q?", "--posture", "once"])
    return Case(["send", "planner", "a", "--reply-to", ask, "--cap", "2"], REVIEWER, 2)


def s_cap_closed(rig, mp):
    """A verified once ask by someone else stays answerable in a closed thread; a cap on
    that answer is refused."""
    pair(rig)
    root = ok(rig, PLANNER, ["send", "reviewer", "shall we?", "--posture", "open"])
    ask = ok(rig, REVIEWER, ["reply", root, "which one?", "--posture", "once"])
    ok(rig, PLANNER, ["reply", root, "stop here", "--posture", "none"])
    return Case(["send", "reviewer", "this one", "--reply-to", ask, "--cap", "3"],
                PLANNER, 2)


def s_cap_not_opener(rig, mp):
    pair(rig)
    root = ok(rig, PLANNER, ["send", "reviewer", "shall we?", "--posture", "open"])
    return Case(["send", "planner", "yes", "--reply-to", root, "--cap", "3"], REVIEWER, 2)


def s_cap_below(rig, mp):
    pair(rig)
    root = ok(rig, PLANNER, ["send", "reviewer", "shall we?", "--posture", "open"])
    first = ok(rig, REVIEWER, ["reply", root, "yes"])
    return Case(["send", "reviewer", "ok", "--reply-to", first, "--cap", "1"], PLANNER, 2)


# ---- 5. the cache reads

def s_cold_refused(rig, mp):
    """A read comms cannot make refuses the whole post, exit 6 ([F7])."""
    pair(rig)
    real = rig.daemon._session_query

    def old(frame):
        answer = real(frame)
        for key in ("cache_kind", "age_seconds", "cache_reason"):
            answer.pop(key, None)
        return answer

    mp.setattr(rig.daemon, "_session_query", old)
    return Case(["send", "reviewer", "x", "--posture", "none"], PLANNER, 6)


# ---- 6. the audience table

def s_audience_changed(rig, mp):
    pair(rig)
    ask = ok(rig, PLANNER, ["send", "reviewer", "q?", "--posture", "once",
                            "--audience", "operator"])
    return Case(["send", "planner", "a", "--reply-to", ask, "--audience", "critic"],
                REVIEWER, 2)


# ---- after the checks: the bodies and the credential

def s_over_the_cap(rig, mp):
    pair(rig)
    text = "x" * rig.gateway.max_content_length
    return Case(["send", "reviewer", text, "--posture", "none"], PLANNER, 2)


def s_no_credential(rig, mp):
    ghost = rig.shim(slot=3)
    return Case(["send", ghost.identity, "hello", "--posture", "none"],
                dict(name="planner"), 5)


# ---- before the checks: who is sending (`_send`)

def s_stamp_only(rig, mp):
    rig.shim(**REVIEWER)
    return Case(["send", "reviewer", "x", "--posture", "none"],
                dict(name="planner", stamp_only=True), 2)


GOT = ("_send_checks", "return got, None")

ROWS = [
    # 1. the flag checks
    Row("reply-to-not-digits", s_reply_to_digits, 2,
        (("_send_checks", "is not a message id"),)),
    Row("audience-shape", s_audience_shape, 2,
        (("_send_checks", "is not a name comms can carry"),)),
    Row("posture-required", s_posture_required, 2,
        (("_send_checks", "POSTURE_REQUIRED"),)),
    Row("new-with-reply-to", s_new_with_reply_to, 2,
        (("_send_checks", "--new says this message"),)),
    Row("cap-and-no-cap", s_cap_both, 2, (("_send_checks", "CAP_BOTH"),)),
    Row("cap-shape", s_cap_shape, 2, (("_send_checks", "CAP_SHAPE"),)),
    Row("block", s_block, 3, (("_send_checks", "block is on in comms' config"),)),
    # 2. the daemon block
    Row("daemon-offline", s_daemon_offline, 4,
        (("_send_checks", "The daemon performs tier/route"),)),
    Row("verb-protocol-mismatch", s_verb_protocol, 4,
        (("_send_checks", "except VerbProtocolMismatch"),)),
    Row("resolve-refused", s_resolve_refused, 2, (("_send_checks", "_refused_tag"),)),
    Row("send-only", s_send_only, 2, (("_send_checks", "send_only_refusal"),)),
    Row("standing-without-row", s_standing, 2,
        (("_send_checks", "declared standing"),)),
    Row("never-bound", s_never_bound, 2, (("_send_checks", "_never_bound_refusal"),)),
    Row("bound-by-another-session", s_bound_elsewhere, 2,
        (("_send_checks", "is bound by another session"),)),
    # 3. the single-reply refusal
    Row("single-reply", s_single_reply, 2, (("_send_checks", "_ask_refusal"),)),
    # 4. the reply checks
    Row("cap-on-a-new-send-opening-no-thread", s_cap_no_thread_new, 2,
        (("_send_checks", "CAP_NO_THREAD"),)),
    Row("referent-unreadable-gateway", _unreadable(GatewayError("record read failed")), 5,
        (("_send_checks", "could not be read"),)),
    Row("referent-unreadable-offline", _unreadable(DaemonOffline("daemon down")), 4,
        (("_send_checks", "could not be read"),)),
    Row("reply-to-no-reply", s_no_reply, 2,
        (("_reply_checks", "NO_REPLY_REFUSED"), GOT)),
    Row("open-on-a-once-ask", s_once_not_open, 2,
        (("_reply_checks", "ONCE_NOT_OPEN"), GOT)),
    Row("thread-closed-by-cap", s_closed_by_cap, 2,
        (("_reply_checks", "CLOSED_BY_CAP"), GOT)),
    Row("thread-closed-by-none", s_closed_by_none, 2,
        (("_reply_checks", "CLOSED_BY_NONE"), GOT)),
    Row("cap-on-a-reply-in-no-thread", s_cap_no_thread_reply, 2,
        (("_reply_checks", "CAP_NO_THREAD"), GOT)),
    Row("cap-in-a-closed-thread", s_cap_closed, 2, (("_reply_checks", "CAP_CLOSED"), GOT)),
    Row("cap-not-the-opener", s_cap_not_opener, 2,
        (("_reply_checks", "CAP_NOT_OPENER"), GOT)),
    Row("cap-below-the-count", s_cap_below, 2, (("_reply_checks", "CAP_BELOW"), GOT)),
    # 5. the cache reads
    Row("cold-not-held", s_cold_refused, 6, (("_send_checks", "EXIT_COLD"),)),
    # 6. the audience table
    Row("reply-changes-the-audience", s_audience_changed, 2,
        (("_send_checks", "_reply_audience"),)),
    # after the checks: the bodies and the credential (no guarded site for the last)
    Row("over-the-cap", s_over_the_cap, 2,
        (("_bodies", "cap on one message"),)),
    Row("no-posting-credential", s_no_credential, 5),
    # before the checks: who is sending (`_send`, not guarded)
    Row("stamp-without-a-session", s_stamp_only, 2),
]


def test_every_refusal_site_has_a_row():
    """Each refusal `return` in `_send_checks`, `_reply_checks` and `_bodies` is claimed by
    a row, and each row's anchor names exactly one site. A refusal added later fails
    here until the table has a row that reaches it."""
    sites = refusal_sites()
    claimed = {site_of(func, anchor) for row in ROWS for func, anchor in row.sites}
    missing = {key: seg.strip().splitlines()[-2:] for key, seg in sites.items()
               if key not in claimed}
    assert not missing, f"refusals with no row in the --check table: {missing}"
    assert claimed <= set(sites)


def test_check_stop_is_immediately_before_the_post_loop():
    """No send refusal can fit between the passing `--check` stop and posting."""
    source = textwrap.dedent(inspect.getsource(cli._send_resolved))
    statements = ast.parse(source).body[0].body

    def is_check_stop(node):
        return (isinstance(node, ast.If)
                and isinstance(node.test, ast.Call)
                and isinstance(node.test.func, ast.Name)
                and node.test.func.id == "getattr"
                and len(node.test.args) >= 2
                and isinstance(node.test.args[1], ast.Constant)
                and node.test.args[1].value == "check")

    [index] = [n for n, node in enumerate(statements) if is_check_stop(node)]
    one, posting = statements[index + 1:index + 3]
    assert (isinstance(one, ast.Assign)
            and len(one.targets) == 1
            and isinstance(one.targets[0], ast.Tuple)
            and [e.id for e in one.targets[0].elts] == ["body"]
            and isinstance(one.value, ast.Name)
            and one.value.id == "bodies")
    assert (isinstance(posting, ast.Try)
            and posting.body
            and isinstance(posting.body[0], ast.Assign)
            and posting.body[0].targets[0].id == "msg"
            and isinstance(posting.body[0].value, ast.IfExp)
            and posting.body[0].value.body.func.id == "post")


def test_the_site_reader_finds_send_checks_refusals():
    """The reader's own pin: it sees the refusals the table names, so an empty read can't
    pass the table above."""
    sites = refusal_sites()
    by_func = {}
    for func, _line in sites:
        by_func[func] = by_func.get(func, 0) + 1
    assert by_func["_send_checks"] >= 20
    assert by_func["_reply_checks"] >= 7
    assert by_func["_bodies"] == 1


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_check_refuses_as_send_does(rig, monkeypatch, doors, row):
    """`--check` prints send's own refusal, with send's own exit code, through the same
    site; and it posts, nudges and records nothing."""
    case = row.setup(rig, monkeypatch)
    settle()
    before = snapshot(rig)
    nposts, nnudge = len(doors["posts"]), len(doors["nudges"])
    nnotify = len(doors["notify"])
    with Tracer() as checked:
        code_c, out_c = rig.cli(with_check(case.argv), **case.who, clock=case.clock)
    settle()
    assert snapshot(rig) == before
    assert len(doors["posts"]) == nposts and len(doors["nudges"]) == nnudge
    assert "nudge" not in doors["notify"][nnotify:]
    with Tracer() as sent:
        code_s, out_s = rig.cli(case.argv, **case.who, clock=case.clock)
    assert code_s == row.code, out_s
    assert (code_c, out_c) == (code_s, out_s)
    assert not out_c.startswith(cli.WOULD_SEND)
    assert checked == sent
    assert checked == {site_of(func, anchor) for func, anchor in row.sites}
    assert len(doors["posts"]) == nposts, "send refused too"



#: Each row's refusal as data (`SendResult.refusal.code`): what a library caller branches
#: on in place of the text and the exit number.
CODES = {
    "reply-to-not-digits": "reply-to-shape", "audience-shape": "audience-shape",
    "posture-required": "posture-required", "new-with-reply-to": "new-with-reply-to",
    "cap-and-no-cap": "cap-both", "cap-shape": "cap-shape", "block": "blocked",
    "daemon-offline": "offline", "verb-protocol-mismatch": "protocol-mismatch",
    "resolve-refused": "resolve-refused", "send-only": "send-only",
    "standing-without-row": "not-standing", "never-bound": "never-bound",
    "bound-by-another-session": "bound-elsewhere", "single-reply": "single-reply",
    "cap-on-a-new-send-opening-no-thread": "cap-no-thread",
    "referent-unreadable-gateway": "referent-unreadable",
    "referent-unreadable-offline": "referent-unreadable",
    "reply-to-no-reply": "no-reply", "open-on-a-once-ask": "once-not-open",
    "thread-closed-by-cap": "thread-closed", "thread-closed-by-none": "thread-closed",
    "cap-on-a-reply-in-no-thread": "cap-no-thread", "cap-in-a-closed-thread": "cap-closed",
    "cap-not-the-opener": "cap-not-opener", "cap-below-the-count": "cap-below",
    "cold-not-held": "cache-unread", "reply-changes-the-audience": "audience-differs",
    "over-the-cap": "too-long", "no-posting-credential": "credential",
    "stamp-without-a-session": "no-claim-socket",
}


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_every_refusal_is_data_with_its_code(rig, monkeypatch, row):
    """The verb renders the library's result: each refusal carries its code and the
    verb's own words as data, nothing posted, and the outcome its exit code names."""
    case = row.setup(rig, monkeypatch)
    settle()
    seen = []
    real = cli._render

    def spy(ctx, result):
        seen.append(result)
        return real(ctx, result)

    monkeypatch.setattr(cli, "_render", spy)
    code, out = rig.cli(case.argv, **case.who, clock=case.clock)
    (result,) = seen
    assert code == row.code == result.exit_code, out
    assert result.refusal is not None and result.refusal.code == CODES[row.id], result
    assert result.refusal.text in out and out.endswith(result.refusal.text + "\n")
    assert result.message_id is None and result.recipients == ()
    assert "\n".join(result.lines()) + "\n" == out
    assert set(CODES) == {r.id for r in ROWS}

# --------------------------------------------------------------------------- passing

def test_a_passing_check_says_would_send_and_posts_nudges_and_records_nothing(rig, doors):
    _planner, reviewer = pair(rig)
    argv = ["send", "reviewer", "hello there", "--posture", "once"]
    settle()
    before = snapshot(rig)
    code, out = rig.cli(with_check(argv), **PLANNER)
    assert (code, out) == (0, f"would send 1 message(s) to {R}\n")
    settle()
    assert snapshot(rig) == before
    assert doors["posts"] == [] and doors["nudges"] == []
    assert "nudge" not in doors["notify"]
    assert reviewer.queue == []
    # The same command without --check posts, and the snapshot sees it: it is not blind.
    code, out = rig.cli(argv, **PLANNER)
    assert code == 0 and out.startswith("sent ")
    assert len(doors["posts"]) == 1 and len(doors["nudges"]) == 1
    wait_until(lambda: reviewer.queue)
    settle()
    after = snapshot(rig)
    assert after["rooms"] != before["rooms"] and after["queues"] != before["queues"]
    assert after["daemon"]["delivered"] != before["daemon"]["delivered"]
    assert before["files"]
    if rig.backend == "local":
        assert after["files"] != before["files"], "the record file is in the snapshot"


def test_a_passing_check_records_no_ask_for_the_single_reply_check(rig):
    """A once check opens no ask: the recipient's plain send to the checker still goes."""
    pair(rig)
    code, out = rig.cli(with_check(["send", "reviewer", "q?", "--posture", "once"]),
                        **PLANNER)
    assert code == 0, out
    code, out = rig.cli(["send", "planner", "unrelated", "--posture", "none"], **REVIEWER)
    assert code == 0 and out.startswith("sent "), out


def test_a_check_held_for_a_cold_recipient_prints_the_held_lines_send_prints(rig, doors):
    rig.shim(**PLANNER)
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    write_transcript(sid, 2 * HOUR)
    reviewer = rig.shim(**REVIEWER, session_id=sid)
    argv = ["send", "reviewer", "for when you are back", "--posture", "none"]
    settle()
    before = snapshot(rig)
    code, out = rig.cli(with_check(argv), **PLANNER)
    settle()
    assert snapshot(rig) == before and doors["posts"] == []
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == f"would send 1 message(s) to {R}"
    assert len(lines) == 2 and lines[1].startswith("held: ")
    assert reviewer.queue == []
    code, sent = rig.cli(argv, **PLANNER)
    assert code == 0, sent
    held = [ln for ln in sent.splitlines() if ln.startswith("held: ")]
    assert lines[1:] == held


def test_a_check_to_several_names_names_them_all(rig):
    planner, reviewer = pair(rig)
    critic = rig.shim(slot=3, name="critic")
    code, out = rig.cli(with_check(["send", "reviewer,critic", "hi", "--posture", "none"]),
                        **PLANNER)
    assert (code, out) == (0, f"would send 1 message(s) to {R}, critic\n")
    settle()
    assert reviewer.queue == [] and critic.queue == []


def test_the_launch_console_argv_parses_and_passes(rig):
    """A send-only console's form: `send --check` first, then the flags, `--` before the
    positionals, and a text that starts with `-`."""
    pair(rig)
    code, out = rig.cli(["send", "--check", "--posture", "none", "--new", "--",
                         "reviewer", "-x is the brief"], **PLANNER)
    assert (code, out) == (0, f"would send 1 message(s) to {R}\n")
    code, out = rig.cli(["send", "--check", "--new", "--", "reviewer", "-x"], **PLANNER)
    assert code == 2 and out.strip() == cli.POSTURE_REQUIRED


def test_the_operators_lane_checks_too(rig, doors):
    if rig.backend != "local":
        pytest.skip("the mock provisions no operator credential; send refuses there too")
    reviewer = rig.shim(**REVIEWER)
    env = {"MOXI_COMMS_OPERATOR": rig.prov.operator_user_id}
    code, out = rig.cli(["send", "--check", "reviewer", "from the terminal"], env=env)
    assert (code, out) == (0, f"would send 1 message(s) to {R}\n")
    settle()
    assert doors["posts"] == [] and doors["nudges"] == [] and reviewer.queue == []


def test_reply_takes_check(rig, doors):
    """comms/refine §7.7: `moxi comms reply --check` runs the reply's checks and stops at the
    post, as `send --check` does, after its "replying to" line."""
    assert cli.build_parser().parse_args(["reply", "1", "x", "--check"]).check
    pair(rig)
    ask = ok(rig, PLANNER, ["send", "reviewer", "q?", "--posture", "once"])
    settle()
    before, nposts, nnudges = snapshot(rig), len(doors["posts"]), len(doors["nudges"])
    code, out = rig.cli(["reply", ask, "a", "--check"], **REVIEWER)
    assert (code, out.splitlines()) == (0, [f"replying to {P} (chosen, verified) in #names",
                                            f"would send 1 message(s) to {P}"])
    settle()
    assert snapshot(rig) == before and len(doors["posts"]) == nposts
    assert len(doors["nudges"]) == nnudges


def test_the_help_names_the_check():
    s = cli.build_parser()._subparsers._group_actions[0].choices["send"]
    help_text = s.format_help()
    assert "--check" in help_text and "would send N message(s) to NAMES" in " ".join(
        help_text.split())
