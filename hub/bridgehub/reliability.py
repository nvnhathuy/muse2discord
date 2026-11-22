"""Section 3 lifecycle state. One monotonic sweep, no per-message timers.

The hub journal is authoritative; metrics are a separate append-only audit log.
Monotonic timestamps are intentionally rebased rather than replayed after restart.
"""
import asyncio
import json
import logging
import math
import os
import time
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


class Reliability:
    def _init_reliability(self, clock, interval):
        self.clock = clock or time.monotonic
        self.started_at = self.clock()
        self.run_id = uuid.uuid4().hex
        self.initial_recovery = True
        self.next_ping = self.started_at + 60
        self.sweeper_interval = interval
        keys = tuple(self.config.agents)
        self.completed = {a: set() for a in keys}
        self.suspects = {a: set() for a in keys}
        self.delivery = {}
        self.queued_at = {}
        self.last_wait_at = dict.fromkeys(keys)
        self.last_progress_at = dict.fromkeys(keys)
        self.receive_samples = {a: [] for a in keys}
        self.warning_state = []
        self.warning_lock = asyncio.Lock()
        self.sweeper_task = None
        self.restart_sources = set()
        self.metrics_path = Path(self.config.metrics_path or str(self.config.journal_path) + ".metrics.jsonl")
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_listening = False

    def _restore_reliability(self, rec):
        kind = rec["type"]
        if kind in {"delivery", "working"}:
            self.delivery[(rec["agent"], rec["id"])] = rec["state"]
            if kind == "working" and rec.get("latency") is not None:
                # setdefault: the journal may name a since-removed agent
                self.receive_samples.setdefault(rec["agent"], []).append(rec["latency"])
        elif kind == "suspect":
            self.suspects.setdefault(rec["agent"], set()).add(rec["id"])
        elif kind == "warning":
            self.warning_state = rec["groups"]

    def _reset_reliability_clocks(self):
        now = self.clock()
        self.restart_sources = set(self.sources)
        for a in self.config.agents:
            for mid in set(self.queues[a]) | self.suspects[a]:
                self.queued_at[(a, mid)] = now + 300
        for state in self.delivery.values():
            state["delivered_at"] = now
            state["first_at"] = now
            state["work_started"] = now if state.get("work_started") is not None else None
            state["lease_until"] = now + 300 if state.get("received") else None
            state["due"] = False
        for group in self.warning_state:
            for member in group["members"].values():
                member["elapsed_before"] = member.get("elapsed_before", 0) + max(0, group.get("saved_at", member["started"]) - member["started"])
                member["started"] = now
        for a in self.config.agents:
            self._signal(a)

    def _metric(self, event, **data):
        record = {"event": event, "t": time.time(), "monotonic": self.clock(), "run_id": self.run_id, **data}
        # Metrics failure must not turn a successful external reply into a retry.
        try:
            with self.metrics_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            log.error("metrics append failed; check storage")

    def _low_water(self, agent):
        cursor = self.acked[agent]
        for mid in sorted(self.targets, key=int):
            if int(mid) <= cursor or agent not in self.targets[mid]:
                continue
            if mid not in self.completed[agent]:
                break
            cursor = int(mid)
        self.acked[agent] = cursor

    def alive(self, agent):
        self._agent(agent)
        last = self.last_wait_at[agent]
        return last is not None and self.clock() - last <= 100

    def is_hub_message(self, message):
        if self.config.hub_bot_id and message.author_id == self.config.hub_bot_id:
            return True
        # Warning messages are posted by the runtime's default agent:
        # the first EFFECTIVE agent in config.agents (the "lead" key in
        # legacy mode, the first registry key in integrated mode) - the
        # same rule DiscordRuntime._default_agent uses in send_warning.
        # Config.Agent carries no leader role, so key order is the rule.
        default = next(iter(self.config.agents.values()), None)
        sender = self.config.hub_bot_id or (default.bot_id if default is not None else 0)
        return message.author_id == sender and message.id in {
            g[key] for g in self.warning_state for key in ("message_id", "tier2_id") if g.get(key)}

    def receive_deadline(self, agent):
        samples = sorted(self.receive_samples[agent][-1000:])
        if not samples:
            return self.config.receive_deadlines[agent]
        return max(180, 2 * samples[math.ceil(.95 * len(samples)) - 1])

    def _deliverable(self, agent):
        return [m for mid, m in sorted(self.queues[agent].items(), key=lambda kv: int(kv[0]))
                if (agent, mid) not in self.delivery or self.delivery[(agent, mid)].get("due")]

    def _context(self, agent):
        excluded = set(self.queues[agent]) | self.suspects[agent]
        entries = [m for mid, m in sorted(self.sources.items(), key=lambda kv: int(kv[0]))
                   if mid not in excluded]
        entries = entries[-self.config.context_count:] if self.config.context_count else []
        return [{"kind": "context_only", "do_not_reply": True, **m.public()} for m in entries]

    def _heartbeat(self, agent):
        return {"messages": [], "context": self._context(agent), "heartbeat": True}

    def _deliver(self, agent, message):
        key = (agent, message.id)
        old = self.delivery.get(key)
        now = self.clock()
        state = {"first_at": old["first_at"] if old else now, "delivered_at": now,
                 "received": False, "had_ack1": bool(old and old["had_ack1"]),
                 "lease_until": None, "work_started": old.get("work_started") if old else None,
                 "extensions": old.get("extensions", 0) if old else 0,
                 "redelivered": old["redelivered"] + 1 if old else 0, "due": False}
        self.journal.append({"type": "delivery", "agent": agent, "id": message.id, "state": state})
        self.delivery[key] = state
        self._metric("redelivery" if old else "delivery", agent=agent, id=message.id,
                     redelivered=state["redelivered"], had_ack1=state["had_ack1"])
        result = message.public()
        if old:
            reply = self.replies.get(key, {})
            result.update(redelivered=state["redelivered"], had_ack1=state["had_ack1"],
                          parts=[{"part": n + 1, **p} for n, p in enumerate(reply.get("parts", []))])
        return result

    async def working(self, agent, mid=None, eta=None):
        from .core import BridgeError
        self._agent(agent)
        if eta is not None and (isinstance(eta, bool) or not isinstance(eta, (float, int))
                                or not math.isfinite(eta) or eta <= 0):
            raise BridgeError("eta must be finite positive minutes")
        await self.ready.wait()
        async with self.say_locks[agent]:
            mids = [self._check_target(agent, mid)] if mid is not None else [
                m for m in self.queues[agent] if (agent, m) in self.delivery]
            if not mids:
                raise BridgeError("working requires delivered, unfinished work")
            now = self.clock()
            states = []
            for target in mids:
                old = self.delivery.get((agent, target))
                if target not in self.queues[agent] or not old:
                    raise BridgeError("working requires delivered, unfinished work")
                if old["extensions"] >= 6:
                    raise BridgeError("working lease limit of 30 minutes reached")
                state = dict(old)
                start = old["work_started"] if old["work_started"] is not None else now
                state.update(received=True, had_ack1=True, due=False, work_started=start,
                             extensions=old["extensions"] + 1,
                             lease_until=min(max(now, old["lease_until"] or now) + 300, start + 1800))
                if state["lease_until"] <= now:
                    raise BridgeError("working lease limit of 30 minutes reached")
                states.append((target, state, None if old["had_ack1"] else now - old["delivered_at"]))
            for target, state, latency in states:
                self.journal.append({"type": "working", "agent": agent, "id": target,
                                     "state": state, "latency": latency, "eta": eta})
                self.delivery[(agent, target)] = state
                if latency is not None:
                    self.receive_samples[agent].append(latency)
                self._metric("ack1", agent=agent, id=target, latency=latency, eta=eta,
                             lease_seconds=max(0, state["lease_until"] - now))
            self._signal(agent)
            return {"ok": True, "leases": [{"id": mid, "remaining_seconds": state["lease_until"] - now}
                                            for mid, state, _ in states],
                    "last_acked_id": str(self.acked[agent])}

    def _overdue(self, agent, mid):
        state = self.delivery.get((agent, mid))
        if not state:
            return False
        if state["received"]:
            return self.clock() > state["lease_until"]
        return self.clock() - state["delivered_at"] > self.receive_deadline(agent)

    def _completion_metric(self, agent, mid, event):
        self.last_progress_at[agent] = self.clock()
        state = self.delivery.get((agent, mid))
        self._metric(event, agent=agent, id=mid,
                     latency=self.clock() - state["first_at"] if state else None)

    def start_maintenance(self):
        if self.sweeper_task is None:
            self.sweeper_task = self.spawn(self._sweeper())

    async def _sweeper(self):
        while not self.closed:
            await asyncio.sleep(self.sweeper_interval)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("lifecycle sweep failed (%s); will retry", type(exc).__name__)

    async def sweep(self):
        if self.recovering:
            return
        now = self.clock()
        for agent in self.config.agents:
            for mid in list(self.queues[agent]):
                if not self._overdue(agent, mid):
                    continue
                state = self.delivery[(agent, mid)]
                if state["redelivered"] >= 3:
                    self.journal.append({"type": "suspect", "agent": agent, "id": mid})
                    self.suspects[agent].add(mid)
                    del self.queues[agent][mid]
                    self._metric("suspect", agent=agent, id=mid, redelivered=3)
                else:
                    state["due"] = True
            self._signal(agent)
        if now >= self.next_ping:
            self.next_ping = now + 60
            await self._health_ping()
        async with self.warning_lock:
            await self._flush_recoveries()
            await self._warn(now)

    async def _health_ping(self):
        url = self.config.healthcheck_ping_url
        if not url or not self.socket_listening or not self.ready.is_set() or not self.transport.healthy():
            return
        def ping():
            with urllib.request.urlopen(url, timeout=5) as response:
                response.read(1)
        try:
            await asyncio.to_thread(ping)
        except Exception as exc:
            log.warning("healthcheck ping failed (%s)", type(exc).__name__)

    def _save_warnings(self):
        for group in self.warning_state:
            group["saved_at"] = self.clock()
        self.journal.append({"type": "warning", "groups": self.warning_state})

    def _warning_text(self, group):
        lines = []
        for agent, member in group["members"].items():
            if member["recovered"]:
                lines.append(f"✅ {agent} đã hồi sau {member['minutes']:.1f} phút")
            else:
                lines.append(f"{agent}: {member['reason']}")
        return "Bridge status\n" + "\n".join(lines)

    async def _post_warning(self, text, edit_id=None, mention=False):
        # A transport operation has explicit mention permission only at tier 2.
        return str(await asyncio.wait_for(self.transport.send_warning(text, edit_id, mention), 10))

    async def _warn(self, now):
        active = {a for g in self.warning_state for a, m in g["members"].items() if not m["recovered"]}
        new = {}
        ages = {}
        for agent in self.config.agents:
            pending = set(self.queues[agent]) | self.suspects[agent]
            if not pending:
                continue
            age = max(now - self.queued_at.get((agent, mid), now) for mid in pending)
            ages[agent] = age
            overdue = any(self._overdue(agent, mid) for mid in pending)
            suspect = bool(self.suspects[agent])
            had_ack = any(self.delivery.get((agent, mid), {}).get("had_ack1") for mid in pending)
            reason = "tin nghi ngờ" if suspect else "nhận rồi im" if had_ack else "chưa nhận"
            if agent in active:
                for group in self.warning_state:
                    member = group["members"].get(agent)
                    if member and not member["recovered"] and member["reason"] != reason:
                        member["reason"] = reason
                        group["needs_edit"] = True
                        self._save_warnings()
                continue
            progress = self.last_progress_at[agent]
            if progress is not None and now - progress <= 300:
                continue
            if not (suspect or (age > 300 and (not self.alive(agent) or overdue))):
                continue
            new[agent] = {"reason": reason, "started": now - max(0, age),
                          "elapsed_before": 0, "recovered": False, "minutes": 0}
        if new:
            group = next((g for g in reversed(self.warning_state)
                          if any(not m["recovered"] for m in g["members"].values())), None)
            if group is None:
                group = {"message_id": None, "tier2_id": None, "members": {}}
                self.warning_state.append(group)
            group["members"].update(new)
            group["needs_edit"] = True
            self._save_warnings()
        for group in self.warning_state:
            members = {a: m for a, m in group["members"].items() if not m["recovered"]}
            if not members and not group.get("needs_edit"):
                continue
            if group["message_id"] is None or new.keys() & members.keys() or group.get("needs_edit"):
                text = self._warning_text(group)
                mid = await self._post_warning(text, group["message_id"])
                first = group["message_id"] is None
                group["message_id"] = mid
                group["needs_edit"] = False
                self._save_warnings()
                if first or new.keys() & members.keys():
                    self._metric("warning1", agents=list(members), message_id=mid,
                                 detection_seconds={a: ages.get(a) for a in members},
                                 cause="", false_positive=None)
            if members and group["tier2_id"] is None and any(ages.get(a, 0) >= 1800 for a in members):
                text = f"<@{self.config.owner_id}> Bridge escalation: " + ", ".join(members)
                group["tier2_id"] = await self._post_warning(text, mention=True)
                self._save_warnings()
                self._metric("warning2", agents=list(members), message_id=group["tier2_id"])

    async def _recover_warning(self, agent):
        async with self.warning_lock:
            for group in self.warning_state:
                member = group["members"].get(agent)
                if not member or member["recovered"]:
                    continue
                member.update(recovered=True, minutes=(member.get("elapsed_before", 0) + max(0, self.clock() - member["started"])) / 60)
                group["needs_edit"] = True
                if group["tier2_id"] is not None:
                    group.setdefault("recovery_pending", []).append(agent)
                self._save_warnings()
                self._metric("recovery", agent=agent, duration_seconds=member["minutes"] * 60,
                             message_id=group["message_id"], cause="", false_positive=None)
            # Posting failure must not undo a durable ack. Sweep retries edits.
            try:
                await self._flush_recoveries()
            except Exception as exc:
                log.warning("recovery notification failed (%s); will retry", type(exc).__name__)

    async def _flush_recoveries(self):
        for group in self.warning_state:
            if group.get("needs_edit") and group["message_id"]:
                await self._post_warning(self._warning_text(group), group["message_id"])
                group["needs_edit"] = False
                self._save_warnings()
            if group.get("recovery_pending"):
                await self._post_warning("Bridge recovered: " + ", ".join(group["recovery_pending"]))
                group["recovery_pending"] = []
                self._save_warnings()
