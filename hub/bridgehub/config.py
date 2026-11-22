import json
import os
import math
from dataclasses import dataclass, field

AGENTS = ("lead", "worker", "deputy")


@dataclass(frozen=True)
class Agent:
    key: str
    bot_id: int
    token: str
    display_name: str
    call_tokens: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    guild_id: int
    channel_id: int
    owner_id: int
    agents: dict[str, Agent]
    journal_path: str = "/data/bridge.jsonl"
    socket_path: str = "/run/bridge.sock"
    context_count: int = 5
    receive_deadlines: dict[str, float] = field(default_factory=lambda: {"lead": 180., "worker": 300., "deputy": 180.})
    healthcheck_ping_url: str = ""
    metrics_path: str = ""
    external_alerts_enabled: bool = False
    external_alert_url: str = ""
    hub_bot_id: int = 0
    hub_bot_token: str = ""
    # Integrated mode (PATCH-POINTS section 1): when set, the shared
    # registry under this directory is the source of truth for the agent
    # set; env only bootstraps it on first boot. Empty = legacy env-only
    # behaviour, exactly as before.
    registry_dir: str = ""

    @staticmethod
    def _registry_agents(registry_dir):
        """Build the agent set + receive deadlines from the shared registry."""
        from hub_integration.registry_sync import RegistryView, bootstrap_state
        registry, secrets = bootstrap_state(registry_dir, os.environ)
        view = RegistryView(registry, secrets)
        agents, deadlines = {}, {}
        for key, profile in view.agents().items():
            if not profile.token or profile.token.startswith("replace-"):
                raise ValueError(f"agent {key} has no usable bot token in the shared registry")
            if profile.bot_id <= 0:
                raise ValueError(f"agent {key} has no valid bot ID in the shared registry")
            if not profile.call_tokens or any(not t.strip() for t in profile.call_tokens):
                raise ValueError(f"agent {key} has no usable call tokens in the shared registry")
            agents[key] = Agent(key, profile.bot_id, profile.token,
                                profile.display_name, tuple(profile.call_tokens))
            if profile.receive_deadline is not None:
                deadlines[key] = float(profile.receive_deadline)
            else:
                deadlines[key] = float(os.getenv(f"{key.upper()}_RECEIVE_DEADLINE", "180"))
        if not agents:
            raise ValueError("shared registry has no agents; seed it via AGENT_KEYS env on first boot")
        return agents, deadlines

    @classmethod
    def from_env(cls):
        def positive(name):
            value = int(os.environ[name])
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        registry_dir = os.getenv("BRIDGE_REGISTRY_DIR", "").strip()
        if registry_dir:
            agents, deadlines = cls._registry_agents(registry_dir)
        else:
            defaults = {"lead": ("Lead", ["lead"]), "worker": ("Worker", ["worker"]),
                        "deputy": ("Deputy", ["deputy"])}
            agents = {}
            for key, (name, tokens) in defaults.items():
                prefix = key.upper()
                token = os.environ[f"{prefix}_BOT_TOKEN"]
                if not token or token.startswith("replace-"):
                    raise ValueError(f"Set {prefix}_BOT_TOKEN locally")
                calls = json.loads(os.getenv(f"{prefix}_CALL_TOKENS", json.dumps(tokens)))
                if not isinstance(calls, list) or not calls or any(
                        not isinstance(t, str) or not t.strip() for t in calls):
                    raise ValueError(f"{prefix}_CALL_TOKENS must be a nonempty JSON string array")
                agents[key] = Agent(key, positive(f"{prefix}_BOT_ID"), token,
                                    os.getenv(f"{prefix}_DISPLAY_NAME", name), tuple(calls))
            # Legacy env-only branch: the agent set here IS the AGENTS
            # default (built from `defaults` above), so iterating AGENTS
            # cannot leak default keys into the registry path - the
            # registry branch above builds deadlines from the registry.
            deadlines = {a: float(os.getenv(f"{a.upper()}_RECEIVE_DEADLINE", "300" if a == "worker" else "180"))
                         for a in AGENTS}
        owner = positive("OWNER_ID")
        ids = [a.bot_id for a in agents.values()]
        if len(set(ids + [owner])) != len(ids) + 1:
            raise ValueError("Owner and bot IDs must be distinct")
        count = int(os.getenv("CONTEXT_COUNT", "5"))
        if not 0 <= count <= 50:
            raise ValueError("CONTEXT_COUNT must be between 0 and 50")
        if any(not math.isfinite(v) or v < 180 for v in deadlines.values()):
            raise ValueError("receive deadlines must be finite and at least 180 seconds")
        hub_id = int(os.getenv("HUB_BOT_ID", "0"))
        hub_token = os.getenv("HUB_BOT_TOKEN", "")
        if bool(hub_id) != bool(hub_token) or hub_id < 0 or hub_id in ids + [owner] or hub_token.startswith("replace-"):
            raise ValueError("Set a distinct HUB_BOT_ID and HUB_BOT_TOKEN together, or leave both unset")
        external = os.getenv("BRIDGE_EXTERNAL_ALERTS_ENABLED", "false").lower()
        if external not in {"true", "false"}:
            raise ValueError("BRIDGE_EXTERNAL_ALERTS_ENABLED must be true or false")
        return cls(positive("GUILD_ID"), positive("CHANNEL_ID"), owner, agents,
                   os.getenv("BRIDGE_JOURNAL", "/data/bridge.jsonl"),
                   os.getenv("BRIDGE_SOCKET", "/run/bridge.sock"), count, deadlines,
                   os.getenv("HEALTHCHECK_PING_URL", ""), os.getenv("BRIDGE_METRICS", ""),
                   external == "true", os.getenv("BRIDGE_EXTERNAL_ALERT_URL", ""), hub_id, hub_token,
                   registry_dir)
