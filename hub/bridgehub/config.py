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

    @classmethod
    def from_env(cls):
        def positive(name):
            value = int(os.environ[name])
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

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
        owner = positive("OWNER_ID")
        ids = [a.bot_id for a in agents.values()]
        if len(set(ids + [owner])) != 4:
            raise ValueError("Owner and three bot IDs must be distinct")
        count = int(os.getenv("CONTEXT_COUNT", "5"))
        if not 0 <= count <= 50:
            raise ValueError("CONTEXT_COUNT must be between 0 and 50")
        deadlines = {a: float(os.getenv(f"{a.upper()}_RECEIVE_DEADLINE", "300" if a == "worker" else "180"))
                     for a in AGENTS}
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
                   external == "true", os.getenv("BRIDGE_EXTERNAL_ALERT_URL", ""), hub_id, hub_token)
