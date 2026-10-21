# Adapters

How comms puts mail in front of a model, written from the code in
`src/moxi_comms/adapters/`. The message, routing and the daemon socket are in
[protocol.md](protocol.md); this page is about the other end of that socket.

## What an adapter is

An adapter is a daemon-socket client that binds one name, puts that name's mail in front
of a model at a turn boundary, and writes that name's cursor. One adapter process serves
one name. It registers with the daemon as one of the daemon's adapter **kinds**:

| kind | launcher | what it serves |
|---|---|---|
| `claude-shim` | `moxi-comms-claude` | a Claude Code session |
| `codex` | `moxi-comms-codex` | a Codex thread, through `codex app-server` |
| `opencode` | `moxi-comms-opencode` | an OpenCode session, through `opencode serve` (see [OpenCode](#opencode)) |
| `send-only` | | a module's own process holding a name that sends and receives nothing (see [Send-only identities](#send-only-identities)) |

A kind decides two things. One is the **wire**: how the adapter talks to its CLI, and so
how mail gets into a turn and how the adapter learns the turn ended. The other is the
**cache clock keyed to the kind's name**: how `moxi comms send` and the daemon read the
recipient's prompt cache, or that the kind has none, and then its mail goes live. Every
kind takes held mail, `--held` included. A new kind brings its own clock, or none.

The four above are comms' own. Another package adds a kind by an entry point in the
group `moxi.comms.kinds`, named by the kind, whose target is a plain table: `{"cache":
"person"}` for a person at the other end (its cache reads `no-cache` and its mail is never
held), or `{"cache": "harness", "harness": "claude", "evidence":
"package.module:function"}` for a session of one of the harnesses comms has a clock for,
with the function that says which session to read. The daemon calls it at each read as
`evidence(qualified, session_id)`; it answers `(session_id, new_session)` or
`"unreadable"`, and comms hands that to the cache module's read, so the kind decides no
verdict itself (`moxi_comms/kinds.py` states the contract). comms declares its own four
the same way. The daemon reads the group when it starts and refuses a register of any other
kind; a declaration that does not hold is left out and logged
(`moxi_comms/kinds.py`). An adapter built outside comms uses `moxi_comms.adapter`: the
daemon client, the backoff, cursors, the digest and frame line, the attribution tiers
and the machine line's parse.

## What a harness is

A harness is a terminal session a person can sit in, which comms reaches as one of its
agents. Four things make it one:

- **A person can sit in it.** It runs in a terminal window, and the person types into it
  as they would into any session of that CLI.
- **Mail arrives as model turns in that same live session**, idle or busy, by a way in
  of its own. Comms never types into the person's input box, so what they type and what
  another agent sends never interrupt each other.
- **The cursor moves only on the CLI's own evidence** that the turn carrying the digest
  completed: an in-band completion event, never a guess from output or timing.
- **The agent sends as its identity** from that session: its shell inherits the
  session's claim socket (`MOXI_COMMS_CLAIM_SOCKET`), and `moxi comms send` and `moxi comms reply` read
  the identity from it. A process outside the session, or one with only the launch
  stamp, sends under no identity.

A CLI that can't take mail into a live session, or can't be sat in, isn't a harness.
Running it once per message with no session anyone sits in is headless automation, not
a harness.

## The rules every adapter keeps

1. **Its connection is its liveness.** An adapter is an ordinary client of the daemon
   socket. When its process dies the connection closes and the daemon unbinds the name.
   The daemon spawns and supervises nothing.
2. **A roster names the agents a launcher runs.** `moxi-comms-codex` and `moxi-comms-opencode`
   read theirs from the `[codex]` and `[opencode]` tables of comms' config file: a
   pinned slot per identity and a closed key set, and a key that looks like a token is
   refused. `--config PATH` names another comms config file, checked whole, whose table
   that one launch reads as its roster instead (a launcher checks a candidate file with
   `--config PATH --dry-run` before it replaces the box's); every other setting still
   comes from the box's file. A register never starts a process.
3. **One environment stamp.** The agent's process gets its stamp from
   `stamped_env`, which scrubs every comms variable and sets them again, so authority
   can't leak in from whoever started the launcher. The adapter is its session's claim
   holder and stamps its claim socket into everything it spawns; that socket, not the
   stamp, is what a send speaks as.
4. **One renderer.** Every adapter renders the digest with `render_digest`, so every
   model reads the same frame lines. A frame line ends with the message's marks, each
   only when present: the thread mark (`[1 reply left]` or `[last reply]`) and the posture (`[no reply]`, `[reply-optional]`, `[reply-once]`, or `[open]` on the
   message that opens a thread with limits). An open message with no posture token shows
   none. Layer one, the block a session is shown at start, is `context.layer_one_block`
   for every harness: the context block, with the director sentence after it only for
   an agent with a declared director (`--director` for Claude, the roster's `directors`
   for Codex and OpenCode). After the block comes one line, the same on every harness,
   naming which comms to run by absolute path: the launching install's own `moxi`
   script, since the agent's shell may find another install's `moxi` first. The Codex
   and OpenCode launchers write it into layer one themselves; the Claude launcher stamps
   it into the session's environment as `MOXI_COMMS_COMMAND`, and comms' listener writes
   the line from it (from its own install for a session an older launcher started).
   With no `moxi` script to name, every harness's line names the bare `moxi comms`.
   Codex's thread record hashes the director sentence and that line with the rest, so a
   thread whose director changes is handed the new layer one.
5. **One flush path.** Nothing is delivered while catch-up is still arriving. A backlog
   coalesces into one digest, sorted by message id; live mail arrives one message at a
   time and takes the same path.
   - **When a batch goes back.** The flush puts a batch back on the queue, ahead of
     anything newer and with its catch-up notice, only when the adapter's delivery
     reports that nothing reached a live turn, so that trying again at once can't put
     the same digest into a turn it already reached. Any other outcome is a delivery:
     the digest is recorded, and whether the cursor moves for it is decided by the
     completion evidence. An error the adapter didn't expect drops the batch, which
     replays when the name next binds.
   - **The wait before a retry.** Every batch that goes back waits before the next try,
     one second by default, so an adapter whose delivery keeps failing doesn't spin.
6. **The adapter is the only cursor writer, and writes only on completion evidence.** A
   digest whose turn can't be identified stays untracked and replays at the next bind.
   Replay is the safe direction; a cursor that moved too far is not.
7. **A recorded session is resumed, never replaced.** Where an adapter resumes a session
   (Codex and OpenCode), the session or thread id lives in STATE, written by
   the adapter. A resume that fails is said out loud and retried; the
   adapter never starts a fresh session under the same name, because that would be an
   agent with no memory answering as the old one.

   *Codex and OpenCode:* a Codex or OpenCode launch starts a new
   thread or session, as a Claude launch starts a new session, and never resumes or
   loads the one an earlier launch recorded. A launch is one `moxi-comms-codex` or
   `moxi-comms-opencode` process, and no code path in it reads an id from the record that it
   did not itself start. The rule above holds inside one launch: when the launch's own
   thread or session fails to resume or is gone, that is said out loud and never
   replaced. Codex retries the resume; OpenCode stops posting for the rest of the
   launch.
8. **Supervision retries.** A lost daemon connection, or a lost CLI server, is retried
   with backoff rather than ending the process, since an adapter that exits leaves its
   name silent. An unexpected error is logged with its traceback, said once per
   exception type, and retried. A daemon refusal of the name at start is fatal.
   `moxi-comms-codex --window` gives up and exits 3 if its thread is never ready to show, and
   so does `moxi-comms-opencode` if its session never is.
9. **The daemon hears about deliveries.** The adapter reports `injected`, with the
   digest's text, and `activity`, so `moxi comms explain` works for every kind.

## The base

`codex` and `opencode` share one class, `HarnessAdapter` in
`adapters/harness.py`. It owns
the daemon binding, the queue and the catch-up hold, the flush path, the daemon side of
supervision and the two notifications. An adapter for a CLI is a subclass, its **port**,
which fills in these hooks:

| hook | what the port says | default |
|---|---|---|
| `KIND` | the kind it registers as | |
| `deliver(text, high, identity, count)` | puts the digest in front of the model; returns `REQUEUE` or a receipt | required |
| `delivered(..., receipt)` | what to do after the base recorded a delivered digest | nothing |
| `retry_delay()` | seconds to wait after a `REQUEUE`, read each time | the base module's `FLUSH_RETRY`, 1.0 |
| `can_inject()` | whether a digest can go in now, such as "the CLI server is connected" | true |
| `start_port()` | brings the CLI side up after the bind | nothing |
| `supervise_port()` | one supervision pass on the CLI side; false if it already waited its own backoff | true |
| `stop_before_unbind()`, `stop_after_unbind()` | what `stop()` closes around closing the daemon connection | nothing |
| `ONE_AT_A_TIME` | hold one delivery in flight per name | false |
| `QUIET_OFFLINE_ON_STOP`, `STATE_NOUN`, `registered_detail()` | wording of the offline line, the bind error and the "registered as" log line | |

The flush path, in order:

1. Under the lock, return if catch-up is still arriving, the queue is empty,
   `can_inject()` is false, or the port holds one delivery at a time and one is running.
   Otherwise take the queue sorted by message id, and the catch-up notice.
2. Render the digest and call `deliver`.
3. On `REQUEUE`, put the batch back ahead of the queue with its notice, wait
   `retry_delay()`, and wake the flush loop.
4. On any other value, record the digest and call `delivered` with the receipt. The base never
   reads a receipt's truth: `None` is a delivery too.

Each port maps its own outcomes to `REQUEUE`:

- **Codex** returns `REQUEUE` when the steer or start raised an app-server or JSON-RPC
  error, so nothing was injected. Its receipt is the turn id. It returns `None` when the
  text went into a turn that was already running and whose id couldn't be found, or when
  it gave up steering; nothing tracks that digest, so it replays at the next bind.
- **OpenCode** returns `REQUEUE` when nothing was admitted: the status read before the
  post doesn't say idle, the post can't connect, or the server answers it with a 5xx, a
  429 or any refusal. Its receipt is the message id comms chose, also when a post got no
  answer at all, since the server may have admitted it; the session's message list then
  says whether it did.

## Adding a harness

A harness is a port on the base, in a kind of its own, plus the cache clock for that
kind's name, if it has one. Every kind's cache is checked: the daemon delivers nothing
live to a binding whose cache reads cold, and holds that mail for its next session
start, /clear, compaction or turn. A kind comms has no cache clock for reads
`no-cache`, and its mail goes live. Before writing
one, answer these for your CLI:

1. **Does the CLI's shell inherit the environment it was launched with, and can it reach
   a Unix socket from inside its sandbox?** If not, the agent can't speak as its name or
   send, and there is no adapter.
2. **Can something other than the person's keyboard put text into the running,
   interactive session as a model turn, an idle session included, and does the CLI then
   say, in its own events, that the turn completed?** Typing into the terminal doesn't
   count. If not, it isn't a harness.
3. **Can a person sit in the session that receives the mail?** If not, it isn't a
   harness.
4. **Does it have a place for comms' instructions at session start?** If not, they go in
   a first turn, as Codex's do.
5. **How does it load a recorded session, and does that carry the same settings the
   session started with?**

Then write down which of the port's failures return `REQUEUE`, against the rule in
"When a batch goes back", give `retry_delay()` a positive answer, and, if the CLI runs a
server, make `can_inject()` false while it is down so the flush doesn't fail and wait in
a loop while supervision brings it back.

## What each harness supports

| harness | kind | how a person sits in the session | what ends a turn | status |
|---|---|---|---|---|
| Claude Code | `claude-shim` | `moxi comms claude --name <name>`: the session is the terminal it runs in; the launcher forks the session's adapter process | the turn's end as Moxi turns reports it: a digest is handled when its turn ends with an answer, and handed again when it was interrupted (Esc), ended in an error or refusal, refused, or lost | supported through Moxi turns; the session must load turns' Claude plugin |
| Codex | `codex` | `moxi-comms-codex <identity> --window` opens the Codex window on a new thread for each launch | the turn's end as Moxi turns' Codex host reports it: handled on an answer, handed again otherwise | experimental; through Moxi turns |
| OpenCode | `opencode` | `moxi-comms-opencode <identity>` opens `opencode attach` on a new session for each launch | the first idle after the model saw the digest, when the turn ended with `finish=stop` | experimental |

comms reads a cache clock for both. A Codex thread's clock is the end of its last
request in its rollout, under the sessions root its launcher reports; an OpenCode
session's is the start of its last request in `opencode.db`, at the path the launcher
asks OpenCode for. Each counts the lifetime its harness, provider and model have in the
cache module's lifetime table (comms reads both clocks through the cache module; without
it every agent's mail goes live); a model or provider with no
row has no clock to count by, and its mail goes live. A message to a cold one is posted
and held, as for a cold Claude session. A Codex agent is reached while its cache counts
warm: 30 minutes after its last request on `gpt-5.6-terra` and `gpt-5.6-luna`, and on
the models a longer probe measured, the measured number, lowered by the daemon when it
sees a cache go cold sooner. An OpenCode agent whose next request runs on one of Zen's
free models is reached at any age. A model change reads cold until the next request on
the new model, so mail to it is held. A send to anything comms cannot read is refused
(exit 6, nothing posted, "Nothing was sent." first and last): a launcher built before
these reports, a reverted or forked Codex thread, an OpenCode session with a revert
pending, and a rollout or row comms does not recognise.

## Codex

**Through turns.** `moxi-comms-codex` writes no hook and runs no app-server. It calls
Moxi turns' `turns.codex.launch` with the roster row's thread settings (sandbox or
permission profile, writable roots, network access, model, multi-agent off) and a check
of what each thread load reports. turns runs the app-server, wires Codex's hooks with
their per-launch trust, lays the settings on the start and on every resume, and refuses
to deliver into a load whose settings did not take. `moxi comms doctor` shows why, in
turns' rows.

**One thread.** The launcher delivers into the thread this launch started. /clear, /new
and /fork in the window start another thread, and /resume opens one; comms does not
follow any of them, and mail still goes to the launch's thread. To start fresh, end the
launch and launch again.

## OpenCode

`moxi-comms-opencode <identity>` runs an [OpenCode](https://opencode.ai) session you sit in,
in the terminal you start it from. It is experimental. It was built and tested on
OpenCode 1.18.34, with OpenCode Zen's free models (no account and no key) and with a
local model through Ollama.

```toml
# CONFIG/comms/config.toml
[opencode.agents."coder"]
slot      = 6
cwd       = "/home/me/src/my-project"    # best at a git root
directors = ["main"]
# model   = "opencode/minimax-m3.1-flash-free"  # the launch default; a list adds
#                                               # fallbacks; none: the shipped list
# binary  = "/home/me/.opencode/bin/opencode"
```

**What runs.** The launcher registers the identity, then starts one
`opencode serve --hostname 127.0.0.1 --port 0` for it, with the identity and the launcher's claim socket stamped into
the server's environment. OpenCode runs a session's tools in the server's own process,
so that is what the agent's shell has, and `moxi comms send` from there sends as the
identity through the claim socket. One server per identity, never shared. The server gets a password made for
this launch (`OPENCODE_SERVER_PASSWORD`), which the launcher holds; the password keeps
other users and web pages out, not other processes of your own user, which can read the
server's environment. The launcher creates a new session for each launch and keeps it
while it runs; if that session is gone during the launch, it says so and posts nothing
more. It opens `opencode attach <url> -s <session>` in your terminal. Closing the window
stops the launcher and takes the identity offline. If the server dies, the window closes and reopens on the server the
launcher starts in its place.

**How mail arrives.** The launcher posts each digest to the server
(`POST /session/:id/prompt_async`) under a message id it chooses. The window is a client
of the same server and shows the turn as it runs. Nothing is typed into the window, so
what you have half-typed in its input box stays as it is. The launcher posts only into
an idle session: it reads the session's status immediately before each post, and after
its own post it waits for that turn to end before the next. Mail that arrives meanwhile
goes as the next digest.

**What it can't prevent.** OpenCode's session has one loop. A prompt that arrives while
a turn runs is taken at that turn's next step, and the running turn's own reply is never
written.

- If you press Enter in the few milliseconds between the launcher's status read and the
  server admitting its post, the digest lands in your turn, and the reply you asked for
  may not be written. On loopback that window is a few milliseconds, plus the few
  milliseconds between your Enter and the server marking the session busy. This is the
  harness's remaining risk.
- If you submit a prompt while a digest's turn runs, your prompt takes that turn over:
  the model answers you, and the digest's own reply may not be written. The digest still
  counts as delivered, since the model saw it.

**When the cursor moves.** On the first idle after an assistant message answering the
digest appears, when the turn ended with `finish=stop`. A turn that ends by an abort or
an error moves nothing, and the digest comes again at the next launch.

**Failures.** A post that can't connect, a busy session, a 5xx or a 429 is tried again
after a second, and a run of five or more is said once in the status line. Any other
refusal (a 400, a wrong password, a session that is gone) stops mail into the session for
the rest of the launch and is said once; the undelivered messages come again at the next
launch. An event stream that closes, or sends nothing for 30 seconds (OpenCode sends a
heartbeat every 10), is reopened, and the session's messages are read again before any
more mail goes in.

**Your configuration.** The session loads your own OpenCode setup, as it does when you
run OpenCode yourself: your OpenCode config and sign-in, your `~/.claude/CLAUDE.md`, and
the skills under `~/.claude/skills` and `~/.agents/skills`. With no git root above the
roster's `cwd`, OpenCode treats a `.opencode/` directory in a parent as project config
and writes into it. `scripts/opencode-home-check.py` shows what a session would load
from your HOME, without running a model.

**The model.** The launch sets OpenCode's `model` and `small_model` to the first model of
the roster's `model` that the server offers free. A row that names no model gets comms'
shipped list, in order: `opencode/minimax-m3.1-flash-free`, `opencode/glm-5.3-flash-free`
and `opencode/deepseek-v4.1-flash-free`, the free OpenCode Zen models that keep no data
and train on none by [Zen's terms](https://opencode.ai/docs/zen/) as read on 2026-10-20.
MiniMax M3.1 Flash Free is free there for a limited time; when it is retired, a launch
takes the next. If the server offers none of the row's models, the launch uses the first
free Zen model offered. A launch on any model but the row's first says which before the
window opens, in the launcher's log and in the identity's status line, and says whether
it is one of the shipped zero-retention defaults or a model that may keep or train on
what the session sends it. If no free model is offered, it sets none, and OpenCode uses
its own default, which may be a paid model if your own credentials allow one. comms never
names a paid model and adds no key, credential or provider of its own, and after the
launch it never changes the model: its posts name none, so a post runs on whatever model
the session uses. A model you pick in the window is yours, and the session keeps it, also
in later launches, since a session runs on the model it last used; it may keep or train
on what the session sends it, your `CLAUDE.md` and skill descriptions included. The
launch default applies to new sessions and to OpenCode's side requests, such as a new
session's title. A request that runs on any model but the launch default is written to
the launcher's log and its status line, and nothing more is done.

**The model list.** OpenCode keeps the list of models it offers in
`~/.cache/opencode/models.json` (under `$XDG_CACHE_HOME` when that is set) and refreshes
it in the background, so a server that has just started can still read a list from
before a model was retired, and choose it. Before it starts the server, the launcher
therefore runs `opencode models --refresh` under the server's environment, which fetches
the list and writes it at once (about 1.5 s and a 6 MB download), and chooses the model
from what the new server then offers. It gives the refresh 20 seconds. A refresh that
fails or takes longer leaves the list as it stands, and the launch says so before the
window opens, with how long ago the list was written. The file it refreshes is your
own OpenCode model cache, which OpenCode rewrites itself every hour. `--dry-run` runs no
refresh and says that a launch will. Two gaps remain: OpenCode's published list can lag
Zen, so a model Zen has retired and the list does not yet mark is still chosen and
fails at its first request; and a server the launcher did not start keeps the list it
read when it started.

**The status line.** While the window covers the terminal, the launcher's status lines
go to its log under `STATE/comms/opencode/` and to the identity's session file there.
`moxi comms explain <identity>` prints the newest ones, and when the window closes the
launcher prints its model notes and any refusal again.

**Not contained.** OpenCode runs shell commands with no sandbox, as your user. The
launch config allows file tools outside the project (the shell reaches those files
anyway) and denies a tool call that keeps repeating itself (OpenCode's `doom_loop`
ask), so a session nobody is watching never waits on a question. Don't describe an OpenCode agent as contained.

**One session.** The launcher reads and posts into the session this launch created only.
If you open another session in the window (/sessions), mail still goes to the launch's
session, and you see it when you switch back. /new and /clear are followed: the server
hears of the new session when you submit its first prompt, and from then on mail goes
there, starting with any digest still pending in the old one. Until that first prompt
the launcher cannot know, and mail still goes to the old session.

## Send-only identities

A send-only identity is a standing row whose directory `kind` is `send-only`, made with
`moxi-comms-provision send-only NAME@SCOPE`. It posts from its own webhook bundle, and its
holder is a module's own process (a `ClaimServer` with a daemon arm), which binds it
with the `send-only` adapter kind. The daemon accepts that kind only for such a row, and
such a row only with that kind, so no agent session can bind the name and take mail in
its place.

It holds no slot. Its register asks `next-free` and is answered `"slot": null`; one that
asks for a slot by number is refused. It takes none of the provisioned slots, gets no
slot keep when it ends, and no free-slot count sees it. `moxi comms agents --seats` shows it
with `no slot`.

Nothing is delivered to it, bound or not. `moxi comms send` and `moxi comms reply` refuse it as a
recipient before anything is posted; a record line that names it anyway is delivered to
nobody, pending for nobody and held for nobody, and opens no ask for it; the daemon never
sends its binding a `deliver` frame. A bare name typed in another scope is not that
identity: it resolves in the sender's own scope like any unbound name.
