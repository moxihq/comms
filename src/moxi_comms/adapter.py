"""What an adapter built outside comms uses (`moxi_comms.adapter`), public beside
`moxi_comms.api`.

An adapter outside comms binds its own name with the daemon, keeps its own cursor and
renders mail as comms' own adapters do:

* `DaemonClient`, `DaemonError`, `DaemonOffline`: one connection to the daemon, a
  refusal, the socket gone. `backoff_delays()`: the reconnect backoff comms' adapters use.
* `cursors`: the cursor files (`cursors.read`, `cursors.write`, ...).
* `render_digest(payloads, ...)` and `frame_line(payload, ...)`: delivered mail as a
  digest, and one payload's frame line.
* The attribution tiers and marks: `TIER_EPHEMERAL`, `TIER_CHOSEN`, `TIER_STANDING`,
  `TIER_OPERATOR`, `TIER_UNKNOWN`, `VERIFIED`, `LABELED`, `UNVERIFIED`.
* `parse_machine_line(body)`: a message's line zero as a `MachineLine`, or None when it
  is prose.
"""

from __future__ import annotations

from . import cursors
from .adapters.claude_shim import frame_line, render_digest
from .attribution import (LABELED, TIER_CHOSEN, TIER_EPHEMERAL, TIER_OPERATOR,
                          TIER_STANDING, TIER_UNKNOWN, UNVERIFIED, VERIFIED)
from .client import DaemonClient, DaemonError, DaemonOffline, backoff_delays
from .machine_line import MachineLine
from .machine_line import parse as parse_machine_line

__all__ = ["DaemonClient", "DaemonError", "DaemonOffline", "backoff_delays", "cursors",
           "render_digest", "frame_line", "TIER_EPHEMERAL", "TIER_CHOSEN",
           "TIER_STANDING", "TIER_OPERATOR", "TIER_UNKNOWN", "VERIFIED", "LABELED",
           "UNVERIFIED", "parse_machine_line", "MachineLine"]
