"""API-agent configuration store (issue #22, contract section 1.2).

Agents with kind="api" are executed by the hub itself from a provider
API key - they have no machine, no shell and no SSH key. Their config
lives next to the other registry state:

- ``api_agents.json`` in the registry dir (mode 0600, atomic writes):
  ``{"version": 1, "agents": {"<key>": {"provider": ..., "models":
  [...], "updated_at": ...}}}`` - the shared source of truth the hub
  side reads with its own store implementation of the same format.
- ``api-keys/<agent_key>.key`` (mode 0600): the raw provider key, one
  file per agent. The key NEVER appears in the JSON, in an API
  response, in the audit log or on the page - this module deliberately
  has no method that returns a stored key.

Both sides reload on mtime change, so an edit on the admin page takes
effect on the hub's next execution without a restart.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Dict, List, Optional

PROVIDERS = ("gemini", "openai")
FORMAT_VERSION = 1
# Form defaults (contract 1.3): suggested when an admin picks the
# provider and leaves the model list empty.
DEFAULT_MODELS = {
    "gemini": ["gemini-3.5-flash", "gemini-3.8-flash",
               "gemini-2.5-flash", "gemini-3.1-flash-lite"],
    "openai": [],
}
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ApiAgentsError(ValueError):
    """Raised for any invalid API-agent configuration operation."""


def _check_key(key: str) -> str:
    key = (key or "").strip().lower()
    if not _KEY_RE.match(key):
        raise ApiAgentsError(f"invalid agent key: {key!r}")
    return key


def validate_provider(provider: str) -> str:
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise ApiAgentsError(f"provider must be one of {PROVIDERS}")
    return provider


def validate_models(models, provider: str) -> List[str]:
    """Normalize the model list; empty falls back to the provider's
    suggested defaults (contract 1.3)."""
    if models is None:
        models = []
    if not isinstance(models, list):
        raise ApiAgentsError("models must be a list of model names")
    out: List[str] = []
    for item in models:
        if not isinstance(item, str):
            raise ApiAgentsError("models must be a list of model names")
        name = item.strip()
        if name and name not in out:
            out.append(name)
    if not out:
        out = list(DEFAULT_MODELS.get(provider, []))
    return out


class ApiAgentsStore:
    """Write side (admin page) of the API-agent configuration."""

    def __init__(self, registry_dir: str):
        self.registry_dir = registry_dir
        self.path = os.path.join(registry_dir, "api_agents.json")
        self.keys_dir = os.path.join(registry_dir, "api-keys")
        self._lock = threading.RLock()
        self._mtime: Optional[float] = None
        self._agents: Dict[str, dict] = {}
        self._reload_if_changed(force=True)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _reload_if_changed(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        agents: Dict[str, dict] = {}
        if mtime is not None:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for key, cfg in (raw.get("agents") or {}).items():
                if isinstance(cfg, dict):
                    agents[key] = {
                        "provider": cfg.get("provider", ""),
                        "models": list(cfg.get("models") or []),
                        "updated_at": cfg.get("updated_at"),
                    }
        self._agents = agents
        self._mtime = mtime

    def _save(self) -> None:
        os.makedirs(self.registry_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        payload = {"version": FORMAT_VERSION,
                   "agents": {k: self._agents[k] for k in sorted(self._agents)}}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)  # atomic: readers never see a half file
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None

    def _key_path(self, key: str) -> str:
        return os.path.join(self.keys_dir, _check_key(key) + ".key")

    # ------------------------------------------------------------------
    # reads (never the key itself)
    # ------------------------------------------------------------------
    def get_config(self, key: str) -> Optional[dict]:
        key = _check_key(key)
        with self._lock:
            self._reload_if_changed()
            cfg = self._agents.get(key)
            return dict(cfg) if cfg else None

    def has_key(self, key: str) -> bool:
        try:
            return os.path.getsize(self._key_path(key)) > 0
        except OSError:
            return False

    def public_block(self, key: str) -> Optional[dict]:
        """The `api` block the admin API attaches to agent rows:
        provider + models + key presence. Never the key."""
        cfg = self.get_config(key)
        if cfg is None:
            return None
        return {
            "provider": cfg["provider"],
            "models": list(cfg["models"]),
            "has_key": self.has_key(key),
        }

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def set_config(self, key: str, provider: str, models) -> dict:
        key = _check_key(key)
        provider = validate_provider(provider)
        models = validate_models(models, provider)
        with self._lock:
            self._reload_if_changed()
            cfg = {"provider": provider, "models": models,
                   "updated_at": time.time()}
            self._agents[key] = cfg
            self._save()
            return dict(cfg)

    def set_key(self, key: str, raw_key: str) -> None:
        key = _check_key(key)
        value = (raw_key or "").strip()
        if not value:
            raise ApiAgentsError("api_key must not be empty")
        with self._lock:
            os.makedirs(self.keys_dir, exist_ok=True)
            tmp = os.path.join(self.keys_dir, key + ".key.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, value.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)  # in case a stale tmp had looser bits
            os.replace(tmp, self._key_path(key))

    def delete_config(self, key: str) -> bool:
        """Remove the config record AND the key file. Returns True if
        anything was removed."""
        key = _check_key(key)
        removed = False
        with self._lock:
            self._reload_if_changed()
            if key in self._agents:
                del self._agents[key]
                self._save()
                removed = True
            try:
                os.unlink(self._key_path(key))
                removed = True
            except OSError:
                pass
            return removed
