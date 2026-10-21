"""Cursors are keyed by backend.

A cursor is a position in ONE record's id space, so each backend's cursors are filed in
their own directory and a flip in either direction starts from that backend's own
positions.

The mock keeps the bare directory (`tests/test_paths.py` pins it). A `discord/` directory
and the bare files from before the split are left where they are and never read on the
local record.
"""

from __future__ import annotations

import json

import pytest

from moxi_comms import cursors, gateway
from moxi_comms.paths import cursors_dir

IDENT = "curator@proj-a"
OLD_ID = "1400000000001"     # an id from before the local record's counter base
LOCAL_ID = str(2 ** 62 + 7)  # the local counter's base


@pytest.fixture
def on_backend(monkeypatch):
    """Put the process on a backend the way a box does: MOXI_COMMS_GATEWAY. The real
    `gateway.backend_name` chain (flag > MOXI_COMMS_GATEWAY > config.toml > `local`)
    is what cursors.py calls, and it is what these tests exercise."""
    def use(name: str) -> None:
        monkeypatch.setenv("MOXI_COMMS_GATEWAY", name)

    return use


def bare(qualified: str = IDENT):
    return cursors_dir() / f"{qualified}.json"


def keyed(backend: str, qualified: str = IDENT):
    return cursors_dir() / backend / f"{qualified}.json"


def write_cursor(path, cursor: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cursor": cursor, "updated_at": "2026-09-12T00:00:00+00:00"}))


# ------------------------------------------------------------------- the directory split

def test_the_mock_keeps_the_bare_directory(on_backend):
    """The mock is one record per store file with no flip to make. The bare path is
    pinned by `tests/test_paths.py`."""
    on_backend(gateway.MOCK)
    assert cursors.advance(IDENT, OLD_ID) == OLD_ID
    assert bare().exists()
    assert not (cursors_dir() / gateway.MOCK).exists()
    assert cursors.read(IDENT) == OLD_ID


def test_an_unresolvable_backend_falls_back_to_the_bare_directory(monkeypatch):
    """`advance` runs inside the adapter's Stop hook, so a MOXI_COMMS_GATEWAY naming a backend
    that does not exist degrades to the pre-split layout instead of raising out of it."""
    monkeypatch.setenv("MOXI_COMMS_GATEWAY", "no-such-backend")
    assert cursors.advance(IDENT, OLD_ID) == OLD_ID
    assert bare().exists()
    assert cursors.read(IDENT) == OLD_ID


# ------------------------------------------------- what an older install left behind

def test_local_writes_under_local_and_never_bare(on_backend):
    """`cursors_dir() / <backend> / f"{qualified}.json"`, resolved inside cursors.py."""
    on_backend(gateway.LOCAL)
    cursors.advance(IDENT, LOCAL_ID)
    assert keyed(gateway.LOCAL).exists()
    assert not bare().exists()
    assert cursors.read_meta(IDENT)["cursor"] == LOCAL_ID


def test_old_cursor_files_are_left_alone_and_never_read(on_backend):
    """A bare file and a `discord/` file from before comms was local-only stay where they
    are: the local record starts fresh beside them and writes neither."""
    write_cursor(bare(), OLD_ID)
    write_cursor(keyed("discord"), OLD_ID)
    before = (bare().read_text(), keyed("discord").read_text())
    on_backend(gateway.LOCAL)

    assert cursors.read(IDENT) is None
    cursors.advance(IDENT, LOCAL_ID)
    assert cursors.read(IDENT) == LOCAL_ID
    assert (bare().read_text(), keyed("discord").read_text()) == before
