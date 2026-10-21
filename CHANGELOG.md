# Changelog

## 0.1.0 (unreleased)

comms becomes a module of Moxi.

- The commands run as `moxi comms <verb>`; there is no `comms` script (`moxi shortcut
  comms` adds one). The internal entry points are `moxi-comms-daemon`,
  `moxi-comms-claude`, `moxi-comms-codex`, `moxi-comms-opencode`, `moxi-comms-provision`
  and the shim's `moxi-comms-shim`.
- Claude Code sessions go through Moxi turns. `moxi comms claude` (also
  `moxi-comms-claude`) forks the session's adapter process before it execs `claude`: the
  adapter binds the name and listens on the claim socket before `claude` starts, finds
  the session's turns endpoint, holds the session's Stops for reply enforcement, and hands
  each digest to turns, settling it from the turn's outcome (an Esc reads as an
  interrupted turn). comms writes no Claude hook and no settings file any more: its
  launch context (the context block, the roster and its deltas) reaches the session
  through comms' listener, registered with turns, and `moxi-comms-shim-roster` is gone.
  So are the hook ping `moxi-comms-shim-ping`, its control socket and
  `MOXI_COMMS_SHIM_SOCKET`: no hook of comms' runs it, and turns takes each turn's start
  and end from the session itself.
  comms now requires `moxi-turns`.
- A bound Claude session whose adapter has not found its turns endpoint reads `waiting`
  to a sender, with a NOTE saying how long; `moxi comms doctor` lists such sessions and
  shows turns' own rows.
- `moxi comms claude` names the session's id to `claude` (`--session-id`, unless the
  arguments after `--` name one or resume a session without naming it), and the adapter
  states that id from its bind, so a session with no turns endpoint gets a cache verdict.
  The launcher's own `--session-id` must be a UUID and agree with an id after `--`.
- The import package is `moxi_comms`, and every variable comms reads is spelled
  `MOXI_COMMS_`.
- Files live in comms' own directories under Moxi's roots (`~/.local/share/moxi/comms/`
  and the rest); sockets are in `$XDG_RUNTIME_DIR/moxi/comms/`, the daemon's socket is
  always there, and a claim socket anywhere else is refused.
- One config file, `~/.config/moxi/comms/config.toml`, declared in the package and read
  through `moxi config`: `gateway.toml`, `codex.toml` and `opencode.toml` become its
  top-level keys and its `[codex]` and `[opencode]` tables. New keys: `[secrets] store`
  and `roles`, `[policy] labels`, `[provision] slots` and `operator`, `[replay]
  messages` and `hours`, and `block`, which replaces the `comms_block` toggle file.
- The config file is read stricter, so a file merged from the old three needs care:
  - a table or key comms does not declare is refused, so drop any table the old
    `gateway.toml` carried for other programs (a `[discord]` or `[mirror]` table, say);
  - a retired key is refused with what to do: `[claude] channel`,
    `[policy] voice_companion`, `[policy] never_held` and `[policy] next_turn_refused`,
    which loaded and were ignored before;
  - `[policy] cache_callers` is the whole list and replaces the default, where it used
    to add to it: a box that adds a caller lists `operator` too.
- The daemon refuses to start on a config file that does not load. Once it runs, a bad
  edit leaves it on its last good settings, and it logs the error once per change of
  the file.
- `moxi-comms-codex --config PATH` and `moxi-comms-opencode --config PATH` name a comms
  config file, read whole, whose `[codex]` or `[opencode]` table is that launch's
  roster in place of the box's file's.
- One behaviour: the profile is gone. The secrets chain is `env, store`, the default
  backend is `local`, the daemon's first start always provisions an empty directory, and
  `moxi comms service install` is allowed on every box. The unit runs this environment's
  `moxi-comms-daemon`, never one found on PATH.
- A library for sending, `moxi_comms.api` (interface 1): `api.send` and `api.check`
  return a `SendResult` (the outcome, the message id, one recipient per name with how it
  is delivered, the refusal's code and text, the notes, and `lines()`, the verb's
  text), from `Sender.session()`, `.socket(path)`, `.claim(server)` or
  `.operator(user_id)`. `moxi comms send` and `reply` render what it returns, with the
  same output and exit codes. Beside it: `ClaimServer`, the reads `attributed`,
  `session`, `roster`, `owner`, `record_dir` and `daemon_socket`, the name helpers,
  `replay()`, `moxi_comms.api.tokens` and, for adapters built outside comms,
  `moxi_comms.adapter`.
- Cache tracking moved to the cache module (`moxi-cache`, the extra `cache`: `moxi
  install comms --with cache`). comms reaches it through one seam, `moxi_comms/cache.py`,
  over `moxi.modules.load("cache", interface=1)`, and keeps the queue, the hold and its
  words. Its own readers, lifetime table and drift record are gone; the drift record is
  cache's (`STATE/cache/drift.json`; `STATE/comms/cache-drift.json` is no longer read).
  A Claude session on the five-minute cache tier now counts five minutes, not an hour.
- Without the cache module every agent reads `no-cache` (code `module-absent`) and gets
  its mail live. The new key `[cache] required` makes the daemon refuse to start when the
  module is absent or broken; `moxi comms doctor` says whether cache is present.
- A module's harness kind is read through its evidence function (`moxi.comms.kinds`),
  and the `session` answer carries the read's `cache_code`. The verbs' protocol
  (`cli_protocol` in the `ping` answer) rises to 2 for it, so a verb and a daemon from
  either side of this change refuse each other (exit 4) instead of misreading the answer.
- Adapter kinds come from the entry-point group `moxi.comms.kinds`; comms declares its
  own four there.
- Removed: the Claude Code channel and comms' plugin and marketplace, the Bitwarden step,
  the overseer heartbeat, the `driver` and `speech` adapter kinds, and the context
  percent: a held send's `held:` line ends after its cold sentence, and `moxi comms
  cache` reads no context size. `moxi comms doctor` loses its profile, files, overseer,
  provider and channel rows.
- A turn end whose cache read is cold no longer misses its release point: the daemon
  reads again for up to 8 s (`RELEASE_SETTLE_STEPS`) and releases held mail at the first
  warm read. A cold send read made while a release of its name is running reads once
  more before it queues.
- An OpenCode roster row that names no `model` gets `DEFAULT_MODELS`, free models whose
  terms keep no data, in order; the first OpenCode offers is chosen, and a launch that
  falls past the first says so (`SHIPPED_DEFAULT`). The old default,
  `opencode/space-bunny-free`, is retired.
- Every OpenCode launch has OpenCode refresh its model list first (about 1.5 s); when the
  refresh fails, the launch goes on with the list as it stands and says how old it is
  (`MODELS_STALE`).
- A Claude session's layer one names the comms to run by path, as Codex's and OpenCode's
  do: `moxi comms claude` stamps its own as `MOXI_COMMS_COMMAND`.

## 0.0.1 (unreleased)

The first public release of Moxi comms.

- A local daemon that routes messages between named agents on one machine, over a
  SQLite record and Unix sockets, with no network.
- The `comms` CLI: `send`, `reply`, `agents`, `history`, `help`, `whois`, `explain`,
  `cache`, `doctor` and `service`.
- `moxi-comms-claude`, which launches a named Claude Code session whose mail arrives at its
  turn boundaries through Claude Code's channel feature.
- A Claude Code plugin and a marketplace in this repository (`plugin` and `approved`
  launch modes, experimental).
- An experimental adapter for Codex (`moxi-comms-codex`).
- Local only: no Discord backend and no Discord mirror. `comms --gateway` takes `mock`,
  and `moxi-comms-daemon --gateway` takes `local` or `mock`. A slot's or a standing
  identity's posting bundle is one `#names` webhook, and `moxi comms reply` always posts in
  `#names`.
- A send's identity is its session's claim, read through the claim socket its adapter
  serves (`MOXI_COMMS_CLAIM_SOCKET`). A launch stamp alone sends nothing: only a live session
  holding a name sends under it. A claim that names no one, as an auto-named session's
  does until its first bind, holds no identity: `whoami` says so and a send is refused.
  The operator's own terminal (`MOXI_COMMS_OPERATOR`) is the one lane outside every session.
- The operator's lane has a phone origin: `moxi comms send --via phone` writes `[via-phone]` on
  line zero, accepted only on the operator's lane from a process outside every session,
  refused from any claim, and shown to the recipient only on a verified operator
  message. It earns no pass through the cold-cache hold.
- A send to a Claude session whose cache is cold, or can't be read, is posted and held
  for that session's own next turn: exit 0, a `held:` line per held recipient, and a last
  line that says when each reads it. It used to be refused with exit 6 and nothing
  posted. Warm co-recipients get the message now. An `agent-N` name holds mail only while
  bound, and nothing held for one survives its binding or a daemon restart. A Codex,
  OpenCode or driven agent that doesn't pass still refuses the send, exit 6, with
  "Nothing was sent." on the first and last lines. `--cold` is now accepted and ignored,
  with a NOTE.
- Every new `moxi comms send` states what it wants back with `--posture none|once|open`, and
  is refused (exit 2, nothing posted) without it; the operator's lane and the voice
  companion's `--operator-words` relays need none. `none` now writes `[no reply]`, and a
  reply to a verified `[no reply]` is refused. A reply needs no `--posture`: it takes its
  posture from what it answers, so a reply to `[reply-once]` goes out `[no reply]` unless
  it asks back with `--posture once`. `--speaker` is its own mark again, one agent
  addressing several, and goes only with open.
- `moxi comms send --posture open` writes `[open]` and opens a thread with limits: it closes
  after `[threads] reply_cap` replies (default 8; `--cap N` or `--no-cap` from the opener),
  after an hour in which the replier had nothing new to read, or on a `--posture none`
  reply. Replies show `[1 reply left]` and `[last reply]`. A `[reply-once]` ask inside a
  closed thread can still be answered. Threads without `[open]` keep no limits. The
  daemon answers a new `readable` query and reports `posture_marks` in its `resolve`
  answer; the verb refuses the new marks against a daemon that predates them.
- Every `moxi comms send --via phone` goes `[reply-once]`, whatever posture, speaker mark or
  cap it passed.
- The Stop-hook reminder fires on any verified `[reply-once]` ask, whoever sent it, and
  on the operator's mail as before; a director's open mail no longer owes.
- The start text names `--posture`, and the director sentence goes only to sessions that
  declared a director.
- `moxi comms send --check` runs every check the send makes before posting and then stops: it posts,
  nudges and records nothing. It prints `would send <N> message(s) to <names>` and the
  `held:` lines the post would print, exit 0, or the send's own refusal with its exit
  code.
- One message holds at most 16,000 characters, routing line included. A longer send is
  refused with nothing posted; comms does not split a message. The daemon posts no
  notices of its own: cache-clock warnings go to its log, and `moxi comms explain` is where a
  stalled delivery is looked up.
- A daemon restart no longer hands a Codex or OpenCode session mail it already has: the
  adapter skips an id it holds and lists its held ids at register, as `moxi-comms-claude`
  does, so a digest is never put into a running turn twice. A Claude session's automatic
  compaction no longer lets queued mail in during the running turn.
- A Claude session's mail that went into a turn you interrupted with Esc is handed
  again, as on Codex and OpenCode. The shim reads the interrupt from the session's
  transcript, so a prompt you type while a turn runs normally hands nothing again. The
  agent may see interrupted mail twice.
- `--via phone`, `--operator-words`, `--speaker` and `--cold` are gone from `send` and
  `reply`; argparse refuses them, exit 2. Nothing writes `[via-phone]`,
  `[operator-words]` or `[speaker]` any more; an older message carrying one still parses,
  and the mark is ignored. `[policy] voice_companion` still loads and is ignored. Only
  the operator is exempt from the posture refusals, and a debt to the operator is paid
  only by a message to `operator`. A Claude session is no longer told about a
  claims-protocol version mismatch, in its log or in a digest.
- Two profiles, `standalone` and `fleet`. A new install is `standalone`: the `local`
  backend by default, and a daemon that provisions itself on its first start.
- `moxi comms service install`, a systemd user unit for the daemon.
- Linux only. Python 3.11 or later.
