# Configuration

Every setting, environment variable and file location Moxi comms reads, in one place.
The commands run as `moxi comms <verb>`; the internal entry points are
`moxi-comms-daemon`, `moxi-comms-claude` and the like; the Python package is
`moxi_comms`.

Most installs set nothing at all. With no config file and no variables, comms runs on
the `local` backend, and the daemon provisions itself on its first start.
`moxi comms doctor` prints what this box resolved and where each answer came from, and
`moxi config comms` prints every key with its value and source.

## Where files live

comms keeps its files in its own five directories under Moxi's roots:

| directory | default | holds |
|---|---|---|
| DATA/comms | `~/.local/share/moxi/comms/` | `directory.db`, `bindings.db`, `cards/` |
| STATE/comms | `~/.local/state/moxi/<device>/comms/` | `record.db`, `cursors/`, `tokens/`, `slot-keeps.json`, `cold-gate.json`, `codex/`, `opencode/`, `logs/` |
| CONFIG/comms | `~/.config/moxi/comms/` | `config.toml` |
| CACHE/comms | `~/.cache/moxi/<device>/comms/` | nothing yet |
| RUNTIME/comms | `$XDG_RUNTIME_DIR/moxi/comms/` | `moxi-comms-daemon.sock`, session directories, `opencode/` launch files |

`<device>` is the host name unless `MOXI_COMMS_DEVICE` says otherwise. With no
`XDG_RUNTIME_DIR` and no `MOXI_COMMS_RUNTIME_DIR`, the runtime root is under the state
root, `runtime/`.

| variable | what it does |
|---|---|
| `MOXI_COMMS_HOME` | puts all five roots under one directory: `data`, `state/<device>`, `cache/<device>`, `config` and `runtime` in it |
| `MOXI_COMMS_DATA_HOME` | the data root |
| `MOXI_COMMS_STATE_HOME` | the state root |
| `MOXI_COMMS_CACHE_HOME` | the cache root |
| `MOXI_COMMS_CONFIG_HOME` | the config root |
| `MOXI_COMMS_RUNTIME_DIR` | the runtime root |
| `MOXI_COMMS_DEVICE` | the device name in the state and cache paths |
| `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR` | the usual XDG bases, used when the variables above are unset |

A per-root variable wins over `MOXI_COMMS_HOME`, which wins over XDG.

comms uses a claim or daemon socket only when it lies inside RUNTIME/comms. The daemon's
socket is always `RUNTIME/comms/moxi-comms-daemon.sock`, and no variable moves it. A
`MOXI_COMMS_CLAIM_SOCKET` that names a socket anywhere else is refused, naming the
variable and the directory.

## The config file

One file, `CONFIG/comms/config.toml`. Every key is optional, and so is the whole file.
`moxi config comms init` writes it with every key commented out at its default, and
`moxi config comms check` exits non-zero when it does not load. A malformed file, an
unknown key, a retired key or a value of the wrong type is refused with the file, the
key and the fix. The daemon refuses to start on such a file. Once it runs, a file that
stops loading leaves it on its last good settings: it logs the error once for each
change of the file, and picks the file up again when it loads.

It holds non-secret values only: comms refuses a file with a key that looks like a
credential (`token`, `secret_value`, `password`, `webhook_url`). Credentials live in the
token store.

```toml
backend = "local"                     # local or mock

[local]
record      = "/path/to/record.db"    # empty: STATE/comms/record.db
retain_days = 90                      # days of message bodies kept; 0 keeps them all

[threads]
reply_cap = 8                         # replies an open thread takes before it closes

[secrets]
chain = ["env", "store"]              # resolver order
store = "/path/to/tokens"             # empty: STATE/comms/tokens
roles = ["calendar"]                  # role holders this box adds

[policy]
cache_callers = ["operator", "planner@proj"]
labels = { "notifier@proj" = "the notifier, which only sends" }

[provision]
slots    = 6                          # slots a first start makes
operator = "operator"                 # the human's identity name

[replay]
messages = 50                         # a reader's catch-up bound with no cursor
hours    = 24
```

| key | default | override | what it does |
|---|---|---|---|
| `backend` | `local` | `MOXI_COMMS_GATEWAY` | which record backend every comms process on this box uses: `local` (a SQLite record) or `mock` (a JSON test double) |
| `[local] record` | `STATE/comms/record.db` | `MOXI_COMMS_LOCAL_RECORD` | where the local record lives |
| `[local] retain_days` | `90` | `MOXI_COMMS_LOCAL_RETAIN_DAYS` | the daemon trims older message bodies at start and once a day; `0` turns trimming off |
| `[threads] reply_cap` | `8` | | how many replies a thread opened with `moxi comms send --posture open` takes before it closes, unless its opener set its own with `--cap N` or `--no-cap`. A whole number, 1 or more. The send verb reads it at each reply. |
| `[secrets] chain` | `["env", "store"]` | | the order the resolver chain tries. The steps are `env` and `store`; anything else, or an empty list, is refused. |
| `[secrets] store` | `STATE/comms/tokens` | `MOXI_COMMS_TOKEN_STORE` | the token store's directory |
| `[secrets] roles` | `[]` | | role holders this box adds to comms' own four (`reader`, `daemon`, `provisioner`, `operator`). Each keys the token store under its own name; a role not listed is unknown. |
| `[policy] cache_callers` | `["operator"]` | | the identities `moxi comms cache` answers, by qualified name. The list is whole: a box that adds a caller lists `operator` too. |
| `[policy] labels` | `{}` | | send-only identities by qualified name, each mapped to the words the send and reply verbs use when they refuse it as a recipient. Without a label it is "a send-only identity". |
| `[provision] slots` | `6` | | how many slots the daemon's first start provisions in an empty directory |
| `[provision] operator` | `operator` | | the human's identity name, provisioned at first start |
| `[replay] messages`, `hours` | `50`, `24` | | a reader with no cursor catches up on at most this many messages, none older than this many hours. The daemon reads them at use, so a change needs no restart. |
| `[cache] required` | `false` | | makes the daemon refuse to start when the cache module is absent or broken (`moxi install comms --with cache` installs it), so the cold-send hold cannot switch off without a word. With this off, an absent or broken cache makes every agent read as having no cache and get its mail live. Read at the daemon's start. |
| `block` | `false` | | stops all routing: while it is true the daemon neither delivers to nor routes from any identity, and `moxi comms send` says so and sends nothing. Read at every use, so a change needs no restart. |
| `[codex] defaults`, `[codex] agents` | `{}` | | the Codex roster (experimental, below) |
| `[opencode] defaults`, `[opencode] agents` | `{}` | | the OpenCode roster (experimental, below) |

Retired keys, still declared so a file that sets one is told what to do: `[claude]
channel` (comms passes Claude Code no channel), `[policy] voice_companion`,
`[policy] never_held` (no sender is exempt from the cold-cache hold) and
`[policy] next_turn_refused` (`--held` is accepted for every recipient). Remove them.

### Choosing the backend

The backend is the first of: a command's `--gateway` flag, `MOXI_COMMS_GATEWAY`,
`backend` in the config file, then `local`. `mock` is a test fake; nothing picks it
unless you name it, and it needs `MOXI_COMMS_MOCK_GATEWAY`.

`moxi-comms-daemon` refuses to start when its `--gateway` flag names a different backend
from the one the environment and config resolve, because every other comms process
resolves without a flag. The exception is `--gateway mock --gateway-store <file>`.

A daemon on `local` whose directory isn't provisioned provisions it before it listens:
`[provision] slots` slots, no standing identities, and the operator identity
`[provision] operator`, which is what `moxi-comms-provision` makes with no flags. A
directory that already holds slots is left alone. A token store that has lost tokens
the record expects is a refusal, not a first start (see [security.md](security.md)).

### The Codex and OpenCode rosters (experimental)

The Codex and OpenCode adapters each read a roster of the identities they run, from the
config file's `[codex]` and `[opencode]` tables. Each pins every identity to a slot:

```toml
[codex.defaults]
sandbox = "workspace-write"

[codex.agents."planner@proj-a"]
slot = 3
cwd  = "/home/me/proj-a"

[opencode.defaults]
model = "opencode/big-pickle"

[opencode.agents."reviewer@proj-a"]
slot = 4
cwd  = "/home/me/proj-a"
```

`[codex.defaults]` holds only `sandbox`, and `[opencode.defaults]` only `model`; a row
that names its own takes none from the defaults. Every other key, `directors` included,
is set on each row. The key lists are in `adapters/codex_roster.py` and
`adapters/opencode_roster.py`.

## Other variables

| variable | | what it does |
|---|---|---|
| `MOXI_COMMS_OPERATOR` | public | set in your own terminal to send as the operator, the human. Its value is the operator's user id, which `moxi-comms-provision` and the daemon's first-start log line print. The lane also needs the `operator` token in the store. A shell that also carries `MOXI_COMMS_NAME` or `MOXI_COMMS_SLOT`, or that runs inside an agent's session, is refused. |
| `MOXI_COMMS_SECRET_<KEY>` | public | the `env` step of the secrets chain. `<KEY>` is the secret id upper-cased, with every run of non-alphanumerics folded to one underscore: `comms/slot-1/names` reads `MOXI_COMMS_SECRET_COMMS_SLOT_1_NAMES`. |
| `MOXI_COMMS_MOCK_GATEWAY` | test | the JSON store of the `mock` backend |
| `CLAUDE_CONFIG_DIR` | Claude Code's | where a reader finds Claude Code's transcripts (`projects/` in it); default `~/.claude` |

## The identity stamp

The launchers stamp these into the process they start. A send's identity comes from
the session's claim socket, `MOXI_COMMS_CLAIM_SOCKET`, and from nothing else: only a live
session holding a name sends under it. `MOXI_COMMS_NAME` and the variables beside it are
read by `whois`, `explain`, `cache` and comms' listener when there is no claim socket,
and by a refused send to name the identity the stamp claims; a stamp alone sends
nothing. You don't set these by hand, except to drive a test identity from a shell.

| variable | what it does |
|---|---|
| `MOXI_COMMS_CLAIM_SOCKET` | the session's claim socket, served by its adapter (the Claude shim, `moxi-comms-codex`, `moxi-comms-opencode`) or by a module's own process that holds a binding. `moxi comms send`, `moxi comms reply` and `moxi comms whoami` read the identity there, and only from a process inside that session |
| `MOXI_COMMS_NAME` | the identity's name |
| `MOXI_COMMS_SCOPE` | its scope (a project name), or `global` |
| `MOXI_COMMS_GLOBAL` | `1` for a global identity |
| `MOXI_COMMS_SLOT` | the slot the identity is bound to |
| `MOXI_COMMS_DIRECTORS` | the identity's declared directors, qualified, comma-separated |
| `MOXI_COMMS_PROJECT` | the project key the launcher computed; also an override of it |

A Claude session's adapter states the session id the launch named to `claude` from the
moment it binds, and takes the id Moxi turns reports from its first look at the session's
endpoint on; it never takes an inherited `CLAUDE_CODE_SESSION_ID`. A launch that resumes a
session without naming its id states none until that look.

## Moxi turns' settings comms depends on

comms' listener gives a Claude session its start context and roster changes through Moxi
turns, which waits at most its `listen.wait_ms` (2000 by default) for every listener of an
event and drops a late answer. The listener's own query to comms' daemon waits at most
1.5 s, so keep `listen.wait_ms` above 1500. Set lower, a slow or stopped daemon costs a
session its whole start context, where it would have had the context block and a line
saying comms was offline.
