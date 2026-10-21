"""The OpenCode launch model: the shipped zero-retention defaults, in order, chosen from a
model list OpenCode refreshed for this launch.

A row that names no model gets `DEFAULT_MODELS`. Before a launch starts its server it has
OpenCode refresh its model list, within `MODELS_REFRESH_TIMEOUT`, so a model retired since
OpenCode last fetched the list is not offered and the next default is chosen. A refresh
that fails or overruns leaves the list as it stands, and the launch says so with the
list's age. A server handed in to the launcher gets no refresh.

The servers here are the stub `opencode` (`tests/fake_opencode.py`), spawned for real, or
the in-process fake. No model, no network.
"""

from __future__ import annotations

import itertools
import os
import signal
import time

import pytest

import fake_opencode
from fake_opencode import FakeOpenCode
from moxi_comms.adapters import opencode as oc
from moxi_comms.adapters import opencode_roster
from moxi_comms.adapters.opencode import OpenCodeAdapter

IDENTITY = "coder@proj-a"
PASSWORD = "test-password"
MINIMAX = "opencode/minimax-m3.1-flash-free"
GLM = "opencode/glm-5.3-flash-free"
DEEPSEEK = "opencode/deepseek-v4.1-flash-free"
PICKLE = "opencode/big-pickle"


# ------------------------------------------------------------------- the shipped list

def test_the_shipped_defaults_are_the_zero_retention_free_models_in_order():
    assert opencode_roster.DEFAULT_MODELS == (MINIMAX, GLM, DEEPSEEK)
    assert opencode_roster.DEFAULT_MODEL == MINIMAX, "an alias of the first"


def test_a_row_that_names_no_model_gets_the_whole_list(tmp_path):
    roster = opencode_roster.from_table(
        {"agents": {IDENTITY: {"slot": 5, "cwd": str(tmp_path)}}})
    assert roster.agents[IDENTITY].models == [MINIMAX, GLM, DEEPSEEK]
    named = opencode_roster.from_table(
        {"agents": {IDENTITY: {"slot": 5, "cwd": str(tmp_path), "model": PICKLE}}})
    assert named.agents[IDENTITY].models == [PICKLE], "a named model is kept as it is"


def test_a_later_shipped_default_is_chosen_with_a_notice_that_says_which():
    model, notice = oc.choose_default(list(opencode_roster.DEFAULT_MODELS), [PICKLE, GLM])
    assert model == GLM
    assert notice == oc.SHIPPED_DEFAULT.format(wanted=MINIMAX, model=GLM)
    assert "one of comms' shipped zero-retention defaults" in notice
    assert "not the zero-retention one" not in notice


def test_with_none_of_the_shipped_defaults_offered_the_fallback_and_its_notice_hold():
    model, notice = oc.choose_default(list(opencode_roster.DEFAULT_MODELS), [PICKLE])
    assert (model, notice) == (PICKLE, oc.FALLBACK_MODEL.format(wanted=MINIMAX,
                                                                model=PICKLE))
    assert oc.choose_default(list(opencode_roster.DEFAULT_MODELS), ["local/x"]) == \
        (None, oc.NO_FREE_MODEL)


# ------------------------------------------------------------------- the launch

class Handle:
    """The in-process fake as a handed-in server: already listening, never respawned."""

    def __init__(self, fake):
        self.fake = fake
        self.alive = True
        self.returncode = None

    def wait_listening(self, timeout=None):
        return self.fake.url

    def stop(self):
        pass


def unnamed_row(cwd) -> opencode_roster.OpenCodeAgent:
    """The roster row with no `model`, as the config file gives it."""
    roster = opencode_roster.from_table({"agents": {IDENTITY: {"slot": 5, "cwd": str(cwd)}}})
    return roster.agents[IDENTITY]


@pytest.fixture
def launch(rig, tmp_path, monkeypatch):
    """Launch the row with no model: spawned on the stub `opencode`, or (`free=...`)
    on a handed-in in-process fake offering those models."""
    records = tmp_path / "rec"
    stub = fake_opencode.install(tmp_path / "bin")
    monkeypatch.setenv("FAKE_OPENCODE_DIR", str(records))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    for var in ("OPENCODE_MODELS_URL", "OPENCODE_MODELS_PATH", "FAKE_OPENCODE_REFRESH",
                "FAKE_OPENCODE_FREE", "FAKE_OPENCODE_FREE_REFRESHED"):
        monkeypatch.delenv(var, raising=False)
    made, fakes = [], []

    def build(*, free=None, start=True):
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        kw = {}
        if free is not None:
            fake = FakeOpenCode(password=PASSWORD, free=free, heartbeat=0.5).start()
            fakes.append(fake)
            kw = {"spawn": False, "server": Handle(fake)}
        statuses: list = []
        adapter = OpenCodeAdapter(unnamed_row(work), binary=str(stub), password=PASSWORD,
                                  backoff=itertools.repeat(0.05), clock=rig.clock,
                                  status_sink=statuses.append, **kw)
        adapter.statuses = statuses
        if free is not None:
            fakes[-1].on_log = adapter.on_log_line
        made.append(adapter)
        if start:
            adapter.start()
        return adapter

    build.events = lambda kind=None: [e for e in fake_opencode.events(records)
                                      if kind is None or e["event"] == kind]
    build.work = tmp_path / "work"
    yield build
    for adapter in made:
        adapter.stop()
    for fake in fakes:
        fake.stop()
    for e in build.events("serve"):
        try:
            os.kill(e["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_row_with_no_model_launches_on_the_first_shipped_default_with_no_notice(launch):
    adapter = launch(free=[PICKLE, MINIMAX])
    assert adapter.default_model == MINIMAX
    assert adapter.launch_notice is None


def test_a_later_shipped_default_launches_with_the_shipped_default_notice(launch):
    adapter = launch(free=[PICKLE, GLM])
    assert adapter.default_model == GLM
    assert adapter.launch_notice == oc.SHIPPED_DEFAULT.format(wanted=MINIMAX, model=GLM)
    assert any("shipped zero-retention defaults" in s for s in adapter.statuses)


def test_the_model_is_chosen_from_the_list_refreshed_before_the_server_starts(
        launch, monkeypatch):
    """The stale list: OpenCode's cached list still offers minimax, and the list the
    refresh fetches does not. The launch chooses glm, from the fetched list."""
    monkeypatch.setenv("FAKE_OPENCODE_FREE", f"{PICKLE},{MINIMAX},{GLM}")
    monkeypatch.setenv("FAKE_OPENCODE_FREE_REFRESHED", f"{PICKLE},{GLM}")
    adapter = launch()
    assert adapter.default_model == GLM
    assert adapter.launch_notice == oc.SHIPPED_DEFAULT.format(wanted=MINIMAX, model=GLM)
    kinds = [e["event"] for e in launch.events() if e["event"] in ("models-refresh", "serve")]
    assert kinds[0] == "models-refresh", "the refresh ran before any server started"
    assert kinds.count("models-refresh") == 1
    (refresh,) = launch.events("models-refresh")
    assert refresh["argv"] == ["models", "--refresh", "--pure", "--print-logs",
                               "--log-level", "ERROR", "opencode"]
    assert refresh["password"] is True, "under the server's environment"
    assert refresh["cwd"] == str(launch.work)
    assert launch.events("serve")[-1]["config"]["model"] == GLM


def test_a_refresh_that_works_says_nothing(launch, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_FREE", f"{MINIMAX},{PICKLE}")
    adapter = launch()
    assert launch.events("models-refresh")
    assert (adapter.default_model, adapter.launch_notice) == (MINIMAX, None)


def models_file() -> str:
    return os.path.join(os.environ["XDG_CACHE_HOME"], "opencode", "models.json")


def write_models_list(age: float) -> None:
    path = models_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{}")
    then = time.time() - age
    os.utime(path, (then, then))


@pytest.mark.parametrize("mode, reason", [
    ("fetch-fail", "Failed to fetch models.dev"),
    ("exit", "exit 1"),
    ("slow", "no answer within 0.5s"),
])
def test_a_refresh_that_fails_leaves_the_list_as_it_stands_and_says_so_with_its_age(
        launch, monkeypatch, mode, reason):
    monkeypatch.setattr(oc, "MODELS_REFRESH_TIMEOUT", 0.5, raising=False)
    monkeypatch.setenv("FAKE_OPENCODE_REFRESH", mode)
    monkeypatch.setenv("FAKE_OPENCODE_FREE", f"{PICKLE},{MINIMAX},{GLM}")
    monkeypatch.setenv("FAKE_OPENCODE_FREE_REFRESHED", f"{PICKLE},{GLM}")
    write_models_list(age=2 * 3600 + 30)
    started = time.monotonic()
    adapter = launch()
    assert adapter.default_model == MINIMAX, "chosen from the list as it stands"
    stale = oc.MODELS_STALE.format(reason=reason, age="last written 2 h ago")
    assert adapter.launch_notice == stale
    assert stale in adapter.statuses[0]
    assert stale.startswith("OpenCode's model list could not be refreshed (")
    if mode == "slow":
        assert time.monotonic() - started < 15, "the refresh is bounded"


@pytest.mark.parametrize("pointed", [None, "OPENCODE_MODELS_URL", "OPENCODE_MODELS_PATH"])
def test_a_list_of_unknown_age_says_so(launch, monkeypatch, pointed):
    monkeypatch.setenv("FAKE_OPENCODE_REFRESH", "fetch-fail")
    monkeypatch.setenv("FAKE_OPENCODE_FREE", MINIMAX)
    if pointed is None:
        pass                                    # no list file at all
    else:
        write_models_list(age=60)               # a file, but not the one OpenCode reads
        monkeypatch.setenv(pointed, "http://127.0.0.1:9" if pointed.endswith("URL")
                           else "/elsewhere/models.json")
    adapter = launch()
    assert adapter.launch_notice == oc.MODELS_STALE.format(
        reason="Failed to fetch models.dev", age="of unknown age")


def test_a_stale_list_and_a_fallback_are_both_said(launch, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_REFRESH", "exit")
    monkeypatch.setenv("FAKE_OPENCODE_FREE", f"{PICKLE},{GLM}")
    adapter = launch()
    assert adapter.default_model == GLM
    assert adapter.launch_notices == [
        oc.MODELS_STALE.format(reason="exit 1", age="of unknown age"),
        oc.SHIPPED_DEFAULT.format(wanted=MINIMAX, model=GLM)]
    assert len(adapter.statuses) == 2


def test_a_handed_in_server_gets_no_refresh(launch):
    adapter = launch(free=[MINIMAX])
    assert adapter.default_model == MINIMAX
    assert launch.events("models-refresh") == []
    assert adapter.launch_notice is None


def test_the_shown_notice_puts_each_on_its_own_line(launch, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_OPENCODE_REFRESH", "exit")
    monkeypatch.setenv("FAKE_OPENCODE_FREE", f"{PICKLE},{GLM}")
    adapter = launch()
    oc.show_notice(adapter, 0)
    err = capsys.readouterr().err.splitlines()
    assert err == [f"moxi-comms-opencode: {IDENTITY}: {n}" for n in adapter.launch_notices]


@pytest.mark.parametrize("age, said", [(5, "5 s"), (125, "2 min"), (7300, "2 h"),
                                       (3 * 86400 + 60, "3 d")])
def test_the_age_reads_in_one_unit(age, said):
    assert oc.age_words(age) == said
