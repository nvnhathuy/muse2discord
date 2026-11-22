"""Live hub status for the admin page (spec 8.2 + 8.3).

Replaces the admin package's reference DeliveryState when running
integrated. Two surfaces:

- HubSnapshot.status() -> the dict hub_admin's status_provider expects:
  {ready, recovering, journal_healthy, online_bots, expected_bots},
  read from the REAL hub object (ready event, recovering flag, journal
  health) and the runtime's online set.

- DeliveryFacade -> the DeliveryState interface the admin API calls
  (probe / queue_for / cursor_of / force_release / ack_on_behalf /
  reset_cursor), served from the core's actual structures:
    queues[agent]   {message_id: Message}
    delivery[(agent, id)]  {lease_until (monotonic), redelivered, due,
                            received, first_at, delivered_at, ...}
    acked[agent]    cursor (int snowflake)
  force_release mirrors the core's own redelivery trigger (mark the
  delivery due, drop its lease). ack / cursor-reset have no safe
  direct-manipulation equivalent in the core (ack is a journaled async
  op; the cursor only moves forward via _low_water), so they go
  through injected ops callables - PATCH-POINTS.md specifies the two
  small core methods (Hub.admin_ack_on_behalf, Hub.admin_reset_cursor)
  that production injects here.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional


def _message_epoch(message) -> Optional[float]:
    ts = getattr(message, "timestamp", "") or ""
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


class HubSnapshot:
    def __init__(self, hub, online_provider: Callable[[], List[str]],
                 expected_provider: Callable[[], List[str]]):
        self.hub = hub
        self._online = online_provider
        self._expected = expected_provider

    def status(self) -> dict:
        hub = self.hub
        return {
            "ready": bool(hub.ready.is_set()) if hasattr(hub, "ready") else False,
            "recovering": bool(getattr(hub, "recovering", False)),
            "journal_healthy": not bool(getattr(getattr(hub, "journal", None), "failed", False)),
            "online_bots": sorted(self._online()),
            "expected_bots": sorted(self._expected()),
        }


class DeliveryFacade:
    def __init__(self, hub, *, ack_op: Optional[Callable[[str, str], bool]] = None,
                 reset_cursor_op: Optional[Callable[[str, str], str]] = None):
        self.hub = hub
        self._ack_op = ack_op
        self._reset_cursor_op = reset_cursor_op

    # ------------------------------------------------------------------
    def _now(self) -> float:
        clock = getattr(self.hub, "clock", None)
        return clock() if callable(clock) else time.monotonic()

    def probe(self, agent_key: str) -> dict:
        queue = self.hub.queues.get(agent_key, {})
        now = self._now()
        leases = 0
        for (agent, _mid), state in self.hub.delivery.items():
            if agent == agent_key and state.get("lease_until") and state["lease_until"] > now:
                leases += 1
        return {"pending": len(queue), "leases": leases}

    def queue_for(self, agent_key: str) -> List[dict]:
        now = self._now()
        out = []
        for mid, message in self.hub.queues.get(agent_key, {}).items():
            state = self.hub.delivery.get((agent_key, mid), {})
            lease_until = state.get("lease_until")
            leased = bool(lease_until and lease_until > now)
            out.append({
                "message_id": mid,
                "author": getattr(message, "author_name", ""),
                "ts": _message_epoch(message) or 0.0,
                "deliveries": int(state.get("redelivered", 0)) + (1 if state else 0),
                "leased": leased,
                "lease_seconds_left": int(lease_until - now) if leased else 0,
                "received": bool(state.get("received")),
            })
        return sorted(out, key=lambda r: int(r["message_id"]))

    def cursor_of(self, agent_key: str) -> str:
        return str(self.hub.acked.get(agent_key, 0))

    # ------------------------------------------------------------------
    def force_release(self, agent_key: str, message_id: str) -> dict:
        """Drop a stuck lease so the hub redelivers: exactly what the
        core's sweeper does when a lease expires (mark due)."""
        state = self.hub.delivery.get((agent_key, message_id))
        if state is None or message_id not in self.hub.queues.get(agent_key, {}):
            raise KeyError(f"no pending message {message_id!r} for agent {agent_key!r}")
        state["lease_until"] = None
        state["due"] = True
        state["received"] = False
        state["work_started"] = None
        return {"message_id": message_id, "released": True}

    def ack_on_behalf(self, agent_key: str, message_id: str) -> bool:
        if self._ack_op is None:
            raise RuntimeError("ack_on_behalf needs the core Hub.admin_ack_on_behalf op (see PATCH-POINTS.md)")
        return self._ack_op(agent_key, message_id)

    def reset_cursor(self, agent_key: str, to_message_id: str = "0") -> str:
        if self._reset_cursor_op is None:
            raise RuntimeError("reset_cursor needs the core Hub.admin_reset_cursor op (see PATCH-POINTS.md)")
        return self._reset_cursor_op(agent_key, to_message_id)
