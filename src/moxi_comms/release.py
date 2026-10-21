"""The public name on every machine surface, built from moxi.names and the module's name.

The name is Moxi comms: the harness Moxi, the org `moxihq` and the module `comms`.
`MODULE` is the one spelling of the module here; the harness and the org are the core's
(`moxi.names`). Everything below is built from them, and `tests/test_release.py` holds
`pyproject.toml` to them.

The import package is `moxi_comms`, the commands run as `moxi comms <verb>`, and the
internal entry points are `moxi-comms-<part>`.
"""

from __future__ import annotations

from moxi import names

#: This module's name, from which `moxi.names` spells every other name.
MODULE = "comms"

#: The PyPI distribution: harness-module.
DISTRIBUTION = names.distribution(MODULE)

#: What a person reads: descriptions, the unit's Description=.
DISPLAY_NAME = f"{names.HARNESS.capitalize()} {MODULE}"

#: The systemd user unit `moxi comms service install` writes.
UNIT = f"{DISTRIBUTION}-daemon.service"

#: The public repository: the org and the module's plain name.
REPO_URL = f"https://github.com/{names.repo(MODULE)}"

__all__ = ["MODULE", "DISTRIBUTION", "DISPLAY_NAME", "UNIT", "REPO_URL"]
