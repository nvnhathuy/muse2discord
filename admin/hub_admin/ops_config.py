"""Hub operational settings (spec 4.7) + journal retention policy (4.8).

Defaults mirror the values measured on the live hub on 2026-11-22:
lease +5 min per claim with a 30 min ceiling, at most 3 redeliveries
before a room warning, escalation marks at 5 and 30 minutes, circuit
breaker at 8 consecutive peer messages. Changes are validated, take
effect immediately, and are meant to be audit-logged by the caller
(the API layer records old -> new).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict

DEFAULTS: Dict[str, object] = {
    "lease_base_seconds": 300,
    "lease_max_seconds": 1800,
    "redelivery_max": 3,
    "escalation_marks_seconds": [300, 1800],
    "circuit_breaker_peer_messages": 8,
    "journal_online_days": 90,
    "journal_rotate_mb": 50,
    "admin_session_idle_seconds": 1800,
}

_POSITIVE_INT = {
    "lease_base_seconds", "lease_max_seconds", "redelivery_max",
    "circuit_breaker_peer_messages", "journal_online_days",
    "journal_rotate_mb", "admin_session_idle_seconds",
}


class OpsConfig:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._values: Dict[str, object] = dict(DEFAULTS)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            for key in DEFAULTS:
                if key in stored:
                    self._values[key] = stored[key]

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._values, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._values)

    def get(self, key: str):
        with self._lock:
            return self._values[key]

    def update(self, changes: dict) -> dict:
        """Validate and apply; returns {key: (old, new)} for auditing."""
        with self._lock:
            diff = {}
            for key, value in changes.items():
                if key not in DEFAULTS:
                    raise ValueError(f"unknown setting: {key!r}")
                self._validate(key, value)
                if self._values[key] != value:
                    diff[key] = (self._values[key], value)
                    self._values[key] = value
            if self._values["lease_base_seconds"] > self._values["lease_max_seconds"]:
                raise ValueError("lease_base_seconds must be <= lease_max_seconds")
            if diff:
                self._save()
            return diff

    @staticmethod
    def _validate(key: str, value) -> None:
        if key in _POSITIVE_INT:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        elif key == "escalation_marks_seconds":
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(v, int) or v <= 0 for v in value)
                or value != sorted(value)
            ):
                raise ValueError("escalation_marks_seconds must be a sorted list of positive ints")
