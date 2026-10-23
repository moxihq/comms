# Changelog

## 0.1.1 (2026-10-22)

- The directory's webhook column that holds a credential's store pointer is named
  `secret_id` in a directory this release creates. An existing directory keeps the
  column name it was created with: nothing renames it, and every reader and writer takes
  the name from the table it opens, so 0.1.0 and 0.1.1 processes can run side by side
  on a directory 0.1.0 created. A directory 0.1.1 creates can't be read by 0.1.0.
- One of the retired `[policy]` keys is no longer declared, so a config file that sets
  it is refused as an unknown key, with the same exit and the generic fix ("remove it,
  or check its spelling"), rather than as a retired one. `[claude] channel`, `[policy]
  never_held` and `[policy] next_turn_refused` stay declared, each refused with its fix.
- Docs, docstrings, comments and tests are cleaned up: they state each rule in plain
  words, and the test suite uses made-up names throughout.
- The source distribution lists the paths it includes instead of the paths it leaves out.
- The other Moxi modules (`moxi-sessions`, `moxi-turns`, `moxi-cache`) now ship a README
  and a LICENSE.

## 0.1.0 (2026-10-21)

The first public release of Moxi comms: messages between AI coding agents on one
machine, delivered into each recipient's session at a turn boundary.

- A local daemon, `moxi-comms-daemon`, that routes messages between named agents over a
  SQLite record and Unix sockets, with no server, account or network. Mail to a name that
  isn't running waits in the record and is delivered when a session with that name
  starts; held mail survives daemon restarts.
- The commands run as `moxi comms <verb>`: `send`, `reply`, `agents`, `history`, `help`,
  `whois`, `explain`, `cache`, `doctor` and `service`. `moxi shortcut comms` adds a bare
  `comms` command. `moxi comms service install` runs the daemon as a systemd user unit,
  and its first start provisions the directory (6 slots and the operator identity).
- Claude Code sessions through Moxi turns: `moxi comms claude` (also
  `moxi-comms-claude`) launches a named session with an adapter process of its own,
  which binds the name, hands each digest to turns at a turn boundary, settles it from
  the turn's outcome, and holds the session's Stops for reply enforcement. comms writes
  no Claude hook or settings file; its launch context (the context block, the roster and
  its deltas) reaches the session through comms' listener, registered with turns.
- Experimental adapters for Codex (`moxi-comms-codex`) and OpenCode
  (`moxi-comms-opencode`), each launched from a roster in comms' config file.
- Postures: every new send says what it wants back with `--posture none|optional|once|open`.
  A reply takes its posture from what it answers. `open` starts a thread with limits
  (`[threads] reply_cap`, `--cap N` or `--no-cap`). The Stop hold reminds a session of
  every verified `[reply-once]` ask it owes and of the operator's mail.
- Attribution and the custody journal: the record says who sent each message, and a
  send's identity is its session's live claim, read through the claim socket its
  adapter serves. The operator's own terminal (`MOXI_COMMS_OPERATOR`) is the one lane
  outside every session.
- The cold-cache hold (experimental): a message to an agent whose prompt cache is cold
  is posted and held for that agent's next session start, /clear, compaction or turn,
  from any sender and on any adapter. Cache reads come from the optional cache module
  (`moxi install comms --with cache`); without it every agent reads as having no cache
  and gets its mail live, unless `[cache] required` makes the daemon refuse to start.
- `moxi comms send --check` runs every check a send makes and posts nothing.
- One message holds at most 16,000 characters, routing line included; comms does not
  split a message.
- One config file, `~/.config/moxi/comms/config.toml`, declared in the package and read
  through `moxi config`. A key comms does not declare is refused.
- A library for sending, `moxi_comms.api` (interface 1), which `moxi comms send` and
  `reply` render, and `moxi_comms.adapter` for adapters built outside comms. Adapter
  kinds are declared through the entry-point group `moxi.comms.kinds`.
- Linux only. Python 3.11 or later.
