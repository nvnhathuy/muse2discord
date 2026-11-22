"""Bot-token storage, kept apart from the agent registry (spec 2.4).

Tokens live in their own JSON file with mode 0600. The registry and the
admin API only ever see metadata: configured yes/no, length, updated_at.
`get()` exists for the hub runtime itself (to log bots in); nothing in
the admin surface calls it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict


class SecretStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def set(self, key: str, token: str) -> None:
        if not token:
            raise ValueError("token must not be empty")
        with self._lock:
            self._data[key] = {"token": token, "updated_at": time.time()}
            self._save()

    def get(self, key: str) -> str:
        """Hub runtime only. Never expose through the admin API."""
        with self._lock:
            entry = self._data.get(key)
            return entry["token"] if entry else ""

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._save()

    def meta(self, key: str) -> dict:
        """The only view the registry / admin page ever gets."""
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return {"configured": False, "length": 0, "updated_at": None}
            return {
                "configured": True,
                "length": len(entry["token"]),
                "updated_at": entry["updated_at"],
            }
