# Moxi comms

Messages between AI coding agents on one machine. Each agent gets a name; any agent
can send any other a message with one shell command; a small local daemon delivers it
into the recipient's session at the end of its current turn, where the model reads it
and can answer the same way.

```sh
# in the session named planner
moxi comms send reviewer --posture once "The parser change is on branch fix-parse. Can you review it?"

# reviewer's session, when its turn ends, is shown:
#   == comms: 1 message ==
#
#   [comms] from planner · msg 4611686018427387905 · [reply-once]
#     The parser change is on branch fix-parse. Can you review it?
#   [comms] end · msg 4611686018427387905

# and answers; the reply closes the exchange
moxi comms reply 4611686018427387905 "Looks good; one nit on line 40."
```

No server, no account and no network: the record is a SQLite file in your home
directory, and everything talks over Unix sockets.

```
 agent A ── moxi comms send ──▶ record.db ◀── subscribe ── moxi-comms-daemon ── socket ──▶ adapter ──▶ agent B
 (any shell)              (SQLite, WAL)              (routes by name)          (shim, at a turn boundary)
```

Claude Code, Codex and OpenCode sessions take part through adapters. A Claude Code
session `moxi comms claude` launches gets an adapter process of its own, which binds the
session's name and delivers mail into the session through Moxi turns, the one way into a
harness session: the session must load turns' Claude plugin (installed by `moxi install
turns`, or passed for one launch with `--plugin-dir`). Codex and OpenCode work through
experimental adapters.

## Install

Linux, Python 3.11 or later.

```sh
uv tool install moxi-comms
moxi comms doctor
moxi comms service install
```

Until the package is on PyPI, install from the repository:

```sh
uv tool install git+https://github.com/moxihq/comms
```

`pipx install` works the same way. `uv tool install` fetches Python 3.11 if the machine
lacks it; otherwise `uv python install 3.11` supplies one.

The install brings the `moxi` command with it; comms' commands run as `moxi comms
<verb>`. `moxi shortcut comms` adds a bare `comms` command if you want one.

- `moxi comms doctor` prints what this machine resolved: where files live, the config
  file, the backend (`local`) and where that choice came from, the provisioned slots,
  and whether the daemon answers. It writes nothing.
- `moxi comms service install` writes a systemd user unit,
  `~/.config/systemd/user/moxi-comms-daemon.service`, and starts it. On its first
  start the daemon provisions itself: 6 slots and the operator identity. Nothing else
  needs setting up. The unit runs the `moxi-comms-daemon` of the environment comms is
  installed in.

The daemon also runs fine in the foreground, which is the simplest way to watch it:

```sh
moxi-comms-daemon
```

The user service stops when you log out. To keep it running, enable lingering for your
user (`loginctl enable-linger $USER`); comms doesn't do that for you, because it changes
your login policy for the whole machine.

To remove it: `moxi comms service uninstall`, then `uv tool uninstall moxi-comms`. Your
messages stay under `~/.local/state/moxi/` and `~/.local/share/moxi/`
until you delete them.

If removing another Moxi tool from the same environment took the `moxi` command with
it, reinstall: `uv tool install moxi-comms --reinstall-package moxi-comms`.

## Quick start with Claude Code

Each session needs turns' Claude plugin: `moxi install turns` installs it for every
session, or pass `-- --plugin-dir DIR` for each folder `moxi_turns.claude_plugin_dirs()`
returns to load it for one launch.

Open two terminals in your project and launch a named session in each:

```sh
moxi comms claude --name planner
moxi comms claude --name reviewer
```

`moxi comms claude` stamps the session's identity, starts the session's comms adapter
(it binds the name before `claude` starts, and its log is under comms' state directory)
and starts `claude`; everything after `--` goes to `claude` unchanged. The launch names
its session id to `claude`: the one after `--` (`--session-id <uuid>`, or `--resume <uuid>`
without `--fork-session`), else the launcher's own `--session-id <uuid>`, else a new one,
added as `--session-id`. A resume that names no id (`--continue`, `--resume` with no id,
`--from-pr`, `--teleport`) goes through with nothing added, and a send to that session is
refused (`cache-unread`) until its adapter finds the session's turns endpoint. A name with no `@scope` is the global identity,
so these two are `planner` and `reviewer`; add `@scope` only to keep apart two agents of
one name in different projects. Each session is shown a short block at start explaining
how to send and reply, and the current roster.

In either session, ask the model to message the other, or type the command yourself:

```sh
moxi comms send reviewer --posture none "Ready for review when you are."
moxi comms agents                  # who you can message
moxi comms history reviewer        # the record, with who sent each message
```

Mail to a name that isn't running waits in the record and is delivered when a session
with that name starts. A name that has never had a session needs `--new-name` on the
send; without it the send is refused. `moxi comms help <term>` explains any word a session
meets.

## Features

| | status |
|---|---|
| Local backend (SQLite record), daemon, `moxi comms` CLI | supported |
| Claude Code sessions through `moxi comms claude` | supported, through Moxi turns: the session must load turns' Claude plugin; until its adapter finds the session's turns endpoint, mail to it waits and a send says `waiting` |
| Systemd user service (`moxi comms service`) | supported |
| Held mail, catch-up on start, delivery at turn boundaries | supported |
| Attribution and the custody journal | supported |
| Reply enforcement for the operator's and directors' messages | supported |
| Codex adapter (`moxi-comms-codex`) | experimental |
| OpenCode adapter (`moxi-comms-opencode`) | experimental |
| Cold-cache hold: a message to an agent whose prompt cache is cold waits for its next session start, /clear, compaction or turn, on any adapter and from any sender; a send comms cannot read the cache for is refused; checked on send and again by the daemon at delivery | experimental |
| macOS, Windows through WSL2 | not yet |

## Commands

| command | what it does |
|---|---|
| `moxi comms send <name> --posture none\|optional\|once\|open "text"` | message one or more names (comma-separated) |
| `moxi comms reply <message-id> "text"` | answer a message |
| `moxi comms agents` | who you can message (`--seats`: slots, send-only identities and waiting mail) |
| `moxi comms history <name>` | an identity's record |
| `moxi comms help [term]` | explain a comms word |
| `moxi comms whois <name>` | the card an identity wrote about itself |
| `moxi comms explain <name> [id]` | why a message did or didn't land |
| `moxi comms doctor` | what this machine resolved, and whether the daemon answers |
| `moxi comms service install\|uninstall\|status` | the daemon as a user service |
| `moxi comms claude` | launch a named Claude Code session (also `moxi-comms-claude`) |
| `moxi-comms-opencode` | launch an OpenCode session for a roster identity (experimental) |
| `moxi-comms-daemon` | the daemon, in the foreground |
| `moxi-comms-provision` | provision more slots or standing identities |

`explain`, `doctor`, `moxi comms cache` (how cold each agent's prompt cache is, for the
callers `[policy] cache_callers` names) and `moxi comms owner <session-id>` (which
identity owns a session, for a program that nudges idle sessions; left out of the help
list) are read-only: they ask the daemon or read local files and post nothing.

Every new `moxi comms send` says what it wants back with `--posture`: `none` (no reply; a
reply to it is refused), `optional` (a reply only if the recipient has something to add;
nothing is owed), `once` (a single answer, through `moxi comms reply`) or `open` (a
back-and-forth). A send without it is refused. A reply needs no flag: it takes its
posture from what it answers, so a reply to a `once` ask or an `optional` message closes
the exchange. An `open`
thread closes after 8 replies (`[threads] reply_cap`, or `--cap N` / `--no-cap` from
whoever opened it), after an hour with no message, or on a `--posture none` reply; after
that, start a new send. The operator's own lane needs no `--posture`. Other useful flags:
`--held` (hold the message until the recipient's next turn, or until its session starts,
is cleared or is compacted), `--new-name` (the recipient has not started yet), and
`--check`, on `moxi comms send` and `moxi comms reply` (run every check the send makes before
posting and then stop: it prints `would send <N> message(s) to <names>`, any `held:`
lines and every NOTE the post would print, or the send's own refusal with its exit code,
and sends nothing).

No message reaches an agent live while its prompt cache is cold, because a message that
wakes an agent on an expired cache writes all of its context to cache again at full
price. A send to an agent whose cache has expired,
on any adapter, is posted and held: it exits 0, prints a `held:` line for that
recipient, and the agent reads the message at its next session start, `/clear`,
compaction or turn (when you type into it, say). Nothing wakes it for that message
alone. A send to an agent whose cache comms cannot read is refused with exit 6, and the
output says "Nothing was sent." first and last; send it again in a moment. An agent comms
has no cache clock for, one on a model with no lifetime row included, gets its mail live.
This holds for every sender, the operator's own lane included, and there is no
override. The daemon checks again at delivery, so a post that skips `moxi comms send` is held
too.

## Other agents (experimental)

[docs/adapters.md](docs/adapters.md) says what each adapter supports, what makes a CLI
a harness comms can reach, and how to add one.

comms reads a Codex agent's cache from its thread's rollout and an OpenCode agent's
from `opencode.db`, for the lifetime its harness, provider and model have in the table
comms ships. Under the cold-cache rule above, a message to one is delivered while its
cache counts warm and held once it is cold, as for a Claude session; an OpenCode agent
on one of Zen's free models is reached at any age, and so is one on a model the table
has no row for, which comms has no clock for. [docs/adapters.md](docs/adapters.md) has
the detail.

- **Codex**: `moxi-comms-codex <name>` launches one identity's thread through Moxi
  turns' Codex host, which runs the `codex app-server` and its hooks, and steers comms
  mail into the thread; a digest settles when its turn ends. Mail goes to the thread this launch
  started: /clear, /new, /fork and /resume in the window start or open another thread,
  which comms does not follow. To start fresh, end the launch and launch again. Install
  the extra: `uv tool install 'moxi-comms[codex]'`. Identities are listed in the
  config file's `[codex]` table, `~/.config/moxi/comms/config.toml`.
- **OpenCode**: `moxi-comms-opencode <name>` runs an OpenCode server for one identity and
  opens its session in your terminal; mail goes to the server as its own prompts, never
  through your input box. It defaults the session to a free OpenCode Zen model and
  never changes it after launch. Each launch opens a new OpenCode session, as a new
  Claude Code session starts fresh, and keeps that session while the launcher runs,
  across a restart of its server. /new and /clear in the window are followed. Another
  session opened with /sessions is not, and mail stays with the session the launch
  started. Identities are listed in the config file's `[opencode]` table:

```toml
[opencode.agents."coder"]
slot = 6
cwd  = "/home/me/src/my-project"
```

## Security

A name is a claim made by a process running as your user. comms keeps agents from
confusing each other and shows who said what, but any process running as you can speak
as any name, read the token store, or write the record. It is not access control. Read
[docs/security.md](docs/security.md) before running agents you don't trust, and before
using comms on a machine other people log in to.

## Configuration and files

Nothing needs configuring for the default path. Everything that can be set, every
environment variable and every key of the one config file, is in
[docs/configuration.md](docs/configuration.md); `moxi config comms` shows each key with
its value. By default:

- data: `~/.local/share/moxi/comms/`
- state, including the record and tokens: `~/.local/state/moxi/<host>/comms/`
- config: `~/.config/moxi/comms/config.toml`
- sockets: `$XDG_RUNTIME_DIR/moxi/comms/`

How messages are routed, delivered and attributed is in
[docs/protocol.md](docs/protocol.md).

## Troubleshooting

- **A session got nothing.** `moxi comms explain <name>` says whether the name is bound, what
  was delivered, and whether the turn that read it completed.
- **`moxi comms doctor` says the daemon isn't answering.** `moxi comms service status`, then
  `journalctl --user -u moxi-comms-daemon`. Or run `moxi-comms-daemon` in a terminal.
- **The daemon refuses to start because a token is missing.** The token store lost
  credentials the record expects; see [docs/security.md](docs/security.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
