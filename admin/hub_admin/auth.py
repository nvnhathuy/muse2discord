"""Admin-principal authentication (spec 5).

The admin principal is fully separate from agent principals and bridge
tokens. One long random admin token (env HUB_ADMIN_TOKEN or a 0600
token file) exchanges for a short session; sessions idle-expire
(default 30 minutes, configurable in ops config). Sensitive operations
additionally require a fresh re-authentication: the caller presents the
admin token again in the X-Admin-Reauth header. In production this maps
to the passkey/TOTP step-up; the verifier is a single function so the
step-up method can be swapped without touching the API layer.
"""

from __future__ import annotations

import hmac
import os
import secrets as _secrets
import threading
import time
from typing import Callable, Dict, Optional


class AdminAuth:
    def __init__(
        self,
        admin_token: str,
        idle_seconds: int = 1800,
        step_up_verifier: Optional[Callable[[str], bool]] = None,
    ):
        if not admin_token:
            raise ValueError("an admin token is required")
        self._token = admin_token
        self.idle_seconds = idle_seconds
        self._sessions: Dict[str, float] = {}
        self._lock = threading.Lock()
        # Default step-up: re-presenting the admin token. Replace with a
        # passkey/TOTP check at deployment time.
        self._step_up = step_up_verifier or (lambda presented: self._matches(presented))

    def _matches(self, presented: str) -> bool:
        return bool(presented) and hmac.compare_digest(presented, self._token)

    @staticmethod
    def load_token(env_name: str = "HUB_ADMIN_TOKEN", file_path: Optional[str] = None) -> str:
        token = os.environ.get(env_name, "")
        if token:
            return token.strip()
        if file_path and os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        raise ValueError(f"no admin token in ${env_name} or {file_path}")

    def login(self, presented_token: str) -> Optional[str]:
        if not self._matches(presented_token):
            return None
        session_id = _secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = time.time()
        return session_id

    def check_session(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        now = time.time()
        with self._lock:
            last = self._sessions.get(session_id)
            if last is None:
                return False
            if now - last > self.idle_seconds:
                del self._sessions[session_id]
                return False
            self._sessions[session_id] = now  # sliding expiry
            return True

    def logout(self, session_id: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)

    def verify_step_up(self, presented: str) -> bool:
        """Fresh proof for a sensitive operation (spec 6)."""
        return self._step_up(presented or "")
