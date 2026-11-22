"""Per-agent delivery state: pending queues, leases, cursors (spec 4.6).

This mirrors the live hub's delivery semantics (per-agent cursor,
un-acked redelivery with a delivery counter, leases that expire) so the
admin page can show and operate queues. In production the hub feeds
its real state through the same interface; this in-memory model is the
reference implementation and the one the tests exercise.

Semantics kept from the live hub:
- a message is pending for an agent until acked;
- `working` claims a lease (+lease_base each call, capped at lease_max);
- an expired lease makes the message deliverable again and bumps its
  delivery counter;
- reset_cursor rewinds the agent's cursor so old messages redeliver
  (dangerous: duplicates - sensitive operation, spec 6).
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional


class DeliveryState:
    def __init__(self, lease_base: int = 300, lease_max: int = 1800):
        self.lease_base = lease_base
        self.lease_max = lease_max
        self._lock = threading.RLock()
        # agent_key -> {message_id: record}
        self._pending: Dict[str, Dict[str, dict]] = {}
        # agent_key -> last acked message id (opaque string, hub-defined)
        self._cursors: Dict[str, str] = {}

    # ------------------------------------------------------------------
    def enqueue(self, agent_key: str, message_id: str, author: str = "", ts: Optional[float] = None) -> None:
        with self._lock:
            queue = self._pending.setdefault(agent_key, {})
            if message_id not in queue:
                queue[message_id] = {
                    "message_id": message_id,
                    "author": author,
                    "ts": ts if ts is not None else time.time(),
                    "deliveries": 0,
                    "lease_expires_at": None,
                }

    def claim_lease(self, agent_key: str, message_id: str) -> dict:
        """Equivalent of the bridge `working` command."""
        with self._lock:
            record = self._record(agent_key, message_id)
            now = time.time()
            current = record["lease_expires_at"] or now
            record["lease_expires_at"] = min(current + self.lease_base, now + self.lease_max)
            record["deliveries"] += 1
            return dict(record)

    def ack(self, agent_key: str, message_id: str) -> bool:
        with self._lock:
            queue = self._pending.get(agent_key, {})
            if message_id in queue:
                del queue[message_id]
                self._cursors[agent_key] = message_id
                return True
            return False

    def force_release(self, agent_key: str, message_id: str) -> dict:
        """Drop a stuck lease so the hub can redeliver (spec 4.6)."""
        with self._lock:
            record = self._record(agent_key, message_id)
            record["lease_expires_at"] = None
            return dict(record)

    def ack_on_behalf(self, agent_key: str, message_id: str) -> bool:
        """Admin acks a message for a dead agent (sensitive, spec 6)."""
        return self.ack(agent_key, message_id)

    def reset_cursor(self, agent_key: str, to_message_id: str = "0") -> str:
        """Rewind the cursor (sensitive, spec 6: duplicates will flow)."""
        with self._lock:
            previous = self._cursors.get(agent_key, "0")
            self._cursors[agent_key] = to_message_id
            return previous

    # ------------------------------------------------------------------
    def _record(self, agent_key: str, message_id: str) -> dict:
        queue = self._pending.get(agent_key, {})
        if message_id not in queue:
            raise KeyError(f"no pending message {message_id!r} for agent {agent_key!r}")
        return queue[message_id]

    def queue_for(self, agent_key: str) -> List[dict]:
        with self._lock:
            now = time.time()
            out = []
            for record in self._pending.get(agent_key, {}).values():
                item = dict(record)
                lease = record["lease_expires_at"]
                item["leased"] = bool(lease and lease > now)
                item["lease_seconds_left"] = int(lease - now) if lease and lease > now else 0
                out.append(item)
            return sorted(out, key=lambda r: r["ts"])

    def probe(self, agent_key: str) -> dict:
        """Counts used by the registry's hard-delete guard."""
        with self._lock:
            now = time.time()
            queue = self._pending.get(agent_key, {})
            leases = sum(1 for r in queue.values() if r["lease_expires_at"] and r["lease_expires_at"] > now)
            return {"pending": len(queue), "leases": leases}

    def cursor_of(self, agent_key: str) -> str:
        with self._lock:
            return self._cursors.get(agent_key, "0")
