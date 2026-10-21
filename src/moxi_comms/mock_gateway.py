"""A mock record good enough to BE the record (bell-is-record).

The interface it implements lives in `gateway.py`, and `local_gateway.LocalGateway`
implements the same one on the box's SQLite record. The daemon, the sender and the
provisioner talk only to these methods and never learn which one they hold:

    channels        create_channel / channel_by_name
    webhooks        create_webhook (id + owner label) / resolve_secret / execute_webhook
    permissions     deny_send / can_send
    the record      history(channel, after=, limit=) / get_message
    the gateway     subscribe() -> Subscription, disconnect(), reconnect(resume=)

Fidelity the tests depend on:
  * monotonic message ids (string, comparable as ints);
  * 429s with Retry-After, injectable;
  * MESSAGE_CREATE events over a subscription, with a real RESUME-vs-fresh-IDENTIFY
    distinction (a non-RESUME reconnect loses the gap -- the daemon must rescan);
  * an optional JSON store so separate PROCESSES (the `comms` CLI, the daemon) share
    one record -- which is what makes a real end-to-end CLI smoke run possible;
  * a DECLARED slow-gateway hold on `max_message_id`, because a mock that is
    always instant hides every race the daemon runs against a real round trip.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .gateway import (
    MAX_CONTENT_LENGTH,
    NO_MENTIONS,
    Credential,
    Fatal,
    Forbidden,
    GatewayError,
    Message,
    RateLimited,
    Subscription,
    _State,
)

_EPOCH_BASE = 1_400_000_000_000  # only monotonicity matters


class MockGateway:
    """In-process record. Pass `store_path` to share the record across processes."""

    # Declared AND enforced (see execute_webhook), as the local record does: the parity
    # that matters is the parity in the REFUSALS.
    max_content_length = MAX_CONTENT_LENGTH
    #: `history(after=)` with no limit returns the whole range.
    history_bound = None

    # Declared AND enforced (see max_message_id): seconds of round trip on the record's
    # high-water-mark read. The mock answers it out of memory in microseconds; a real
    # record under load can take tens to hundreds of ms, and that difference
    # is the whole reason the repo's smokes structurally could not see a register-
    # concurrency bug -- the daemon's lock hold was ~5ms in test and ~200ms in production.
    # Set it and the round trip's DURATION becomes a test variable, in process.
    # Default 0.0 -- every existing test runs at mock speed.
    #
    # The delay is taken AFTER the record is read, so the answer is the record's state at
    # the START of the call: a REST reply in flight, which is exactly the staleness a
    # caller that reads outside its lock has to be safe against. It is also taken outside
    # `_txn()`, so a message CAN be posted during the round trip -- which is the whole
    # experiment. `subscribe()` and `reconnect()` read the high-water mark too, so a hold
    # engaged before them slows the gateway's lifecycle as well as the daemon's registers.
    max_message_id_hold = 0.0

    def __init__(self, store_path: str | Path | None = None, poll_interval: float = 0.02,
                 clock=time.time, max_message_id_hold: float | None = None):
        self.store_path = Path(store_path) if store_path else None
        self.poll_interval = poll_interval
        if max_message_id_hold is not None:      # else the class-level declaration stands
            self.max_message_id_hold = max_message_id_hold
        self.clock = clock          # record timestamps; injectable so tests can age mail
        self._state = _State()
        self._lock = threading.RLock()
        self._depth = 0
        self._subs: list[Subscription] = []
        self._connected = True
        self._rate_limits: list[float] = []
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        if self.store_path:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            if self.store_path.exists():
                with self._txn():
                    pass
            else:
                with self._txn(write=True):
                    pass

    # ------------------------------------------------------------------ transactions
    @contextlib.contextmanager
    def _txn(self, *, write: bool = False):
        """Reload shared state on entry (outermost only), persist on exit if writing."""
        with self._lock:
            outer = self._depth == 0
            if outer and self.store_path and self.store_path.exists():
                self._state.replace_with(json.loads(self.store_path.read_text()))
            self._depth += 1
            try:
                yield self._state
            finally:
                self._depth -= 1
            if write and self._depth == 0:
                self._save()

    def _save(self) -> None:
        if not self.store_path:
            return
        fd, tmp = tempfile.mkstemp(dir=self.store_path.parent, prefix=".gw.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self._state), f)
            os.replace(tmp, self.store_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _next_id(self, st: _State) -> str:
        st.seq += 1
        return str(_EPOCH_BASE + st.seq)

    # ------------------------------------------------------------------ channels
    def create_channel(self, name: str) -> str:
        with self._txn(write=True) as st:
            for cid, ch in st.channels.items():
                if ch["name"] == name:
                    return cid
            cid = self._next_id(st)
            st.channels[cid] = {"name": name}
            return cid

    def channel_by_name(self, name: str) -> str | None:
        with self._txn() as st:
            for cid, ch in st.channels.items():
                if ch["name"] == name:
                    return cid
            return None

    def channel_name(self, channel_id: str) -> str | None:
        with self._txn() as st:
            ch = st.channels.get(channel_id)
            return ch["name"] if ch else None

    # ------------------------------------------------------------------ webhooks
    def create_webhook(self, channel_id: str, name: str, secret_id: str) -> Credential:
        with self._txn(write=True) as st:
            if channel_id not in st.channels:
                raise GatewayError(f"unknown channel {channel_id}")
            wid = self._next_id(st)
            token = f"tok-{wid}"
            st.webhooks[wid] = {"channel_id": channel_id, "name": name,
                                "token": token, "secret_id": secret_id}
            st.secrets[secret_id] = wid
            # the mock hands the key back as the pointer. Callers record
            # `cred.secret_id` whichever backend they hold.
            return Credential(wid, token, secret_id=secret_id)

    def resolve_secret(self, secret_id: str, *, owner=None) -> Credential:
        """Resolution at use time: pointer on disk, URL only ever in memory.

        `owner` (the custody seam) is accepted and IGNORED here on purpose: the mock
        holds no credential store, so there is nothing for a holder key to select.
        Taking the argument keeps one interface for both implementations — a mock that
        refused it would make the seam real-backend-only and untestable.
        """
        with self._txn() as st:
            wid = st.secrets.get(secret_id)
            if wid is None:
                raise GatewayError(f"unknown secret {secret_id}")
            return Credential(wid, st.webhooks[wid]["token"], secret_id=secret_id)

    def webhook_channel(self, webhook_id: str) -> str | None:
        with self._txn() as st:
            wh = st.webhooks.get(webhook_id)
            return wh["channel_id"] if wh else None

    # ------------------------------------------------------------------ teardown
    # Operator machinery (a smoke run's teardown). No comms verb deletes anything;
    # these exist so a spike run can undo itself.
    def delete_webhook(self, webhook_id: str) -> None:
        with self._txn(write=True) as st:
            wh = st.webhooks.pop(str(webhook_id), None)
            if wh:
                st.secrets.pop(wh.get("secret_id", ""), None)

    def delete_channel(self, channel_id: str) -> None:
        channel_id = str(channel_id)
        with self._txn(write=True) as st:
            st.channels.pop(channel_id, None)
            for wid in [w for w, wh in st.webhooks.items()
                        if wh["channel_id"] == channel_id]:
                st.secrets.pop(st.webhooks[wid].get("secret_id", ""), None)
                st.webhooks.pop(wid, None)
            st.messages = [m for m in st.messages if m["channel_id"] != channel_id]

    # ------------------------------------------------------------------ permissions
    def register_principal(self, name: str) -> str:
        """A non-webhook principal (a bot user / the operator)."""
        with self._txn(write=True) as st:
            pid = self._next_id(st)
            st.users[pid] = name
            return pid

    def deny_send(self, principal: str, channel_id: str) -> None:
        with self._txn(write=True) as st:
            st.denied.setdefault(principal, [])
            if channel_id not in st.denied[principal]:
                st.denied[principal].append(channel_id)

    def can_send(self, principal: str, channel_id: str) -> bool:
        with self._txn() as st:
            return channel_id not in st.denied.get(principal, [])

    # ------------------------------------------------------------------ posting
    def inject_rate_limit(self, count: int = 1, retry_after: float = 0.01) -> None:
        """Queue `count` 429s for the next executes (test hook)."""
        with self._lock:
            self._rate_limits.extend([retry_after] * count)

    def execute_webhook(self, cred: Credential, content: str, *, username: str | None = None,
                        allowed_mentions: dict | None = None) -> Message:
        with self._lock:
            if self._rate_limits:
                raise RateLimited(self._rate_limits.pop(0))
        if allowed_mentions is None or allowed_mentions.get("parse") != []:
            raise GatewayError("allowed_mentions must be {'parse': []} on every post")
        if len(content) > self.max_content_length:
            # The record's own refusal: nothing is written, so an over-length post is a
            # send that did NOT happen here either.
            raise GatewayError(
                f"content is {len(content)} characters, "
                f"over the {self.max_content_length}-character cap on one message")
        with self._txn(write=True) as st:
            wh = st.webhooks.get(cred.webhook_id)
            if wh is None or wh["token"] != cred.token:
                raise GatewayError("bad webhook credential")
            if wh["channel_id"] in st.denied.get(cred.webhook_id, []):
                raise Forbidden(f"Send denied in {wh['channel_id']}")
            msg = Message(
                id=self._next_id(st), channel_id=wh["channel_id"], author_id=cred.webhook_id,
                author_kind="webhook", author_display=username or wh["name"],
                content=content, timestamp=self._now(),
            )
            st.messages.append(msg.to_dict())
        self._fanout()
        return msg

    def post_as_user(self, user_id: str, channel_id: str, content: str, *,
                     token: str | None = None) -> Message:
        """The operator is a first-class sender in every lane (types the machine line,
        `re:` token and all -- there is no native-reply back door here either).

        `token` is ACCEPTED AND IGNORED. The local
        backend guards this lane with the `operator` holder's credential and compares a
        hash; the mock holds no credential of any kind, so there is nothing here for a
        token to be compared against. Taking the keyword is what keeps one seam across
        both implementations -- a mock that refused it would make the sender's
        operator case untestable anywhere but on a real record.

        The one-message cap applies HERE TOO. It is a property of a message, not of
        the webhook route.
        """
        if len(content) > self.max_content_length:
            raise GatewayError(
                f"content is {len(content)} characters, "
                f"over the {self.max_content_length}-character cap on one message")
        with self._txn(write=True) as st:
            if channel_id in st.denied.get(user_id, []):
                raise Forbidden(f"Send denied in {channel_id}")
            msg = Message(
                id=self._next_id(st), channel_id=channel_id, author_id=user_id,
                author_kind="user", author_display=st.users.get(user_id, "operator"),
                content=content, timestamp=self._now(),
            )
            st.messages.append(msg.to_dict())
        self._fanout()
        return msg

    # ------------------------------------------------------------------ the record
    def history(self, channel_id: str, *, after: str | None = None,
                limit: int | None = None) -> list[Message]:
        with self._txn() as st:
            out = [Message.from_dict(m) for m in st.messages if m["channel_id"] == channel_id]
        if after is not None:
            out = [m for m in out if int(m.id) > int(after)]
        if limit is not None:
            out = out[-limit:]
        return out

    def marked(self, channel_id: str, token: str) -> list[Message]:
        """Every message in `channel_id` containing `token` (lowercased compare), no
        bound: the local record's boot read, on the in-memory double."""
        token = token.lower()
        return [m for m in self.history(channel_id) if token in m.content.lower()]

    def queue_add(self, name: str, ids) -> None:
        """The local record's queue rows, on the double: [message id, name] pairs."""
        with self._txn(write=True) as st:
            have = {tuple(p) for p in st.queue}
            for i in ids:
                pair = (str(i), str(name))
                if pair not in have:
                    st.queue.append(list(pair))
                    have.add(pair)

    def queue_remove(self, name: str, ids) -> None:
        gone = {(str(i), str(name)) for i in ids}
        with self._txn(write=True) as st:
            st.queue = [p for p in st.queue if tuple(p) not in gone]

    def queue_rows(self) -> list[tuple[str, str]]:
        with self._txn() as st:
            return sorted(((str(m), str(n)) for m, n in st.queue),
                          key=lambda p: (int(p[0]), p[1]))

    def drop_refusals(self) -> None:
        """The queue's migration: the refusals an older store kept go."""
        with self._txn(write=True) as st:
            if hasattr(st, "cold_refusals"):
                del st.cold_refusals

    def meta_get(self, key: str) -> str | None:
        with self._txn() as st:
            return st.meta.get(key)

    def meta_set(self, key: str, value: str) -> None:
        with self._txn(write=True) as st:
            st.meta[key] = str(value)

    def since(self, channel_id: str, timestamp: str) -> list[Message]:
        """Every message in `channel_id` at or after `timestamp`, no bound: the local
        record's boot read of the sent index, on the double."""
        return [m for m in self.history(channel_id) if m.timestamp >= str(timestamp)]

    def get_message(self, message_id: str) -> Message | None:
        with self._txn() as st:
            for m in st.messages:
                if m["id"] == message_id:
                    return Message.from_dict(m)
            return None

    def max_message_id(self, channel_id: str | None = None) -> str | None:
        with self._txn() as st:
            ids = [m["id"] for m in st.messages
                   if channel_id is None or m["channel_id"] == channel_id]
        answer = max(ids, key=int) if ids else None
        if self.max_message_id_hold:
            time.sleep(self.max_message_id_hold)   # the declared round trip, in flight
        return answer

    # ------------------------------------------------------------------ the gateway
    def subscribe(self, *, from_id: str | None = None) -> Subscription:
        sub = Subscription(from_id if from_id is not None else (self.max_message_id() or "0"))
        with self._lock:
            self._subs.append(sub)
            if self._pump is None and self.store_path is not None:
                self._pump = threading.Thread(target=self._pump_loop, daemon=True)
                self._pump.start()
        self._fanout()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub.close()

    def disconnect(self) -> None:
        """The socket drops. Messages posted from here on exist only in the record."""
        with self._lock:
            self._connected = False

    def reconnect(self, *, resume: bool = True) -> None:
        """RESUME replays the gap; a fresh IDENTIFY does NOT -- those events are gone."""
        with self._lock:
            self._connected = True
            subs = list(self._subs)
        newest = self.max_message_id() or "0"
        for sub in subs:
            if not resume:
                sub.last_id = newest
            sub._push({"t": "READY", "resumed": resume})
        self._fanout()

    def _pump_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self._fanout()
            except Exception:  # pragma: no cover - mock resilience
                pass

    def _fanout(self) -> None:
        with self._lock:
            if not self._connected:
                return
            with self._txn() as st:
                msgs = list(st.messages)
            subs = list(self._subs)
            for sub in subs:
                for m in msgs:
                    if int(m["id"]) > int(sub.last_id):
                        sub.last_id = m["id"]
                        sub._push({"t": "MESSAGE_CREATE", "d": m})

    def close(self) -> None:
        self._stop.set()
        for sub in list(self._subs):
            sub.close()

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))


__all__ = ["MockGateway", "NO_MENTIONS", "Credential", "Message", "Subscription",
           "GatewayError", "RateLimited", "Forbidden", "Fatal"]
