"""Bot lifecycle: apply registry events to Discord bot connections.

The production runtime (bridgehub/discord_runtime.py) supervises one
discord.py client per agent and already replaces a client when its
gateway fails. This manager is the registry-driven counterpart:

  added           -> connect(key, token)
  removed         -> disconnect(key)
  token_changed   -> disconnect(key) then connect(key, new_token)
                     (the bot reconnects with the new token - spec 4.3)
  enabled_changed -> NOTHING: pause is routing-level, the bot stays
                     online in the room (spec 4.4)
  profile_changed -> NOTHING: call tokens / deadlines are read by the
                     routing gate and the deadline logic, not by the
                     gateway connection

The connector is injected: production passes an adapter over
DiscordRuntime (cancel + recreate that agent's supervise task); the
trial passes a recording fake. Calls are serialized; a failing
connect/disconnect is reported in the result list, never raised into
the hub loop.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Protocol

from .registry_sync import HubAgent, RegistryEvent


class Connector(Protocol):
    async def connect(self, key: str, token: str) -> None: ...
    async def disconnect(self, key: str) -> None: ...


class BotLifecycle:
    def __init__(self, connector: Connector):
        self._connector = connector
        self._lock = asyncio.Lock()
        self.connected: Dict[str, str] = {}  # key -> token currently in use (hashed in logs only)

    async def start_all(self, agents: Dict[str, HubAgent]) -> list:
        """Connect every registered agent's bot (enabled or paused -
        pause does not disconnect). Returns per-agent outcomes."""
        results = []
        for key in sorted(agents):
            results.append(await self._connect(key, agents[key].token))
        return results

    async def apply(self, events: List[RegistryEvent], agents: Dict[str, HubAgent]) -> list:
        results = []
        async with self._lock:
            for event in events:
                if event.kind == "added":
                    agent = agents.get(event.key)
                    if agent:
                        results.append(await self._connect(event.key, agent.token))
                elif event.kind == "removed":
                    results.append(await self._disconnect(event.key))
                elif event.kind == "token_changed":
                    agent = agents.get(event.key)
                    if agent:
                        results.append(await self._disconnect(event.key))
                        results.append(await self._connect(event.key, agent.token))
                # enabled_changed / profile_changed: no gateway action
        return results

    async def _connect(self, key: str, token: str) -> dict:
        try:
            await self._connector.connect(key, token)
            self.connected[key] = token
            return {"agent": key, "action": "connect", "ok": True}
        except Exception as exc:  # noqa: BLE001 - report, never crash the hub loop
            return {"agent": key, "action": "connect", "ok": False, "error": type(exc).__name__}

    async def _disconnect(self, key: str) -> dict:
        try:
            await self._connector.disconnect(key)
            self.connected.pop(key, None)
            return {"agent": key, "action": "disconnect", "ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"agent": key, "action": "disconnect", "ok": False, "error": type(exc).__name__}
