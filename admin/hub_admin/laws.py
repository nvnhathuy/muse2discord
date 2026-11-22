"""Room hard laws ("luật cứng"), stored server-side on the hub.

One JSON file in the registry dir (mode 0600, like the other state
files): {version, updated_at, rules}. `version` is a plain integer
that grows by exactly 1 on EVERY change (add / edit / delete), so an
agent can tell at a glance whether the copy in its own memory is
current. Each rule records who added it: "admin" for the admin
principal, or the id of the temporary grant an agent acted under.

The write side lives in the admin page (admin session or full grant);
the read side is deliberately wider: any authenticated identity may
read, and agents outside the page read the very same file through
`bridge laws` (see hub/bridge) after the room is told the announcement
sentence (see LAWS_ANNOUNCEMENT in the admin page).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import List, Optional

MAX_TEXT = 1000


def _validate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("law text must not be empty")
    if len(text) > MAX_TEXT:
        raise ValueError(f"law text must be at most {MAX_TEXT} characters")
    return text


class LawsStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._version = 0
        self._updated_at: Optional[float] = None
        self._rules: List[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self._version = int(raw.get("version", 0))
        self._updated_at = raw.get("updated_at")
        self._rules = list(raw.get("rules", []))

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        payload = {
            "version": self._version,
            "updated_at": self._updated_at,
            "rules": self._rules,
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)  # same discipline as the other state files
        os.replace(tmp, self.path)

    def _touch(self) -> float:
        now = time.time()
        self._version += 1
        self._updated_at = now
        return now

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "updated_at": self._updated_at,
                "rules": [dict(rule) for rule in self._rules],
            }

    # ------------------------------------------------------------------
    # writes (each bumps the version by exactly 1)
    # ------------------------------------------------------------------
    def add(self, text: str, created_by: str) -> dict:
        text = _validate_text(text)
        with self._lock:
            now = self._touch()
            rule = {
                "id": uuid.uuid4().hex[:12],
                "text": text,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            self._rules.append(rule)
            self._save()
            return dict(rule)

    def update(self, rule_id: str, text: str) -> dict:
        text = _validate_text(text)
        with self._lock:
            for rule in self._rules:
                if rule["id"] == rule_id:
                    rule["text"] = text
                    rule["updated_at"] = self._touch()
                    self._save()
                    return dict(rule)
            raise KeyError(f"unknown law: {rule_id!r}")

    def delete(self, rule_id: str) -> dict:
        with self._lock:
            for index, rule in enumerate(self._rules):
                if rule["id"] == rule_id:
                    removed = self._rules.pop(index)
                    self._touch()
                    self._save()
                    return dict(removed)
            raise KeyError(f"unknown law: {rule_id!r}")
