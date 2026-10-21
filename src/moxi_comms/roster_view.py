"""The ONE rendering of the roster, in a module that pulls nothing behind it.

The `agents` verb and the session-start snapshot call the same function, so the injected
snapshot is the verb's output verbatim by construction rather than by a second rendering
kept in sync.

**Why it lives here rather than in `cli`.** If `adapters/shim_roster` imported it from
`cli`, then `cli`, and everything `cli` imports (the card reader among them), would sit
inside an adapter's import closure. Here, `cli` is a leaf -- nothing in the package
imports it -- and a shared renderer is substrate both surfaces use, while a CLI is not
something an adapter should have to import at all. Pinned by
`tests/test_cards.py::test_only_the_cli_imports_the_card_reader`.
"""

from __future__ import annotations

import time

#: The adapter kind a send-only identity binds with (`daemon.SEND_ONLY_ADAPTER`, spelled
#: here so this module imports nothing behind it). Such a name sends and is never a
#: recipient, so no agent-facing roster lists it; only `--seats` does.
SEND_ONLY_ADAPTER = "send-only"

#: The empty roster: no binding anyone can message.
NO_BINDINGS = "no live bindings — nothing is addressable right now."

#: The `--seats` waiting block's header: what one of its rows means.
WAITING_HEADER = ("waiting (queued for each name until its next session start, /clear, "
                  "compaction, or a turn of its own on a warm cache):")


def messageable(bindings) -> list:
    """The bindings an agent can message: every live binding whose adapter is not
    `send-only`. `agent-N` is one while it is bound; so is a session the operator binds,
    a row like any other."""
    return [b for b in bindings if b.get("adapter") != SEND_ONLY_ADAPTER]


def render_agents(info, *, seats: bool = False, now: float | None = None) -> str:
    """The ONE rendering of the roster: the `agents` verb and the launch snapshot
    (`moxi-comms-shim-roster`) both call this, so the injected snapshot is the verb's
    output verbatim by construction — one rendering exists and cannot drift from
    what an agent sees when it re-runs the verb.

    The default view is the names an agent can message, one row each, and nothing else:
    it reads `bindings` alone. `seats=True` (the `--seats` flag, the operator's view)
    lists every binding with its slot, send-only ones included, then the mail waiting
    for each name (`waiting`), the unanswered rows and the slot keeps. A slot is
    webhook-bundle capacity, provisioner inventory, so no agent-facing surface names
    one; the snapshot never asks for `--seats`. `now` (default: the wall clock) ages
    the waiting rows.
    """
    rows = info["bindings"] if seats else messageable(info["bindings"])
    lines = [] if rows else [NO_BINDINGS]
    for b in rows:
        # A send-only binding holds no slot (`slot` is null): `no slot`, same width.
        seat = (("no slot  " if b["slot"] is None else f"slot {b['slot']:<3} ")
                if seats else "")
        lines.append(f"{b['qualified']:<24} {b['tier']:<10} {seat}"
                     f"adapter {b['adapter']}")
    if not seats:
        return "\n".join(lines)
    now = time.time() if now is None else now
    # One queue, one block: a row is the count waiting for the name, for whatever
    # reason (offline, cold, or flagged `--held`; none is stored), and the oldest's wait.
    if info["waiting"]:
        lines.append("")
        lines.append(WAITING_HEADER)
        for name, row in sorted(info["waiting"].items()):
            lines.append(f"  {name:<24} {row['count']} message(s), oldest waiting "
                         f"{waited(now - row['oldest'])}")
    # Ignored reminders: recorded where the owed party sees them, with no timer.
    for row in info["unanswered"]:
        lines.append(f"unanswered: {row['owing']} owes {row['author']} msg "
                     f"{row['message_id']}, {waited(row['for'])}")
    # Slot keeps: a slot held for a name a restart has not brought back yet. JSON makes
    # the slot keys strings.
    keeps = info["keeps"]
    if keeps:
        lines.append("")
        lines.append("slot keeps (held for a name a restart has not brought back yet):")
        for slot, row in sorted(keeps.items(), key=lambda kv: int(kv[0])):
            lines.append(f"  slot {str(slot):<3} {row['qualified']:<24} "
                         f"{int(row['expires_in'])}s left")
    return "\n".join(lines)


def waited(seconds: float) -> str:
    """`42m`, `5h 12m`, `2d 3h`: how long the oldest waiting message has waited."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"

