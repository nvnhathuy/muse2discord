"""Routing gate: hub-level pause + call patterns from the live registry
(spec 8.1 items 1+3).

The hub core's Filter builds one regex per agent from its call tokens
and routes on mention / broadcast / pattern. This gate mirrors that
construction from the SHARED registry and adds the enabled check, so a
pause applied on the admin page excludes the agent from routing on the
very next message - no restart, no container rebuild. The bot itself
stays connected (spec 4.4: pause is a routing stop, not a kick).

Wiring (PATCH-POINTS.md): Filter.route() consults gate.is_enabled(key)
for every candidate target, and the hub rebuilds the gate's patterns
whenever the watcher reports profile/enabled changes.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, Optional

from .registry_sync import HubAgent


class RoutingGate:
    def __init__(self, agents: Optional[Dict[str, HubAgent]] = None):
        self._agents: Dict[str, HubAgent] = {}
        self._patterns: Dict[str, Optional[re.Pattern]] = {}
        self.refresh(agents or {})

    def refresh(self, agents: Dict[str, HubAgent]) -> None:
        """Swap in a new registry snapshot (hot)."""
        self._agents = dict(agents)
        self._patterns = {}
        for key, agent in self._agents.items():
            if agent.call_tokens:
                joined = "|".join(
                    re.escape(unicodedata.normalize("NFC", t)) for t in agent.call_tokens
                )
                self._patterns[key] = re.compile(r'"(?:' + joined + r")(?!\w)", re.I)
            else:
                self._patterns[key] = None

    # ------------------------------------------------------------------
    def is_enabled(self, key: str) -> bool:
        agent = self._agents.get(key)
        return bool(agent and agent.enabled)

    def known(self, key: str) -> bool:
        return key in self._agents

    def bot_ids(self) -> Dict[int, str]:
        return {a.bot_id: k for k, a in self._agents.items() if a.bot_id}

    def route(self, *, content: str, mentions: Iterable[int] = (), broadcast: bool = False) -> list:
        """Mirror of Filter.route's per-agent decision, minus the peer
        circuit breaker (that counter lives in the core Filter and is
        applied after this gate). Returns enabled target keys."""
        text = unicodedata.normalize("NFC", content)
        by_id = self.bot_ids()
        targets = []
        for key, agent in self._agents.items():
            if not agent.enabled:
                continue  # hub-level pause: effective immediately
            pattern = self._patterns[key]
            if broadcast or agent.bot_id in mentions or (pattern and pattern.search(text)):
                targets.append(key)
        return targets
