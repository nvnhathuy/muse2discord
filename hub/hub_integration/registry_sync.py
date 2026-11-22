"""One shared agent registry for hub core + admin page (spec 8.1).

The admin package's AgentRegistry file (agents.json) plus its
SecretStore (bot_secrets.json) are the SINGLE source of truth. The hub
core reads its agent set from these files; the legacy env config is
only a first-boot bootstrap / fallback:

- no agents.json yet -> seed it from env (AGENT_KEYS + <KEY>_*), exactly
  like hub_admin.app does, and migrate env bot tokens into the secret
  store;
- agents.json exists -> env is ignored for agents (hub-wide settings
  such as guild/channel/owner still come from env).

RegistryWatcher polls the two files (mtime + content hash) and turns
changes into events the hub applies WITHOUT restarting:
  added / removed / token_changed / profile_changed / enabled_changed
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

# The admin package is a sibling deliverable. The path inserts below
# cover the known layouts, in order: the assembled repo layout
# (hub/hub_integration -> ../../admin), the container layout (hub_admin
# copied next to this package's parent), and the original trial/drop
# layout (admin_changes/).
for _candidate in reversed((
    os.path.join(os.path.dirname(__file__), "..", "..", "admin"),
    os.path.join(os.path.dirname(__file__), "..", "admin_changes"),
    os.path.join(os.path.dirname(__file__), "..", "..", "trang-quan-tri-hub"),
    os.path.join(os.path.dirname(__file__), ".."),
)):
    sys.path.insert(0, os.path.abspath(_candidate))

from hub_admin.registry import AgentRegistry  # noqa: E402
from hub_admin.secrets_store import SecretStore  # noqa: E402


@dataclass(frozen=True)
class HubAgent:
    """The hub-core-shaped view of one registry profile."""
    key: str
    bot_id: int
    token: str
    display_name: str
    call_tokens: tuple
    receive_deadline: Optional[float]
    enabled: bool
    seat_type: str
    # Execution model (issue #22): "api" agents are executed by the
    # hub itself; everything else is an SSH agent with its own runner.
    kind: str = "ssh"


@dataclass(frozen=True)
class RegistryEvent:
    kind: str  # added | removed | token_changed | profile_changed | enabled_changed
    key: str
    enabled: Optional[bool] = None


def bootstrap_state(state_dir: str, env: Dict[str, str]) -> tuple[AgentRegistry, SecretStore]:
    """Open (and if needed seed) the shared registry under state_dir."""
    os.makedirs(state_dir, exist_ok=True)
    secrets = SecretStore(os.path.join(state_dir, "bot_secrets.json"))
    registry = AgentRegistry(os.path.join(state_dir, "agents.json"), secret_meta=secrets.meta)
    if not registry.list_agents():
        registry.seed_from_env(env)
        for profile in registry.list_agents():
            token = env.get(f"{profile.key.upper()}_BOT_TOKEN", "")
            if token and not secrets.meta(profile.key)["configured"]:
                secrets.set(profile.key, token)
    return registry, secrets


class RegistryView:
    """Point-in-time, hub-shaped snapshot of the shared registry."""

    def __init__(self, registry: AgentRegistry, secrets: SecretStore):
        self._registry = registry
        self._secrets = secrets

    def reload(self) -> None:
        # AgentRegistry keeps state in memory; rebuild it from disk so
        # edits made by the admin process become visible here.
        self._registry = AgentRegistry(self._registry.state_path, secret_meta=self._secrets.meta)
        self._secrets = SecretStore(self._secrets.path)

    def agents(self) -> Dict[str, HubAgent]:
        out: Dict[str, HubAgent] = {}
        for p in self._registry.list_agents():
            # Profiles written before the kind field existed carry no
            # kind attribute value beyond the registry's derivation;
            # fall back to the seat axis so this side works either way.
            kind = getattr(p, "kind", None) or (
                "api" if p.seat_type == "api" else "ssh")
            out[p.key] = HubAgent(
                key=p.key,
                bot_id=int(p.bot_id) if str(p.bot_id).isdigit() else 0,
                token=self._secrets.get(p.key),
                display_name=p.display_name,
                call_tokens=tuple(p.call_tokens),
                receive_deadline=float(p.receive_deadline) if p.receive_deadline else None,
                enabled=p.enabled,
                seat_type=p.seat_type,
                kind=kind,
            )
        return out

    def fingerprint(self) -> Dict[str, str]:
        """Per-agent content fingerprint for change detection. The token
        enters only as a hash - fingerprints are safe to log."""
        fp = {}
        for key, agent in self.agents().items():
            material = json.dumps({
                "bot_id": agent.bot_id,
                "display_name": agent.display_name,
                "call_tokens": list(agent.call_tokens),
                "receive_deadline": agent.receive_deadline,
                "enabled": agent.enabled,
                "seat_type": agent.seat_type,
                "kind": agent.kind,
                "token_hash": hashlib.sha256(agent.token.encode()).hexdigest() if agent.token else "",
            }, sort_keys=True)
            fp[key] = hashlib.sha256(material.encode()).hexdigest()
        return fp


class RegistryWatcher:
    """Polls the shared registry files and diffs successive snapshots."""

    def __init__(self, view: RegistryView):
        self.view = view
        self._agents: Dict[str, HubAgent] = view.agents()
        self._fp: Dict[str, str] = view.fingerprint()

    @property
    def agents(self) -> Dict[str, HubAgent]:
        return dict(self._agents)

    def poll(self) -> List[RegistryEvent]:
        self.view.reload()
        new_agents = self.view.agents()
        new_fp = self.view.fingerprint()
        events: List[RegistryEvent] = []
        for key in sorted(set(new_agents) - set(self._agents)):
            events.append(RegistryEvent("added", key, enabled=new_agents[key].enabled))
        for key in sorted(set(self._agents) - set(new_agents)):
            events.append(RegistryEvent("removed", key))
        for key in sorted(set(new_agents) & set(self._agents)):
            if new_fp.get(key) == self._fp.get(key):
                continue
            old, new = self._agents[key], new_agents[key]
            if old.token != new.token:
                events.append(RegistryEvent("token_changed", key))
            if old.enabled != new.enabled:
                events.append(RegistryEvent("enabled_changed", key, enabled=new.enabled))
            if (old.bot_id, old.display_name, old.call_tokens, old.receive_deadline, old.seat_type, old.kind) != \
               (new.bot_id, new.display_name, new.call_tokens, new.receive_deadline, new.seat_type, new.kind):
                events.append(RegistryEvent("profile_changed", key))
        self._agents, self._fp = new_agents, new_fp
        return events
