"""Append-only admin audit log (spec 2.5, 4.8).

One JSON object per line. The admin API can read and filter the log but
has no endpoint that modifies or deletes entries.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import List, Optional


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def record(
        self,
        actor: str,
        action: str,
        target: str = "",
        detail: Optional[dict] = None,
        grant_id: Optional[str] = None,
        result: str = "ok",
    ) -> dict:
        entry = {
            "ts": time.time(),
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail or {},
            "grant_id": grant_id,
            "result": result,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry

    def query(
        self,
        agent: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 200,
    ) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        out: List[dict] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if agent and entry.get("target") != agent:
                    continue
                if action and entry.get("action") != action:
                    continue
                if since is not None and entry.get("ts", 0) < since:
                    continue
                if until is not None and entry.get("ts", 0) > until:
                    continue
                out.append(entry)
        return out[-limit:]
