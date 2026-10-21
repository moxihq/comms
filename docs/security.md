# Security model

Moxi comms runs on one machine, for one user. Everything it does rests on that.

## Identity is a same-user claim

A comms name is a claim a process makes when it registers with the daemon. The daemon
checks the claim against who already holds the name and which slot's credential posted
a message, and the record keeps who held which slot when. None of that is
cryptography. It proves that a process running as your user said so, and nothing more.

Any process running as your user can:

- read the token store (`STATE/comms/tokens/`) and post as any holder in it, the
  operator included;
- read and write the record (`STATE/comms/record.db`) and the directory
  (`DATA/comms/directory.db`) directly;
- connect to the daemon's socket and register as any name that isn't held;
- connect to a session's Moxi turns endpoint and report a turn end that never happened,
  which advances that identity's cursor past mail the model never saw;
- send the daemon an `activity` ping for an identity, which changes what `moxi comms
  explain` reports for it.

So the trust boundary is your user account. comms keeps agents from confusing each
other by accident and makes who-said-what visible. It does not protect one agent from a
hostile process running as you, and it never claims to.

The scoped tokens, the 0600 files and the operator's own credential still matter: they
keep an honest agent on its own lane, they make a forged claim take deliberate work,
and a token copied off the machine spends one slot's credential, not all of them.

## What other users on the machine can see

- The daemon socket and every control socket are mode 0600 in a 0700 directory.
- Token store files are 0600 in a 0700 directory. comms refuses to read a token file or
  store directory with any group or other permission bit set.
- `directory.db` and `bindings.db` are 0600.
- `record.db`, which holds message bodies, and the cursor files are created with your
  umask. With the common umask of 022 they are readable by other local users. On a
  shared machine, set `umask 077` for the daemon (for the user service,
  `UMask=0077` with `systemctl --user edit moxi-comms-daemon.service`), or keep
  `STATE` under a 0700 directory.

## Credentials

On the local backend every slot, the daemon and the operator get a posting token. The
record keeps only each token's SHA-256; the token itself lives only in its holder's
file in the token store. Provisioning writes each token in the same call that creates
its row.

A token lost from the store can't be recovered: the record has only the hash. Running
provisioning again converges on what exists and doesn't mint replacements. So a
daemon that finds slots whose tokens are missing refuses to start and says
which holders are missing, rather than run on credentials nothing can present. Restore
the store from a backup, or move `directory.db` and `record.db` aside and start the
daemon again to provision a new set.

comms' config file, `config.toml`, holds non-secret values only. comms refuses to load
it when a key looks like a token.

## Message bodies are untrusted text

A message body is whatever another agent wrote. comms renders it inside a closed
envelope: every body line is indented two spaces, a closing line paired to the frame by
message id ends it, and control and format characters (escape sequences, bidi
overrides, zero-width characters) are removed. comms' own lines start at column zero
and a body can never reach column zero, so a frame line forged inside a body shows up
indented. The rule a model is taught is that only the left margin is comms speaking.

`moxi comms whois` cards are read the same way: a card is its holder's own claim, budgeted
and rendered in the same envelope, and never a source of authority.

A declared director's message is shown as a directive only when its attribution is
verified. That calibrates how a model weighs a message. It doesn't stop a model from
obeying a stranger or ignoring its director, and it isn't access control.

## The Codex adapter (experimental)

Codex's sandbox gates a Unix-socket `connect()` only through its network-access
setting, so a Codex agent that answers through `moxi comms reply` needs `network_access =
true`, and that grants the agent full outbound network. There is no narrower setting
that admits the daemon socket. Treat a Codex comms agent as not network-contained.

## Network

The supported path opens no network connection. The daemon, the CLI and the adapters
talk over Unix sockets and local files. There is no telemetry.

## Reporting a vulnerability

Email codemanethan@gmail.com with the details. Please don't open a public issue for a
security problem.
