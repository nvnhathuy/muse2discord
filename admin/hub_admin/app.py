"""Entrypoint: python3 -m hub_admin.app

Environment:
  HUB_ADMIN_STATE_DIR  where state files live (default ./state)
  HUB_ADMIN_HOST       bind address (default 127.0.0.1; bind the tailnet
                       address at deployment - the page is tailnet-only)
  HUB_ADMIN_PORT       default 8899 (the page's OWN address, spec 5)
  HUB_ADMIN_TOKEN      admin principal token (or admin_token.txt in the
                       state dir, mode 0600)
  HUB_JOURNAL_PATH     hub journal JSONL (default <state>/bridge.jsonl)

On first boot the registry seeds itself from the legacy env-style
agent configuration (AGENT_KEYS + <KEY>_* in the process environment)
if such variables are present; afterwards the state file is the truth.
"""

from __future__ import annotations

import os

from .api import AdminApp, serve
from .audit import AuditLog
from .auth import AdminAuth
from .delivery import DeliveryState
from .grants import GrantStore
from .ops_config import OpsConfig
from .registry import AgentRegistry
from .secrets_store import SecretStore


def build_app(state_dir: str) -> AdminApp:
    os.makedirs(state_dir, exist_ok=True)
    secrets_store = SecretStore(os.path.join(state_dir, "bot_secrets.json"))
    registry = AgentRegistry(
        os.path.join(state_dir, "agents.json"), secret_meta=secrets_store.meta
    )
    registry.seed_from_env(os.environ)
    # Legacy env tokens migrate into the secret store once, so the page
    # can report "configured" truthfully from day one.
    for profile in registry.list_agents():
        env_token = os.environ.get(f"{profile.key.upper()}_BOT_TOKEN", "")
        if env_token and not secrets_store.meta(profile.key)["configured"]:
            secrets_store.set(profile.key, env_token)
    ops = OpsConfig(os.path.join(state_dir, "ops_config.json"))
    auth = AdminAuth(
        AdminAuth.load_token(file_path=os.path.join(state_dir, "admin_token.txt")),
        idle_seconds=int(ops.get("admin_session_idle_seconds")),
    )
    return AdminApp(
        registry=registry,
        secrets_store=secrets_store,
        grants=GrantStore(os.path.join(state_dir, "grants.json")),
        audit=AuditLog(os.path.join(state_dir, "admin_audit.jsonl")),
        ops=ops,
        delivery=DeliveryState(
            lease_base=int(ops.get("lease_base_seconds")),
            lease_max=int(ops.get("lease_max_seconds")),
        ),
        auth=auth,
        journal_path=os.environ.get(
            "HUB_JOURNAL_PATH", os.path.join(state_dir, "bridge.jsonl")
        ),
    )


def main() -> None:
    state_dir = os.environ.get("HUB_ADMIN_STATE_DIR", "./state")
    host = os.environ.get("HUB_ADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("HUB_ADMIN_PORT", "8899"))
    server = serve(build_app(state_dir), host, port)
    print(f"hub admin page on http://{host}:{port}/ (state: {state_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
