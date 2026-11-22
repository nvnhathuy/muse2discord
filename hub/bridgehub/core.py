"""Transport-independent state machine. All mutations run on one asyncio loop."""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Agent
from .reliability import Reliability

log = logging.getLogger(__name__)


class BridgeError(Exception):
    pass


def snowflake(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise BridgeError("id must be a positive decimal string")
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text) or int(text) >= 2**64:
        raise BridgeError("id must be a positive Discord snowflake")
    return text


@dataclass(frozen=True)
class Message:
    id: str
    guild_id: int
    channel_id: int
    author_id: int
    content: str
    mentions: tuple[int, ...] = ()
    author_name: str = ""
    timestamp: str = ""
    attachments: tuple[str, ...] = ()

    def public(self):
        return asdict(self)

    @classmethod
    def restore(cls, data):
        data = dict(data)
        data["mentions"] = tuple(data.get("mentions", ()))
        data["attachments"] = tuple(data.get("attachments", ()))
        snowflake(data["id"])
        return cls(**data)


class Filter:
    def __init__(self, config, gate=None):
        self.config = config
        # Optional RoutingGate (hub_integration): when present, an agent
        # paused at hub level is excluded from routing on the next message.
        self.gate = gate
        self.counts = dict.fromkeys(config.agents, 0)
        self.patterns = {
            key: re.compile(r'"(?:' + "|".join(re.escape(unicodedata.normalize("NFC", t))
                                             for t in a.call_tokens) + r')(?!\w)', re.I)
            for key, a in config.agents.items()
        }

    def authorized(self, message):
        c = self.config
        return (message.author_id != c.hub_bot_id
                and message.guild_id == c.guild_id and message.channel_id == c.channel_id
                and message.author_id in {c.owner_id, *(a.bot_id for a in c.agents.values())})

    def route(self, message):
        if not self.authorized(message):
            return []
        owner = message.author_id == self.config.owner_id
        if owner:
            self.counts = dict.fromkeys(self.config.agents, 0)
        text = unicodedata.normalize("NFC", message.content)
        broadcast = "tất cả" in text.casefold()
        targets = []
        for key, a in self.config.agents.items():
            if not (broadcast or a.bot_id in message.mentions or self.patterns[key].search(text)):
                continue
            if self.gate is not None and not self.gate.is_enabled(key):
                continue
            if not owner and self.counts[key] >= 8:
                log.info("peer circuit dropped message=%s agent=%s", message.id, key)
                continue
            targets.append(key)
            if not owner:
                self.counts[key] += 1
        return targets


class Journal:
    """One append-only, fsynced JSONL file; an exclusive lock prevents two writers.

    Corruption (including a torn last record) fails closed, never silently skips data.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.failed = False
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            self.records = []
            for number, line in enumerate(self.file, 1):
                if not line.endswith(b"\n"):
                    raise BridgeError(f"Incomplete journal record at line {number}; preserve file and repair offline")
                record = json.loads(line)
                if record.get("v") != 1 or record.get("type") not in {
                        "message", "ack", "reply", "working", "warning", "delivery", "suspect",
                        "hub_start", "history_retire"}:
                    raise BridgeError(f"Unsupported journal record at line {number}")
                self.records.append(record)
        except BaseException:
            self.file.close()
            raise

    def append(self, record):
        if self.failed:
            raise BridgeError("Journal is unhealthy; restart after repairing storage")
        data = (json.dumps({"v": 1, **record}, ensure_ascii=False) + "\n").encode("utf-8")
        self.file.seek(0, 2)
        offset = self.file.tell()
        try:
            self.file.write(data)
            self.file.flush()
            os.fsync(self.file.fileno())
        except BaseException:
            # A transient flush/fsync error must not poison unrelated records.
            # Remove the uncertain tail before allowing another append; if the
            # rollback itself cannot be made durable, fail closed as before.
            try:
                self.file.seek(offset)
                self.file.truncate()
                self.file.flush()
                os.fsync(self.file.fileno())
            except BaseException:
                self.failed = True
            raise

    def close(self):
        self.file.close()


class Hub(Reliability):
    def __init__(self, config, transport, *, typing_interval=8.0, typing_grace=180.0, clock=None, sweeper_interval=10.0):
        self.config, self.transport = config, transport
        # Agent keys come from the config (env defaults or, in integrated
        # mode, the shared registry); the watcher keeps this tuple current.
        self.agent_keys = tuple(config.agents)
        # Integration seams (PATCH-POINTS section 2); all None in legacy mode.
        self.routing_gate = None
        self.registry_watcher = None
        self.bot_lifecycle = None
        # API-agent executor hook (issue #22): called with
        # (agent_key, message) whenever a message is enqueued for an
        # agent; the executor itself decides whether the agent is an
        # API agent it should answer for. None = feature not wired.
        self.api_delivery_hook = None
        self._loop = None  # captured by __main__ for the admin-thread ops
        self.journal = Journal(config.journal_path)
        self.sources = {}
        self.history_retired = set()
        self.history_context = set()
        self.acked = dict.fromkeys(self.agent_keys, 0)
        self.replies = {}
        self._init_reliability(clock, sweeper_interval)
        journal_was_empty = not self.journal.records
        starts = [float(rec["at"]) for rec in self.journal.records
                  if rec["type"] == "hub_start" and isinstance(rec.get("at"), (int, float))]
        # Older releases kept startup timestamps only in metrics. Use those as a
        # migration aid when the journal predates durable hub_start records.
        if not starts and not journal_was_empty:
            starts = self._previous_metric_hub_starts()
        now_wall = time.time()
        self.previous_hub_start = max(starts) if starts else None
        self.first_hub_start = min(starts) if starts else now_wall
        self.current_hub_start = now_wall
        self.hub_start_recorded = False
        self.hub_start_metric_recorded = False
        for rec in self.journal.records:
            if rec["type"] == "message":
                msg = Message.restore(rec["message"])
                self.sources[msg.id] = msg
                if rec.get("history_context"):
                    self.history_context.add(msg.id)
            elif rec["type"] == "history_retire":
                self.history_retired.add((rec["agent"], snowflake(rec["id"])))
            elif rec["type"] == "ack":
                # setdefault: a journal may name an agent the registry has
                # since removed; replay must not crash on it at boot.
                self.acked.setdefault(rec["agent"], 0)
                if rec.get("exact"):
                    self.completed.setdefault(rec["agent"], set()).add(snowflake(rec["id"]))
                elif rec.get("reset"):
                    # Admin cursor reset (PATCH-POINTS section 2): ASSIGN,
                    # never max() - the cursor may legitimately move back.
                    self.acked[rec["agent"]] = int(rec["id"])
                else:
                    self.acked[rec["agent"]] = max(self.acked[rec["agent"]], int(snowflake(rec["id"])))
            elif rec["type"] == "reply":
                key = (rec["agent"], snowflake(rec["reply_to"]))
                self.replies[key] = rec["state"]
                self.acked.setdefault(key[0], 0)
                if rec.get("exact"):
                    self.completed.setdefault(key[0], set()).add(key[1])
                else:
                    self.acked[key[0]] = max(self.acked[key[0]], int(key[1]))
            else:
                self._restore_reliability(rec)
        for agent in self.agent_keys:
            self.suspects[agent].difference_update(self.completed[agent])
            self.suspects[agent] = {mid for mid in self.suspects[agent] if int(mid) > self.acked[agent]}
        self.journal.records.clear()
        self.events = {a: asyncio.Event() for a in self.agent_keys}
        self.waiters = dict.fromkeys(self.agent_keys, 0)
        self.typing_interval, self.typing_grace = typing_interval, typing_grace
        self.typing = {}
        self.typing_since = {}
        self.typing_stopped = set()
        self.background = set()
        self.say_locks = {a: asyncio.Lock() for a in self.agent_keys}
        self.recovery_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.recovering = True
        self.buffer = {}
        self.closed = False
        self._rebuild()
        self._retire_stale_history()
        self._reset_reliability_clocks()

    def _previous_metric_hub_starts(self):
        starts = []
        try:
            with self.metrics_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if row.get("event") == "hub_start" and isinstance(row.get("t"), (int, float)):
                        starts.append(float(row["t"]))
        except OSError:
            pass
        return starts

    @staticmethod
    def _message_epoch(message):
        if not message.timestamp:
            return None
        try:
            value = datetime.fromisoformat(message.timestamp.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    def _retire_history(self, agent, mid, reason):
        key = (agent, mid)
        if key in self.delivery:
            return
        if key in self.history_retired:
            self.queues[agent].pop(mid, None)
            return
        self.journal.append({"type": "history_retire", "agent": agent, "id": mid, "reason": reason})
        self.history_retired.add(key)
        self.queues[agent].pop(mid, None)
        self.queued_at.pop(key, None)
        self.typing_stopped.add(key)
        self._metric("history_retire", agent=agent, id=mid, reason=reason)

    def _retire_stale_history(self):
        # Self-heal old journals produced by releases that queued all fetched
        # history. Preserve anything that has ever reached a delivery record.
        stale = []
        for agent in self.agent_keys:
            for mid in self.queues[agent]:
                message = self.sources[mid]
                timestamp = self._message_epoch(message)
                if ((agent, mid) not in self.delivery and timestamp is not None
                        and timestamp < self.first_hub_start):
                    stale.append((agent, mid))
        for agent, mid in stale:
            self._retire_history(agent, mid, "before_first_hub_start")

    def _rebuild(self):
        self.max_seen = max(map(int, self.sources), default=0)
        self.filter = Filter(self.config, self.routing_gate)
        self.queues = {a: {} for a in self.agent_keys}
        self.targets = {}
        for msg in sorted(self.sources.values(), key=lambda m: int(m.id)):
            targets = self.filter.route(msg)
            self.targets[msg.id] = targets
            for a in targets:
                delivered = (a, msg.id) in self.delivery
                if (int(msg.id) > self.acked[a] and msg.id not in self.completed[a]
                        and msg.id not in self.suspects[a] and (a, msg.id) not in self.history_retired
                        and (msg.id not in self.history_context or delivered)):
                    self.queues[a][msg.id] = msg
        for a in self.agent_keys:
            self._low_water(a)
        for a in self.agent_keys:
            self._signal(a)

    def _signal(self, agent):
        if self._deliverable(agent):
            self.events[agent].set()
        else:
            self.events[agent].clear()

    def _agent(self, agent):
        if agent not in self.config.agents:
            raise BridgeError("agent must be one of: " + ", ".join(self.config.agents))

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def _effect(self, method, *args):
        try:
            await getattr(self.transport, method)(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log arbitrary SDK exception strings: they can contain request details.
            log.warning("background %s failed (%s)", method, type(exc).__name__)

    def _start_typing(self, agent):
        if self.queues[agent] and (agent not in self.typing or self.typing[agent].done()):
            self.typing[agent] = self.spawn(self._typing_loop(agent))

    async def _typing_loop(self, agent):
        while not self.closed:
            active = [mid for mid in self.queues[agent]
                      if (agent, mid) not in self.typing_stopped]
            if not active:
                return
            now = time.monotonic()
            if not self.waiters[agent] and not any(
                    now - self.typing_since.get((agent, mid), -float("inf")) < self.typing_grace
                    for mid in active):
                return
            await self._effect("typing", agent)
            await asyncio.sleep(self.typing_interval)

    def _enqueued(self, agent, message, *, react=True):
        if (agent, message.id) not in self.queued_at:
            self.queued_at[(agent, message.id)] = self.started_at + 300 if self.initial_recovery else self.clock()
            self._metric("enqueue", agent=agent, id=message.id)
        self.typing_since[(agent, message.id)] = time.monotonic()
        self.typing_stopped.discard((agent, message.id))
        self._signal(agent)
        self._start_typing(agent)
        if react:
            self.spawn(self._effect("react", agent, message.id))
        if self.api_delivery_hook is not None:
            try:
                self.api_delivery_hook(agent, message)
            except Exception:
                # The hook belongs to the API-agent executor; its
                # failure must never break enqueueing for anyone.
                log.warning("api delivery hook failed agent=%s", agent)

    async def ingest(self, message):
        snowflake(message.id)
        if self.is_hub_message(message) or not self.filter.authorized(message):
            return []
        if self.recovering:
            self.buffer.setdefault(message.id, message)
            return []
        if message.id in self.sources:
            return []
        # Gateway callbacks from three clients can arrive out of order. Rebuild in
        # snowflake order rather than allowing duplicate callbacks to reset counters.
        in_order = int(message.id) > self.max_seen
        before = {a: set(self.queues[a]) for a in self.agent_keys} if not in_order else None
        self.journal.append({"type": "message", "message": message.public()})
        self.sources[message.id] = message
        if in_order:
            self.max_seen = int(message.id)
            targets = self.filter.route(message)
            self.targets[message.id] = targets
            for a in targets:
                if int(message.id) > self.acked[a] and message.id not in self.completed[a] and message.id not in self.suspects[a]:
                    self.queues[a][message.id] = message
                    self._enqueued(a, message)
        else:
            self._rebuild()
            for a in self.agent_keys:
                for mid in set(self.queues[a]) - before[a]:
                    self._enqueued(a, self.queues[a][mid])
        return self.targets[message.id]

    async def recover(self, fetch_history):
        """Fetch EACH bot's history after its cursor; hold waits/live delivery until merged.

        fetch_history(agent, after_id) returns an async iterator. Failure leaves
        buffered live messages intact and readiness closed; caller must retry.
        """
        async with self.recovery_lock:
            self.recovering = True
            self.ready.clear()
            fetched = {}
            async def fetch(agent):
                async for msg in fetch_history(agent, self.acked[agent]):
                    if not self.is_hub_message(msg) and self.filter.authorized(msg):
                        fetched[msg.id] = msg
            await asyncio.gather(*(fetch(a) for a in self.agent_keys))
            fetched.update(self.buffer)
            live_ids = set(self.buffer)
            new_ids = set(fetched) - self.sources.keys()
            history_context = set()
            for mid in sorted(new_ids, key=int):
                msg = fetched[mid]
                timestamp = self._message_epoch(msg)
                is_old_history = (mid not in live_ids and (
                    self.previous_hub_start is None
                    or (timestamp is not None and timestamp <= self.previous_hub_start)))
                if is_old_history:
                    history_context.add(mid)
                self.journal.append({"type": "message", "message": msg.public(),
                                     "history_context": is_old_history})
                self.sources[mid] = msg
                if is_old_history:
                    self.history_context.add(mid)
            self.buffer.clear()
            self._rebuild()
            # A previous retry may have persisted the source before failing to
            # append its retirement audit record. Keep finishing those records.
            history_context.update(self.history_context)
            for mid in sorted(history_context, key=int):
                msg = self.sources[mid]
                timestamp = self._message_epoch(msg)
                if self.previous_hub_start is None:
                    reason = "first_run_history"
                elif timestamp is not None and timestamp <= self.previous_hub_start:
                    reason = ("before_first_hub_start" if timestamp < self.first_hub_start
                              else "before_previous_hub_start")
                else:
                    reason = "first_run_history"
                for agent in self.targets.get(mid, ()):
                    self._retire_history(agent, mid, reason)
            if not self.hub_start_recorded:
                self.journal.append({"type": "hub_start", "at": self.current_hub_start})
                self.hub_start_recorded = True
            if not self.hub_start_metric_recorded:
                self._metric("hub_start")
                self.hub_start_metric_recorded = True
            self.previous_hub_start = self.current_hub_start
            self.recovering = False
            self.ready.set()
            for a in self.agent_keys:
                for mid, msg in self.queues[a].items():
                    self._enqueued(a, msg, react=mid in new_ids)
            self.initial_recovery = False
            self.start_maintenance()

    async def wait(self, agent, timeout=540):
        self._agent(agent)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 <= timeout <= 86400:
            raise BridgeError("timeout must be between 0 and 86400 seconds")
        self.waiters[agent] += 1
        self.last_wait_at[agent] = self.clock()
        self._metric("wait", agent=agent, alive_until=time.time() + 100)
        try:
            async def available():
                await self.ready.wait()
                while not self._deliverable(agent) or self.recovering:
                    self._signal(agent)
                    if self.recovering:
                        await self.ready.wait()
                    else:
                        await self.events[agent].wait()
                self._start_typing(agent)
            if not (self.ready.is_set() and self._deliverable(agent)):
                if timeout == 0:
                    return self._heartbeat(agent)
                try:
                    await asyncio.wait_for(available(), min(timeout, 50))
                except asyncio.TimeoutError:
                    return self._heartbeat(agent)
            messages = self._deliverable(agent)
            # One reply line stays bounded. Remaining messages appear after ack.
            messages = messages[:100]
            payload = []
            for message in messages:
                try:
                    payload.append(self._deliver(agent, message))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A failed append leaves this message in its queue with no
                    # delivery state; keep the rest of the batch independent.
                    log.error("delivery failed agent=%s id=%s (%s)",
                              agent, message.id, type(exc).__name__)
            self._signal(agent)
            return {"messages": payload,
                    "context": self._context(agent),
                    "last_acked_id": str(self.acked[agent]), "more": bool(self._deliverable(agent))}
        finally:
            self.waiters[agent] -= 1

    def _check_target(self, agent, mid):
        self._agent(agent)
        mid = snowflake(mid)
        if agent not in self.targets.get(mid, ()):
            raise BridgeError("reply/ack target was not routed to this agent")
        return mid

    def _advance(self, agent, mid):
        self.completed[agent].add(mid)
        self.queues[agent].pop(mid, None)
        self.suspects[agent].discard(mid)
        self.typing_stopped.add((agent, mid))
        self._low_water(agent)
        self._signal(agent)

    async def ack(self, agent, mid):
        return await self._ack(agent, mid)

    async def _ack(self, agent, mid, *, by=None):
        """Shared ack body; `by="admin"` marks an ack done on the agent's
        behalf from the admin page (PATCH-POINTS section 2)."""
        await self.ready.wait()
        mid = self._check_target(agent, mid)
        async with self.say_locks[agent]:
            if int(mid) > self.acked[agent] and mid not in self.completed[agent]:
                record = {"type": "ack", "agent": agent, "id": mid, "exact": True}
                if by:
                    record["by"] = by
                self.journal.append(record)
                self._advance(agent, mid)
                self._completion_metric(agent, mid, "ack2")
                await self._recover_warning(agent)
        return {"ok": True, "last_acked_id": str(self.acked[agent])}

    # ------------------------------------------------------------------
    # Admin ops (PATCH-POINTS section 2): synchronous facades called from
    # the admin page's thread; the work itself runs on the hub loop.
    # ------------------------------------------------------------------
    def _run_on_loop(self, coro):
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("hub event loop is not available for admin ops")
        try:
            on_loop_thread = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop_thread = False
        if on_loop_thread:
            # Blocking here would deadlock the hub loop; the admin page
            # always calls these ops from its own HTTP thread.
            raise RuntimeError("admin ops must be called from a non-loop thread")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)

    def admin_ack_on_behalf(self, agent, mid):
        result = self._run_on_loop(self._ack(agent, mid, by="admin"))
        return bool(result and result.get("ok"))

    def admin_reset_cursor(self, agent, to_id="0"):
        return self._run_on_loop(self._reset_cursor(agent, to_id))

    async def _reset_cursor(self, agent, to_id):
        self._agent(agent)
        text = str(to_id)
        cursor = 0 if text == "0" else int(snowflake(text))
        async with self.say_locks[agent]:
            self.acked[agent] = cursor
            self.journal.append({"type": "ack", "agent": agent, "id": str(cursor), "reset": True})
            for mid in [m for m in self.queues[agent] if int(m) <= cursor]:
                self.queues[agent].pop(mid, None)
                self.queued_at.pop((agent, mid), None)
                self.typing_stopped.add((agent, mid))
            self._low_water(agent)
            self._signal(agent)
        return str(self.acked[agent])

    # ------------------------------------------------------------------
    # Shared-registry hot reload (PATCH-POINTS section 2): a background
    # poll applies registry events without restarting the hub.
    # ------------------------------------------------------------------
    async def watch_registry(self, poll_interval=2.0):
        while not self.closed:
            await asyncio.sleep(poll_interval)
            try:
                events = self.registry_watcher.poll()
                if events:
                    await self._apply_registry_events(events)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("registry watch failed (%s); will retry", type(exc).__name__)

    async def _apply_registry_events(self, events):
        agents = self.registry_watcher.agents
        # (a) routing gate first: a pause bites on the very next message
        if self.routing_gate is not None:
            self.routing_gate.refresh(agents)
        # (b) config.agents follows the registry snapshot (mutated in
        # place: the runtime and the filter share this Config object)
        old_counts = dict(self.filter.counts)
        for key in list(self.config.agents):
            if key not in agents:
                del self.config.agents[key]
                self.config.receive_deadlines.pop(key, None)
        for key, profile in agents.items():
            old = self.config.agents.get(key)
            token = profile.token or (old.token if old is not None else "")
            self.config.agents[key] = Agent(key, profile.bot_id, token,
                                             profile.display_name, tuple(profile.call_tokens))
            if profile.receive_deadline is not None:
                self.config.receive_deadlines[key] = float(profile.receive_deadline)
            elif key not in self.config.receive_deadlines:
                self.config.receive_deadlines[key] = 180.
        self.agent_keys = tuple(self.config.agents)
        # (c) bot lifecycle through the runtime adapter
        if self.bot_lifecycle is not None:
            await self.bot_lifecycle.apply(events, agents)
        # (d) per-agent state for added / removed agents
        for event in events:
            if event.kind == "added":
                self._init_agent_state(event.key)
            elif event.kind == "removed":
                self._drop_agent_state(event.key)
        self.filter = Filter(self.config, self.routing_gate)
        for key, count in old_counts.items():
            if key in self.filter.counts:
                self.filter.counts[key] = count
        for key in self.agent_keys:
            if key in self.queues:
                self._signal(key)

    def _init_agent_state(self, key):
        self.acked.setdefault(key, 0)
        self.completed.setdefault(key, set())
        self.suspects.setdefault(key, set())
        self.last_wait_at.setdefault(key)
        self.last_progress_at.setdefault(key)
        self.receive_samples.setdefault(key, [])
        self.queues.setdefault(key, {})
        self.events.setdefault(key, asyncio.Event())
        self.waiters.setdefault(key, 0)
        self.say_locks.setdefault(key, asyncio.Lock())

    def _drop_agent_state(self, key):
        for store in (self.acked, self.completed, self.suspects, self.last_wait_at,
                      self.last_progress_at, self.receive_samples, self.queues,
                      self.events, self.waiters, self.say_locks):
            store.pop(key, None)
        for store in (self.delivery, self.queued_at):
            for compound in [k for k in store if k[0] == key]:
                del store[compound]
        self.typing_stopped = {k for k in self.typing_stopped if k[0] != key}
        self.typing.pop(key, None)
        self.typing_since = {k: v for k, v in self.typing_since.items() if k[0] != key}

    async def say(self, agent, reply_to, content, part=None, final=False):
        await self.ready.wait()
        mid = self._check_target(agent, reply_to)
        if not isinstance(content, str) or not content.strip():
            raise BridgeError("say requires nonempty stdin text")
        if not isinstance(final, bool):
            raise BridgeError("final must be boolean")
        if part is not None and (isinstance(part, bool) or not isinstance(part, int) or part < 1):
            raise BridgeError("part must be a positive integer")
        number = 1 if part is None else part
        final = final or part is None
        async with self.say_locks[agent]:
            key = (agent, mid)
            old = self.replies.get(key)
            if old and old["final"] and final:
                raise BridgeError("reply already finalized; a second --final is not allowed")
            if old and number <= len(old["parts"]):
                if old["parts"][number - 1] != {"content": content, "final": final}:
                    raise BridgeError("part retry differs from the saved part")
                return {"ok": True, "message_id": old["message_id"],
                        "last_acked_id": str(self.acked[agent]), "duplicate": True}
            if old and old["final"]:
                raise BridgeError("reply already finalized")
            parts = list(old["parts"]) if old else []
            if number != len(parts) + 1:
                raise BridgeError("parts must arrive sequentially starting at 1")
            text = "".join(p["content"] for p in parts) + content
            if len(text.encode("utf-16-le")) // 2 > 2000:
                raise BridgeError("combined reply exceeds Discord's 2000 UTF-16-unit limit")
            # Stop refreshes on receipt of the first valid say, even if Discord fails.
            self.typing_stopped.add((agent, mid))
            message_id = await self.transport.send_reply(agent, mid, text,
                                                        old["message_id"] if old else None)
            parts.append({"content": content, "final": final})
            state = {"message_id": snowflake(message_id), "parts": parts, "final": final}
            # Reply state + ack in ONE durable record, after Discord succeeds.
            self.journal.append({"type": "reply", "agent": agent, "reply_to": mid, "state": state, "exact": True})
            self.replies[key] = state
            self._advance(agent, mid)
            self._completion_metric(agent, mid, "say")
            await self._recover_warning(agent)
            return {"ok": True, "message_id": state["message_id"],
                    "last_acked_id": str(self.acked[agent]), "final": final}

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*list(self.background), return_exceptions=True)
        self._metric("hub_stop")
        self.journal.close()
