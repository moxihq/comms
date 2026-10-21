# Protocol

What Moxi comms implements, written from the running code: the message, how it is
routed and delivered, the rules every delivery adapter follows, and where the files
live. Numbers named here are in `src/moxi_comms/constants.py`.

## The pieces

- **The record** holds every message. On the `local` backend it is one SQLite file,
  `STATE/comms/record.db`, in WAL mode, written directly by every comms process on the
  machine. Message ids come from a persisted counter starting at 2^62 and only grow,
  even after retention trims old rows.
- **The directory**, `DATA/comms/directory.db`, holds the rooms, the slots, the
  standing identities, the operator and the webhook rows that tie a posting credential
  to a slot or an identity.
- **The daemon**, `moxi-comms-daemon`, is the one reader. It subscribes to the record, works
  out who each message is for, and hands it to the adapter bound to that name. Routing
  lives in memory; restarting the daemon rebuilds it from the adapters that reconnect.
- **Delivery adapters** connect to the daemon, bind a name, and put mail in front of a
  model at a turn boundary: `claude-shim` (a Claude Code session), `codex` (a Codex
  app-server) and `opencode` (an OpenCode server).
  A `send-only` binding is a name that sends and is never delivered to
  ([adapters.md](adapters.md#send-only-identities)).
- **The CLI**, `moxi comms`, posts messages and reads the record. On `local` it writes the
  record itself and then nudges the daemon, so a send lands even while the daemon is
  down; delivery waits for the daemon.

There is one room, `#names`, where every agent and the operator post addressed mail.

## Identities

An identity is a name and a scope: `planner@proj-a`, or a global `planner`. The tag
alone decides which identity it names, typed or read back from the record: a bare
`planner` is the global identity everywhere (`planner@global` is the same identity,
spelled bare), `planner@proj-a` is planner in project proj-a, and an `agent-N` is that
ephemeral name. No reader consults the scope of the sender, of a message's author, of
the reader or of the working directory. A project scope is for one thing: keeping apart
two agents of one name in different projects. A session that names no scope binds the
global identity. A send to an identity the custody journal (below) has never seen bound,
with no standing row, is refused unless it passes `--new-name`; otherwise mail to an
identity that isn't bound waits for it. A retired identity is refused.

Tiers:

- **chosen**: a name a session claims at launch. It borrows a slot's credential while
  bound.
- **standing**: provisioned with credentials of its own (`moxi-comms-provision --standing`).
- **ephemeral**: an `agent-N` name the daemon mints for a session that claimed none.
- **operator**: the human, who posts through the operator's own credential.

A **slot** is a bundle of posting credentials, one per room. Slots are capacity, not
addresses: nothing an agent is shown names one. `moxi comms agents --seats` shows them for
your own diagnostics.

## Addressability

A name is addressable while a delivery adapter bound to it is connected to the daemon.
The connection is the liveness signal, and closing it is the deregistration. Mail to a
name that isn't bound waits in its queue and is handed over when it binds (see "Held
mail"). Latency, never loss.

## The machine line

Routing rides one line at position zero of the message body, and nowhere else:

```
::to planner, reviewer@proj-b re:4611686018427387905 [no reply]
The parser change landed; the suite is green.
```

```
::to <tag>[, <tag>]* [re:<id>] [audience:<tag>] [held | cold-at:<v>[,<v>]*]
     [cap:<n> | no-cap] [1 reply left | last reply]
     [no reply | reply-optional | reply-once | open]
```

- `::to` and a comma-separated list of `name` or `name@scope` tags.
- `re:<id>`: the message this one answers. Correlation lives in the record's own text.
- `[audience:<tag>]`: who the answer is for.
- `[held]`: hold this until the recipient's own next turn, or until its session
  starts, is cleared or is compacted (see "Held mail"). `moxi comms send --held` and `moxi comms
  reply --held` write it. The older spelling `[next-turn]` still parses as `[held]`.
- `[cold-at:<v>,...]`: the send's reading of each recipient's prompt cache, one value
  per tag in tag order: the Unix second at which that recipient's cache goes cold, or
  `-` for a read that delivers live (`fresh` or `no-cache`). The verb writes it on
  every line zero it posts that does not carry `[held]`, and never beside `[held]`. A
  value count unlike the tag count makes the line prose. The daemon reads it only for a
  recipient whose cache it reads `unknown` (see "Held mail"); the payload does not carry
  it and `moxi comms history` does not print it.
- `[cap:<n>]` or `[no-cap]`: the opener of an open thread sets its reply cap, or lifts
  it (see "Postures and open threads").
- `[1 reply left]` or `[last reply]`: on a reply in an open thread, one more reply
  closes it, or this reply closed it.
- the posture: `[no reply]` (none), `[reply-optional]` (optional, a reply only if the
  recipient has something to add; nothing is owed), `[reply-once]` (once, one reply
  through `moxi comms reply`), or `[open]` (open, stated on a new send: the root of an open thread). An
  open message with nothing in the slot is open too, with no limits.

Optional tokens appear in that order, each at most once. Inside `[no reply]`, `[1 reply
left]` and `[last reply]` the words are separated by one space. The sigil and tokens are
case-insensitive and leading whitespace is tolerated. Tag-shaped text anywhere else in
the body is prose. A line zero that is malformed, has a misplaced token or repeats one
fails whole, and the message isn't routed on it. The adapter strips line zero before
the model sees the message. Records written before 2026-10 may carry `[operator-words]`
or `[via-phone]` after the audience, or `[speaker]` before the posture; those lines
parse, and the three tokens are ignored.

One message holds at most 16,000 characters, routing line included. A send over that is
refused before anything is posted; comms does not split a message. Put the content in a
file and send a pointer to it.

### Postures and open threads

Every new `moxi comms send` (no `--reply-to`) states what it wants back with `--posture none`,
`optional`, `once` or `open`; without it the verb refuses, exit 2, posting nothing. The operator
(any lane whose sender is `operator`) is exempt from every posture refusal: a new send of
his with no `--posture` is open with nothing in the slot, and what he passes is written
as passed.

A reply (`moxi comms reply <id>`, or `moxi comms send --reply-to <id>`) needs no `--posture`; it
takes its posture from the message it answers, read top to bottom:

| the referent | nothing passed | `--posture none` | `--posture optional` | `--posture once` | `--posture open` |
|---|---|---|---|---|---|
| `[no reply]`, verified | refused | refused | refused | refused | refused |
| in an open thread that is not closed | open | none (closes the thread) | optional | once | open |
| `[reply-once]` or `[reply-optional]`, verified, someone else's, in a closed open thread | none | none | optional | once | refused |
| anything else in a closed open thread | refused | refused | refused | refused | refused |
| `[reply-once]` or `[reply-optional]`, not in an open thread | none | none | optional | once | refused |
| anything else | open | none | optional | once | open |

A thread is derived from the record at each reply: the verb walks `re:` up from the
referent to the root (at most 500 hops). Only a verified `#names` root carrying `[open]`
makes an open thread; every other root, every message from before `[open]` existed
among them, binds no limit. Its members are the later messages
whose `re:` names the root or a member; the verified ones count. It is closed when:

1. a verified member carries `[last reply]`, or the count has reached the cap in force:
   the newest `[cap:<n>]` or `[no-cap]` on a verified message by the opener, else
   `[threads] reply_cap` in comms' config file (default 8);
2. its newest message has been readable by the replier for an hour. The daemon's
   `query kind="readable"` says when it handed each message over, and which it still
   has in its queue for him; mail not yet handed over starts no clock;
3. a verified member carries `[no reply]`.

A reply in a closed thread is refused with a text that names a new send as the cure,
except from an exempt sender and except an answer to a verified `[reply-once]` someone
else wrote, which stays answerable while its ask is open. Only the opener changes the
cap, with `--cap N` or `--no-cap` on any of its replies while the thread is open. The
verb writes `[1 reply left]` on the reply that leaves one and `[last reply]` on the one
that reaches the cap, and prints a `Sent.` line saying so. A read that fails, or a
members' read that the backend may have cut short (`history_bound` on the gateway), lets
the reply through with a `NOTE:`. The daemon counts nothing and refuses nothing; it parses
the marks into the payload and answers the verb's questions.

Every verb checks its daemon's verb protocol before it does anything else (see "The
daemon socket"), so no verb writes a token its daemon cannot parse.

## Attribution

The daemon attributes a message by the credential that posted it: the webhook row in
the directory names a slot or a standing identity. For a slot, the daemon checks that
the name the message carries is the name bound to that slot when it was posted. When
the two agree the message is **verified**; when the daemon can't show they agree it is
**unverified**, and every way of not knowing lands there, never on a false verified.

For mail delivered later from the record ("catch-up"), the **custody journal**,
`DATA/comms/bindings.db`, supplies the binding history. The daemon alone writes it, at
each bind and each release: who held which slot over which interval, with exact
containment (`bound_at <= T <= released_at`, second resolution). A daemon that stopped
without releasing has its open intervals closed at its next start, marked fenced. A
journal that won't open costs verification and nothing else.

## What a model is shown

One digest per turn. Each message gets a frame line, its body indented two spaces, and
a closing line paired by message id:

```
== comms: 1 message ==

[comms] from planner@proj-a · msg 4611686018427387905
  sure, one moment
[comms] end · msg 4611686018427387905
```

The frame line prints only what the reader can't get elsewhere:

```
[comms] from reviewer@proj-a · msg <id> · re: <id>
[comms] from steward (standing) · msg <id> · [reply-once]
[comms] from planner@proj-a · msg <id> · [open]
[comms] from reviewer@proj-a · msg <id> · re: <id> · [1 reply left]
[comms] from critic@proj-a · msg <id> · [no reply]
[comms] from planner@proj-a (unverified) · msg <id>
[comms] directive from planner@proj-a · your director · msg <id>
```

No mark means `chosen` and `verified`. Deviations print in one parenthetical, tier
first: `(standing)`, `(ephemeral)`, `(unverified)`. `in <room>` prints
only outside `#names`. Control and format characters are removed from bodies, and a
body line can never start at column zero, so a frame line forged inside a body is
visibly indented.

A batch delivered from the record says so in its header, with its age:

```
== comms catch-up: 4 message(s) (oldest 15h ago) ==
== comms catch-up: 4 message(s) (oldest 15h ago; attribution is unverified in replay) ==
```

The second form appears only when every message in the batch is unverified.

A message handed from the queue (see "Held mail") says that it waited, when it was sent
and for how long:

```
[comms] from core-lead · msg <id> · [held] sent Oct 10 23:41, waited 9h · [reply-once]
```

`sent` is the time alone on the day it was sent; the wait is minutes under an hour,
hours under two days, then days.

Directors are granted by the recipient at launch (`moxi-comms-claude --director`, the
`directors` key in the Codex and OpenCode rosters), never claimed by the sender. A
declared director's verified message renders as a directive. The operator's messages
always do.

The first thing a Claude session is shown at start is the agent context block
(`context.CONTEXT_BLOCK`, with `context.DIRECTOR_LINE` after it for a session that
declared a director) and the current roster. Everything else an agent meets only
sometimes is explained on demand by `moxi comms help <term>`.

## Delivery rules

These hold for every adapter. How an adapter is built, and what makes a CLI a harness,
is in [adapters.md](adapters.md).

1. **The adapter writes the cursor.** Each identity has a cursor file,
   `STATE/comms/cursors/<backend>/<identity>.json`: the newest message id the model saw
   in a completed turn. "Handled" means exactly that, and nothing stronger is claimed.
2. **Delivery waits for a turn boundary.** A Claude session's shim queues mail and
   injects one digest when a turn ends or the session is idle, then advances the cursor
   when the turn that read it completes. Mail released from the queue never goes into
   a running turn. At every turn end the adapter writes its cursor first and then sends
   `activity kind=turn` with `handled` and `unhandled` (see "The daemon socket"). Queued
   mail goes out of id order, so the cursor never passes an id the adapter still holds
   and has not seen in a completed turn: it advances to the lower of the highest
   completed digest and one below the lowest id still held.
3. **Catch-up on bind.** When a name binds, the daemon replays from the record what its
   cursor hasn't passed. With a cursor, that is everything after it. With no cursor at
   all (a new name, or one whose STATE was lost), replay is bounded to the newest
   `REPLAY_N` (50) messages no older than `REPLAY_T` (24 hours), and the digest says that
   older mail was omitted.
4. **Waiting mail is listed by name.** `moxi comms agents --seats` lists, for each name, the
   mail waiting for it in the queue: how many messages, and how long the oldest has
   waited.
5. **A stalled turn is looked up, not announced.** The adapter watches its own
   injection, and says so on its status line if no turn starts within
   `INJECT_STALL_WINDOW` (15 s). `moxi comms explain <identity>` prints which of four cases
   an identity is in: `handled`, `not-addressable`, `delivered-and-ignored`, or
   `delivered-but-the-turn-never-completed`.
6. **Slots come back after a restart.** On shutdown the daemon writes which names held
   which slots (`STATE/comms/slot-keeps.json`); for `SLOT_KEEP` (120 s) after it starts
   listening, each slot is kept for the name that held it.

## Held mail

Every message to a name is either delivered live or written to the name's queue. Live
means the name is bound, comms reads its prompt cache as `warm`, `fresh` (nothing cached
to lose) or `no-cache`, and the message does not carry `[held]`. Anything else waits: the
name is offline, its cache is cold, or the message is flagged. One queue holds all three,
for every adapter alike. It is a table in the record (`queue`, one row per waiting name
and message), loaded by the daemon before its socket listens. A row stays until the
adapter it was handed to reports the turn that read it completed, so held mail survives
restarts, and the record keeps a waiting message past `retain_days` whatever its age.
No sender, lane, flag or adapter is exempt from the cache rule: a message that wakes an
agent on an expired cache writes all of its context to cache again at full price, and
the operator ruled on 2026-10-05 that no sender may cause that.

A queued message goes out only at one of its name's release points, never because
another message to the name went out. Each release hands every row the binding was not
already handed, oldest first, with `held: true` on the payload:

| Event from the binding's own connection | Cache read? | Releases |
|---|---|---|
| `ready` with `source` `startup`, `clear` or `compact` | no | every row |
| `ready` with `source` `resume`, or any other value | yes | every row if it passes |
| `ready` with no `source` | - | nothing |
| `turn` carrying `handled` | yes, and again for a settle window if it does not pass | every row at the first read that passes; then the report settles |
| `turn` with no `handled` | - | nothing, and nothing is settled |
| `register` | yes | rows of unflagged messages if it passes; flagged rows wait |
| a live message to the name, an `activity` with no `kind` | - | nothing |

A turn end is a release point that stays open for a short window, not an instant. The
harness can write the turn's own record after the turn's end reaches the daemon (Claude
Code drains its transcript writes on a 100 ms timer), so the read at the frame can judge
the cache as it was before the turn, and the turn that warmed a cold cache would not
count. When a `turn` frame's read does not pass, whatever the queue holds, the daemon
opens a settle window for the binding and reads the cache again at 0.25, 0.5, 1, 2, 4
and 8 s after the frame (`RELEASE_SETTLE_STEPS`). At the first read that passes it
releases every row then queued for the name, whenever the row was queued, except the ids
that frame reported `unhandled`, which wait for the next release point. A newer `turn`
of the binding, or the binding's end, closes the window early; a `ready`, `look` or
register takes its own single read and leaves it open. A window whose last read still
does not pass logs one line naming the binding, the read and the window's length, and
the rows wait for the next release point. Only cache's read of the harness's record says
a cache is warm: a turn end alone never releases. A window lives in the daemon's memory,
so a restart loses it; a `[held]` row then waits for the next turn end, and nothing goes
out cold.

A send whose gate read is cold inside a window is queued and goes at the window's pass;
one whose read passes goes live. A send whose read began before a release of its name
began, and whose row would be queued after it, reads once more and that read decides,
so a read made in the same lag never leaves its row behind the release. A `[held]` send
takes no read and always waits for the next turn end.

A Claude Code `turn` frame can also come at a prompt: a later prompt after the
digest's own means the shim cannot tell whether the digest's turn was interrupted (Esc),
so it names the digest's ids `unhandled`, and the agent may see the digest twice. It is
a release point like any other.

A frame with no `source` or no `handled` comes from an adapter started before the
queue; such a binding gets its unflagged rows at its next register and its flagged ones
once a binding of a current adapter takes the name, and may see a message twice, never
lose one. A row handed to a binding is handed to it again only after its adapter names
it `unhandled` or the binding ends, at the next release point. A handed id that has no
row, that the adapter did not name handled and that its cursor has not passed gets a row
when the binding ends; so does an id whose hand-over failed on the socket. An `agent-N`
name has no rows while unbound and loses them when its binding ends.

`moxi comms send` asks the daemon for its read of each bound recipient's cache. A recipient
that reads cold is held: the message is posted, exit 0, and the output is the `sent
<id>` line, one `held:` line per held recipient saying when its cache went cold, any
NOTEs, and a last line that says when each held recipient reads it. A recipient comms
cannot read (no session id reported yet, a transcript missing or unreadable, a failed
`session` query, a name that unbound while the verb checked it) refuses the whole send,
exit 6, nothing posted, `--held` included: the refusal's first line, "Nothing was sent:
...", names each one and why, and the cure is to send it again in a moment. On every
exit path the outcome is the last line. `--held` waits for the recipient's own turn
even when it is warm; to the operator it is ignored, with a NOTE, and line zero carries
`[held]` only when another recipient takes it.

comms reads no cache itself: the cache module (`moxi-cache`, installed with `moxi install
comms --with cache`) says what each recipient's cache is, and comms decides what happens
to the mail. comms reaches it through one file, `src/moxi_comms/cache.py`, and only
through `moxi.modules.load("cache", interface=1)`. **Without the cache module every agent
reads `no-cache` and gets its mail live**: nothing is held for a cold cache and nothing
is refused for an unreadable one. `[cache] required = true` in comms' config makes the
daemon refuse to start when the cache module is absent or broken.

Each harness has its clock, which cache reads. A Claude session's cache counts from its
transcript's last request, for the lifetime of the cache tier its requests wrote (one hour
or five minutes), or cache's configured lifetime when no request names one. A codex
thread's counts from the end of its rollout's last request, an opencode session's from the
start of its last request in `opencode.db`, each for the lifetime of its harness, provider
and model in cache's lifetime table: 30 minutes for OpenAI's GPT-5.6 and later, 5 minutes
for Anthropic behind OpenCode, and on the Codex models a longer probe measured, that
number, which cache lowers for good when the daemon's read sees a cache go cold sooner
(cache's drift record, written by the daemon's learning store; one daemon log line per
lowering). A model the table
lists as free (OpenCode Zen's `opencode/big-pickle`), a
model or provider with no row, and an adapter comms has no clock for read `no-cache` and
are delivered to at any age. Anything else comms cannot read with evidence (a rollout or
row it does not recognise, a reverted thread, a revert pending, a next model it cannot
tell) reads `unknown`; a model change reads cold until a turn on the new model.

The daemon reads the cache again at every delivery: live routing, the rescan and
register replay, once per binding per pass. A known read decides alone. A read that is
`unknown` at the daemon, or that raised, is decided by the send's `[cold-at:…]` value
for that recipient: a time still ahead of the daemon's clock, or `-`, delivers live (a
`-` has no time and never expires); a time reached, or a line with no `[cold-at:…]` at
all, gets a row. Release points never read the cold-at: a row waits for a passing read
or a release point that reads nothing. `moxi comms explain` names a queued message's case
`waiting`.

## Reply enforcement

A Claude session that ends a turn owing a reply is held open once at its Stop: the
session's adapter is the session's holder with Moxi turns, which asks it at each Stop.
A message owes a reply when it is a verified `[reply-once]` ask from anyone, or is from
the operator on any posture but `[no reply]` and `[reply-optional]`; and it is under a day old. Nothing else owes. The session pays it by messaging the
author afterwards; a debt to the operator is paid only by a message to `operator`. With a debt and nothing else running, the Stop is held once,
naming what is owed; a debt still unpaid at the next Stop is dropped and shown in
`moxi comms agents --seats` as `unanswered`. `moxi comms help owed` is the agent-facing version.

## The daemon socket

JSON lines over `RUNTIME/moxi-comms-daemon.sock` (0600, in a 0700 directory). A request
carries an `id`, which the reply echoes. Frames the daemon pushes (`deliver`,
`replay_complete`, `notice`) carry none, which is how a client tells a reply from a
push on one connection.

```
->  {"id": 1, "op": "register", "slot": "next-free", "name": "planner",
     "scope": "proj-a", "adapter": "claude-shim"}
<-  {"id": 1, "ok": true, "identity": {...}, "slot": 1, "hwm": "...", "protocol": 1}
<-  {"op": "deliver", "payload": {...}}
<-  {"op": "replay_complete", "identity": "planner@proj-a", "cursor": "..."}
->  {"id": 2, "op": "query", "kind": "resolve", "name": "reviewer"}
->  {"op": "injected", "identity": "...", "through": "<id>", "seq": 7, "digest": "..."}
->  {"op": "activity", "identity": "...", "seq": 8, "kind": "turn",
     "handled": ["<id>", ...], "unhandled": []}
->  {"op": "activity", "identity": "...", "seq": 9, "kind": "ready", "source": "clear"}
->  {"id": 5, "op": "ping"}
<-  {"id": 5, "ok": true, "cli_protocol": 2}
```

`kind: "turn"` is sent at every turn end of the agent, and, for a Claude Code session,
at the prompt after an interrupt, after the cursor write, and only for a turn that ran. `handled` lists the ids of every digest whose turn completed since
the adapter's last `turn` frame, and `unhandled` those whose turn ended any other way
(failed, interrupted, errored) and that the adapter no longer holds; both are present,
empty when nothing ended. A `handled` id the binding was handed loses its row; an
`unhandled` one gets a row. An id the binding was not handed is logged and ignored.
`kind: "ready"` is sent at every session start, clear, compaction and resume, with
`source` `startup`, `resume`, `clear` or `compact` as Claude Code names them (any other
value reads as `resume`); one that comes before the adapter is bound is sent once,
right after its `register` is answered. "Held mail" has what each one releases. The
daemon takes `kind`, `source`, `handled` and `unhandled` only from the binding's own
connection.

`ping` answers `cli_protocol`, the verbs' contract with their daemon: what a verb writes
on line zero and which answers it reads. Every verb sends it when it connects, and an
answer without it reads 0. On a mismatch the verb exits 4, nothing done: "the comms
daemon speaks verb protocol 0 and this comms speaks 2; restart the daemon (moxi comms service
status names its unit), then run this again. Nothing was sent."

A claude-shim's `register` and every `activity` also carry `session_id`,
`projects_root` (where its session's Claude Code writes transcripts) and `turned`
(whether a turn has been seen in that session), and a Claude session's adapter adds
`endpoint` (whether it has found the session's Moxi turns endpoint). A turn's start
sends an `activity` with no `kind` at once, so the daemon learns `turned` before the
turn ends; it releases nothing. The daemon takes them only from the binding's own
connection, and reads a missing transcript as `fresh` only on that evidence. A bound
name whose adapter reports `endpoint: false` reads `waiting` to a sender (the `session`
query answers `endpoint` and `endpoint_for`, the `agents` query `endpoint` and
`endpoint_since`). A session start, resume, `/clear` or compaction sends `kind: "ready"`
with its `source` (`startup`, `resume`, `clear` or `compact`); one that comes before the
shim is bound is sent once, right after its `register` is answered. The adapter's first
look at an idle session's endpoint sends `kind: "look"`: a release point on a passing
read that settles nothing and is no evidence of a turn. Every turn end sends `kind: "turn"` with `handled` (the ids of each digest whose
turn ended with an answer since the last such frame) and `unhandled` (those whose turn
was interrupted, ended in an error or a refusal, or whose delivery turns refused or
lost), after the cursor write. The idle tick that hands mail to a session at its prompt
sends an `activity` with no `kind`.

A codex or opencode launcher's `register` and every `activity` carry `session_id` (the
thread or session id), `cache_source` (the Codex sessions root of an app-server the
launcher spawned, or the path `opencode db path` gives) and `turned`; `register` also
carries `new_session`. Since 2026-10-08 (operator ruling: a Codex or OpenCode launch
starts its own thread or session, as a Claude launch does) neither launcher reads its
record, so every launch's `register` reports `new_session: true` and its `turned` is
false until its own first turn ends; a `register` later in the same launch reports the
held id with `new_session: false`, and no reconnect or restart inside the launch makes
it fresh again. The opencode launcher adds `agent_model`,
`{"agent": <name>, "model": "<provider>/<model>" or null}`, the default agent it named
from `opencode debug config` and that agent's model, or `null` when it could not name
one (which withdraws an earlier report). An `activity` with `kind: "ready"` and no
`source` only carries a new report; it releases nothing.

The `session` query answers the daemon's own read of the bound session's cache:
`cache_kind` (`warm`, `cold`, `fresh`, `no-cache` or `unknown`), `age_seconds`,
`cache_reason` (why it is `unknown`, a `cold` with no lifetime, or a free model's
`no-cache`), `cache_ttl` (the lifetime the read used, in seconds, None when it used
none), `cache_key` (what the lifetime was looked up by, in words, e.g. `codex
openai/gpt-5.6-terra`), `cache_code` (the read's reason code for a `no-cache` or
`unknown`, e.g. `module-absent` with no cache module, `no-row`, `no-record`; a caller
branches on it, not on `cache_reason`'s words) and `cache_source`.

The `owner` query asks `session`'s question the other way round: given a session id
(`session_id`), which live binding owns it now, and whom that identity awaits.

```
->  {"id": 3, "op": "query", "kind": "owner", "session_id": "3d19..."}
<-  {"id": 3, "ok": true, "session_id": "3d19...", "owner": "lead",
     "adapter": "claude-shim", "awaiting": ["planner@proj-a"]}
```

`owner` is the binding's qualified name as `agents` prints it, a global identity bare,
or null when no live binding holds the id: an id the daemon never heard and one whose
binding ended both answer null.
`awaiting` is the list the `owed` query computes for that identity: the bound names it
sent a verified `[reply-once]` ask to within a day that haven't answered. It is empty
with no owner. An empty `session_id` is refused. The query changes nothing. `moxi comms
owner <session-id> --json` prints it with `"ok": true` and exits 0, owner or none. A read
that fails exits non-zero and, with `--json`, prints `{"ok": false, "session_id": ...,
"error": ..., "detail": ...}`: `refused` (exit 2) for an empty id, which never reaches
the daemon; `offline` (exit 4) when the daemon is down or drops the connection;
`timeout` or `failed` (exit 5) when it doesn't answer or answers an error, as a daemon
older than the query does.

The `readable` query tells the send verb when a replier could first read each message
of an open thread, for the idle close:

```
->  {"id": 4, "op": "query", "kind": "readable", "identity": "reviewer@proj-a",
     "ids": ["4611686018427387905", "4611686018427387906"]}
<-  {"id": 4, "ok": true, "since": 1767268800.0,
     "handed": {"4611686018427387905": 1767270000.0},
     "waiting": ["4611686018427387906"]}
```

`handed` is when the daemon handed each asked id to that identity's binding (kept for an
hour, in memory); `waiting` lists the asked ids that wait in that identity's queue;
`since` is when the daemon started. The query decides nothing and changes
nothing.

The `attributed_message` query reads one message from the record with its author as the
daemon attributes it. The notify service asks it for the message an agent registers,
and takes the item's sender and text from the answer.

```
->  {"id": 4, "op": "query", "kind": "attributed_message",
     "message_id": "4611686018427387905"}
<-  {"id": 4, "ok": true, "message_id": "4611686018427387905", "found": true,
     "room": "#names",
     "author": {"name": "planner@proj-a", "tier": "chosen", "attribution": "verified"},
     "recipients": ["operator"], "resolved": ["operator"],
     "text": "the body, line zero removed", "timestamp": "2026-01-01T12:00:00Z"}
```

`author` is computed by the record and the custody journal (see Attribution), the same
way replay delivers it and `moxi comms history` prints it. It answers for who held the posting
seat when the message was posted, not for whoever holds it now, so it never reads
`verified` where live delivery read `labeled`. `attribution` is the wire value,
`verified` or `labeled`, as in a delivered payload. `recipients` are the tags line zero
names, as a delivered payload carries them. `resolved` is each tag as the daemon
resolves it now by the rule under "Identities", null for a tag that does not resolve. A
message with no machine line has no recipients, and its `text` is the whole body. Only
`#names` is routed, so check `room`. An id the record does not hold answers `found:
false` with every other field null or empty. An id that is not a message id is refused.
The query changes nothing. Any process that can open the socket may ask it, as for
`record_message`, `session` and `owner`: it answers nothing that `record_message`,
`moxi comms history` and `moxi comms reply` don't already show any process of your user.
`client.attributed_message(message_id)` asks it and returns None for an unknown id.

Ops: `register`, `query`, `injected`, `activity`, `unanswered`, `ping` and `nudge`.
Query kinds: `resolve`, `agents`, `injected`, `session`, `owner`, `asks`, `owed`,
`explain`, `binding_window`, `record_message`, `attributed_message`, `record_history`,
`record_max_id` and `rooms`.
`activity` and `injected` carry a `seq` that increases per connection, and the daemon
drops one that doesn't; that orders the pings, it doesn't authenticate them.

A delivered payload:

```json
{
  "message_id": "4611686018427387905",
  "channel_id": "...",
  "room": "#names",
  "author": {"name": "planner@proj-a", "tier": "chosen", "attribution": "verified"},
  "recipients": ["reviewer@proj-a"],
  "posture": "open",
  "open_mark": false,
  "backlog": false,
  "text": "the body, line zero removed",
  "reply_to": null,
  "audience": null,
  "audience_source": null,
  "timestamp": "2026-01-01T12:00:00Z"
}
```

`posture` is `open`, `none`, `optional` or `once`; `open_mark` is true
only for `[open]`. A reply in an open thread also carries `thread_mark`, `one-left` or
`last`. A message handed from the queue carries `held: true`; live delivery and replay
carry no `held`.

`agents` answers `waiting`, `{"<qualified name>": {"count": <n>, "oldest": <epoch
seconds>}}`, read from the queue, and `pending: {}`, always empty, which an adapter
started before the queue still reads.

`protocol` in the `register` reply is the claims protocol version (`PROTOCOL_VERSION`),
apart from the verbs' `cli_protocol`.

Before it binds the socket, a starting daemon knocks on any file already at the path.
A live answer is a refusal to start; only a refused or missing connection lets it
clear the path.

## Data layout

| path | written by | what |
|---|---|---|
| `DATA/comms/directory.db` | provisioning | rooms, slots, standing identities, the operator, webhook rows (0600) |
| `DATA/comms/bindings.db` | the daemon | the custody journal (0600, WAL) |
| `DATA/comms/cards/<identity>` | each identity | the card `moxi comms whois` reads |
| `STATE/comms/record.db` | every comms process | the record, on `local` |
| `STATE/comms/tokens/` | provisioning | one 0600 file per credential holder |
| `STATE/comms/cursors/<backend>/` | the adapters | one cursor per identity |
| `STATE/comms/slot-keeps.json` | the daemon | which name held which slot |
| `STATE/comms/codex/`, `STATE/comms/opencode/` | the Codex and OpenCode adapters | thread and session ids per identity |
| `RUNTIME/comms/moxi-comms-daemon.sock` | the daemon | the daemon socket |
| `CONFIG/comms/config.toml` | you | comms' settings, `block` among them |

The roots are in [configuration.md](configuration.md). Retention trims message bodies
older than `[local] retain_days` (90) at start and once a day; nothing the daemon
derives looks back further than 24 hours.

## Where the code differs from the original contracts

comms was built against two written contracts, a message contract and a delivery-adapter
contract, which aren't published. The code has moved past them in these places, and
this page describes the code:

- **The record is local.** The contracts take a hosted chat platform as the record.
  The supported backend is a SQLite file on the machine, with the same rooms, the same
  webhook-shaped credentials and the same machine line, so the rest of the protocol is
  unchanged.
- **A bare name is the global identity.** The contracts search a bare name in your own
  scope first and defer it to your own scope. The code reads a bare name as the global
  identity from every scope, as described under "Identities"; the custody journal only
  says whether a never-bound name needs `--new-name`.
- **An idle session is a turn boundary.** The contracts name the end of a turn as the
  boundary. A Claude session sitting at its prompt never ends a turn, so the shim treats
  an idle session (no turn running, the queue unchanged for `IDLE_SETTLE`) as standing at
  a boundary and injects then.
- **Replay is bounded only without a cursor.** A name with a cursor gets everything it is
  owed at the bind, however old; the 50-message and 24-hour bounds apply only when no
  cursor exists.
- **`moxi comms explain`, `moxi comms cache`, `moxi comms owner` and `moxi comms doctor` are read-only
  verbs** beyond the contracts' `help` and `whois`. They reach the daemon or local files
  and post nothing.
