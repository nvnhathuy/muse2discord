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
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Deaf-agent detection + escalation + redelivery cadence (issue: hub-side
# detection layer). Design constants, deliberately NOT env vars in v1:
# these are approved design decisions, not operational tuning knobs.
DEAF_AFTER = 300          # seconds without a wait before an agent counts as DEAF
DEAF_CHECK_INTERVAL = 60  # hub cadence for evaluating the deaf state
ESCALATE_AFTER = 900      # seconds spent DEAF before escalation
REDELIVERY_SCHEDULE = (60, 300, 900)  # redelivery at minutes 1, 5, 15 from first_at


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
        # Explicit per-agent liveness state (deaf/idle detection). In-memory
        # only, never journaled: it is fully derived from last_wait_at plus
        # the queues, both of which are rebuilt after a restart.
        self.deaf_state = {a: {"state": "unknown", "deaf_since": None,
                               "escalated": False, "escalated_at": None} for a in keys}
        self.next_deaf_check = self.started_at + DEAF_CHECK_INTERVAL
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

    def _wall_hhmmss(self, monotonic_at):
        """Render a monotonic timestamp as HH:MM:SS wall time in the room's
        zone (+07). Monotonic values are converted through the offset
        between time.time() and the hub clock at this moment."""
        wall = time.time() - (self.clock() - monotonic_at)
        return datetime.fromtimestamp(wall, tz=timezone(timedelta(hours=7))).strftime("%H:%M:%S")

    def _wall_iso_utc(self, monotonic_at):
        wall = time.time() - (self.clock() - monotonic_at)
        return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()

    def _deaf_check(self, now):
        """Evaluate the explicit per-agent liveness state.

        alive:  a wait within the last 100s (matches alive()).
        stale:  100-300s without a wait - buffer band, no action.
        deaf:   >300s without a wait AND un-acked messages pending.
        idle:   >300s without a wait, nothing pending - log only,
                never warned, never escalated.
        unknown: never waited since hub start. For the first DEAF_AFTER
                seconds after hub start nothing is concluded (a healthy
                agent needs time to reconnect); after that the agent is
                evaluated normally, with deaf_since = started_at + DEAF_AFTER.

        Logs/metrics are emitted only on state transitions, never on
        every evaluation - no 60s spam.
        """
        for agent in self.config.agents:
            entry = self.deaf_state.setdefault(
                agent, {"state": "unknown", "deaf_since": None,
                        "escalated": False, "escalated_at": None})
            last = self.last_wait_at.get(agent)
            if last is None and now - self.started_at <= DEAF_AFTER:
                continue  # post-restart grace: conclude nothing yet
            pending = len(self.queues.get(agent, ()))
            if last is None:
                gap = now - self.started_at
                deaf_since = self.started_at + DEAF_AFTER
            else:
                gap = now - last
                deaf_since = last + DEAF_AFTER
            if gap <= 100:
                new_state = "alive"
            elif gap <= DEAF_AFTER:
                new_state = "stale"
            elif pending:
                new_state = "deaf"
            else:
                new_state = "idle"
            old_state = entry["state"]
            if new_state == old_state:
                continue
            if new_state == "deaf":
                if entry.get("deaf_since") != deaf_since:
                    # A genuinely new deaf episode; a continuing one (same
                    # deaf_since after an idle dip) keeps its escalation flag.
                    entry["escalated"] = False
                    entry["escalated_at"] = None
                entry["deaf_since"] = deaf_since
                log.warning("agent %s is DEAF: no wait for %.0fs, %d pending",
                            agent, gap, pending)
                self._metric("deaf", agent=agent, silent_seconds=gap, pending=pending,
                             deaf_since_wall=self._wall_iso_utc(deaf_since))
            elif new_state == "idle":
                log.info("agent %s idle: no wait for %.0fs, nothing pending", agent, gap)
                self._metric("idle", agent=agent, silent_seconds=gap)
            elif new_state == "alive" and old_state == "deaf":
                silent = now - entry["deaf_since"] if entry.get("deaf_since") is not None else gap
                self._metric("recovered_deaf", agent=agent, silent_seconds=silent)
                entry["deaf_since"] = None
                entry["escalated"] = False
                entry["escalated_at"] = None
            entry["state"] = new_state

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
        ids = {g[key] for g in self.warning_state for key in ("message_id", "tier2_id") if g.get(key)}
        # Escalation messages are tracked per warning member (an agent's own
        # tier2_id), not only per group; include those ids too.
        ids |= {m["tier2_id"] for g in self.warning_state
                for m in g.get("members", {}).values() if m.get("tier2_id")}
        return message.author_id == sender and message.id in ids

    def receive_deadline(self, agent):
        # Observational only since the fixed 1/5/15 redelivery schedule
        # landed (see _next_due_at): the p95 estimate is still measured and
        # restored, but it no longer drives redelivery timing.
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

    def _next_due_at(self, agent, mid):
        """When an unreceived message is next due for redelivery: a fixed
        schedule from first_at (REDELIVERY_SCHEDULE, minutes 1/5/15 by
        redelivery count). None once the schedule is exhausted - the sweep
        then moves the message to suspects. Received messages are governed
        by their working lease instead (handled in _overdue). Derived from
        the existing delivery state; no journal schema change, and the
        post-restart rebase of first_at in _reset_reliability_clocks keeps
        working (the schedule simply restarts from the rebased first_at)."""
        state = self.delivery.get((agent, mid))
        if not state:
            return None
        if state["received"]:
            return state["lease_until"]
        if state["redelivered"] >= len(REDELIVERY_SCHEDULE):
            return None
        return state["first_at"] + REDELIVERY_SCHEDULE[state["redelivered"]]

    def _overdue(self, agent, mid):
        state = self.delivery.get((agent, mid))
        if not state:
            return False
        if state["received"]:
            return self.clock() > state["lease_until"]
        due_at = self._next_due_at(agent, mid)
        if due_at is None:
            return True  # schedule exhausted: overdue so sweep() suspects it
        return self.clock() >= due_at

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
        if now >= self.next_deaf_check:
            self.next_deaf_check = now + DEAF_CHECK_INTERVAL
            self._deaf_check(now)
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

    def _escalation_text(self, agent, deaf_since, pending_ids, now):
        """The single per-agent escalation summary (tier 2). Wall-clock
        times are rendered in the room's zone (+07) from monotonic state."""
        shown = pending_ids[:5]
        ids = ", ".join(shown)
        if len(pending_ids) > len(shown):
            ids += f" +{len(pending_ids) - len(shown)} tin khác"
        last = self.last_wait_at.get(agent)
        last_text = (self._wall_hhmmss(last) if last is not None
                     else "chưa từng gọi wait từ khi hub lên")
        progress = self.last_progress_at.get(agent)
        progress_text = self._wall_hhmmss(progress) if progress is not None else "không có"
        return (f"<@{self.config.owner_id}> Bridge escalation: {agent} ĐIẾC từ "
                f"{self._wall_hhmmss(deaf_since)} (đã {(now - deaf_since) / 60:.1f} phút), "
                f"{len(pending_ids)} tin đang kẹt: {ids}; lần wait cuối {last_text}, "
                f"lần tiến triển cuối {progress_text}.")

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
            entry = self.deaf_state.get(agent) or {}
            is_deaf = entry.get("state") == "deaf"
            last = self.last_wait_at.get(agent)
            # Tier-1 branches: suspect / received-but-silent / explicitly
            # DEAF (was alive, stopped calling wait) / never received.
            if suspect:
                reason = "tin nghi ngờ"
            elif had_ack:
                reason = "nhận rồi im"
            elif is_deaf and last is not None:
                reason = f"điếc: không gọi wait từ {self._wall_hhmmss(last)}"
            else:
                reason = "chưa nhận"
            if agent in active:
                for group in self.warning_state:
                    member = group["members"].get(agent)
                    if member and not member["recovered"]:
                        if member["reason"] != reason:
                            member["reason"] = reason
                            group["needs_edit"] = True
                            self._save_warnings()
                        if member.get("deaf_since") is None and is_deaf:
                            member["deaf_since"] = entry.get("deaf_since")
                            self._save_warnings()
                continue
            progress = self.last_progress_at[agent]
            if progress is not None and now - progress <= 300:
                continue
            if not (suspect or (age > 300 and (not self.alive(agent) or overdue))):
                continue
            deaf_branch = is_deaf and last is not None
            new[agent] = {"reason": reason,
                          "started": entry["deaf_since"] if deaf_branch else now - max(0, age),
                          "elapsed_before": 0, "recovered": False, "minutes": 0,
                          "deaf_since": entry.get("deaf_since") if is_deaf else None,
                          "escalated": False, "tier2_id": None}
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
            # Escalation is PER AGENT, once per deaf episode: an agent that
            # has been DEAF for ESCALATE_AFTER with messages still pending
            # gets its own summary message. (The old group-level check -
            # one tier2_id per group - meant a second deaf agent in the
            # same group was never escalated at all.)
            for agent, member in members.items():
                if member.get("escalated"):
                    continue
                entry = self.deaf_state.get(agent) or {}
                if entry.get("state") != "deaf":
                    continue
                deaf_since = entry.get("deaf_since")
                if deaf_since is None:
                    deaf_since = member.get("deaf_since")
                if deaf_since is None or now - deaf_since < ESCALATE_AFTER:
                    continue
                pending_ids = sorted(set(self.queues[agent]) | self.suspects[agent], key=int)
                if not pending_ids:
                    continue
                text = self._escalation_text(agent, deaf_since, pending_ids, now)
                mid = await self._post_warning(text, mention=True)
                member["escalated"] = True
                member["tier2_id"] = mid
                if group.get("tier2_id") is None:
                    # Group-level id kept for readers that only know the
                    # legacy shape (e.g. the runtime's own-message filter).
                    group["tier2_id"] = mid
                entry["escalated"] = True
                entry["escalated_at"] = now
                self._save_warnings()
                self._metric("warning2", agents=[agent], agent=agent, message_id=mid)
                self._metric("escalation", agent=agent, deaf_seconds=now - deaf_since,
                             pending=len(pending_ids), pending_ids=pending_ids[:5],
                             message_id=mid)

    async def _recover_warning(self, agent):
        async with self.warning_lock:
            for group in self.warning_state:
                member = group["members"].get(agent)
                if not member or member["recovered"]:
                    continue
                member.update(recovered=True, minutes=(member.get("elapsed_before", 0) + max(0, self.clock() - member["started"])) / 60)
                group["needs_edit"] = True
                if member.get("escalated") and member.get("deaf_since") is not None:
                    member["deaf_minutes"] = (self.clock() - member["deaf_since"]) / 60
                # Recovery is announced when THIS member escalated (legacy
                # journals only have the group-level tier2_id; members
                # restored from them carry no "escalated" key at all).
                if (member.get("escalated") or member.get("tier2_id") is not None
                        or (group["tier2_id"] is not None and "escalated" not in member)):
                    group.setdefault("recovery_pending", []).append(agent)
                # The episode is over: the next deaf episode for this agent
                # may escalate again. tier2_id stays on the member so the
                # recovery post can still reference the escalation.
                member["escalated"] = False
                entry = self.deaf_state.get(agent)
                if entry is not None:
                    entry["escalated"] = False
                    entry["escalated_at"] = None
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
                # _post_warning may replace the message (new ID) when the
                # stored one cannot be edited; keep the ID it actually used.
                group["message_id"] = await self._post_warning(self._warning_text(group), group["message_id"])
                group["needs_edit"] = False
                self._save_warnings()
            if group.get("recovery_pending"):
                parts = []
                for agent in group["recovery_pending"]:
                    deaf_minutes = group["members"].get(agent, {}).get("deaf_minutes")
                    if deaf_minutes is not None:
                        parts.append(f"{agent} (điếc {deaf_minutes:.1f} phút)")
                    else:
                        parts.append(agent)
                await self._post_warning("Bridge recovered: " + ", ".join(parts))
                group["recovery_pending"] = []
                self._save_warnings()
