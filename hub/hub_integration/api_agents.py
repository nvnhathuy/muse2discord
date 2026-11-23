"""API-agent configuration, hub side (issue #22, contract 1.2).

Read-only mirror of the admin page's store: the SAME files
(``api_agents.json`` + ``api-keys/<key>.key`` in the registry dir,
same JSON format) read by the hub process that executes API agents.
The format is the contract between the two sides; this reader
reloads whenever the file's mtime changes, so an edit on the admin
page takes effect on the next execution without a restart.

The provider key is read only at execution time and held only in
memory for the duration of the provider call - it is never logged.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ApiAgentsStore:
    """Read side of the API-agent configuration (hub process)."""

    def __init__(self, registry_dir: str):
        self.registry_dir = registry_dir
        self.path = os.path.join(registry_dir, "api_agents.json")
        self.keys_dir = os.path.join(registry_dir, "api-keys")
        self._mtime: Optional[float] = None
        self._agents: Dict[str, dict] = {}
        self._reload_if_changed(force=True)

    def _reload_if_changed(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        agents: Dict[str, dict] = {}
        if mtime is not None:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                for key, cfg in (raw.get("agents") or {}).items():
                    if isinstance(cfg, dict):
                        agents[key] = {
                            "provider": cfg.get("provider", ""),
                            "models": list(cfg.get("models") or []),
                            "base_url": cfg.get("base_url") or None,
                            "updated_at": cfg.get("updated_at"),
                        }
            except (OSError, ValueError):
                agents = {}
        self._agents = agents
        self._mtime = mtime

    def get_config(self, key: str) -> Optional[dict]:
        self._reload_if_changed()
        cfg = self._agents.get(key)
        return dict(cfg) if cfg else None

    def read_key(self, key: str) -> Optional[str]:
        """The provider key for one agent, or None. Internal use by
        the executor only - the value must never reach a log line, an
        audit record or a response."""
        if not _KEY_RE.match(key or ""):
            return None
        try:
            with open(os.path.join(self.keys_dir, key + ".key"),
                      "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            return value or None
        except OSError:
            return None
