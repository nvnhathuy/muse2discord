"""Admin-principal authentication (spec 5).

The admin principal is fully separate from agent principals and bridge
tokens. The admin secret (password) is verified through an injected
verifier - in production that is credentials.AdminCredentials.verify
(PBKDF2 hashes); a raw token string is still accepted for legacy/test
setups. Login exchanges the secret for a short session; sessions
idle-expire (default 30 minutes, configurable in ops config).

Sessions stamp the credentials VERSION at login. When the password is
changed or reset (page, recovery code, or the server-side CLI), the
version bumps and every older session fails its next check - that is
how a reset kills all sessions without the server restarting.

Sensitive operations additionally require a fresh step-up proof:
presenting the admin secret again in the X-Admin-Reauth header. The
verifier is a single function so the step-up method (password now,
passkey/TOTP later) can be swapped without touching the API layer.
"""

from __future__ import annotations

import hmac
import os
import secrets as _secrets
import threading
import time
from typing import Callable, Dict, Optional, Tuple, Union

Verifier = Callable[[str], bool]


class AdminAuth:
    def __init__(
        self,
        secret: Union[str, Verifier],
        idle_seconds: int = 1800,
        step_up_verifier: Optional[Verifier] = None,
        version_provider: Optional[Callable[[], int]] = None,
    ):
        if isinstance(secret, str):
            if not secret:
                raise ValueError("an admin secret is required")
            token = secret
            self._verify: Verifier = lambda presented: bool(presented) and hmac.compare_digest(presented, token)
        else:
            self._verify = secret
        self.idle_seconds = idle_seconds
        self._sessions: Dict[str, Tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._step_up = step_up_verifier or self._verify
        self._version_provider = version_provider or (lambda: 0)

    @staticmethod
    def load_token(env_name: str = "HUB_ADMIN_TOKEN", file_path: Optional[str] = None) -> str:
        token = os.environ.get(env_name, "")
        if token:
            return token.strip()
        if file_path and os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        raise ValueError(f"no admin token in ${env_name} or {file_path}")

    def login(self, presented_secret: str) -> Optional[str]:
        if not self._verify(presented_secret or ""):
            return None
        session_id = _secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = (time.time(), self._version_provider())
        return session_id

    def check_session(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        now = time.time()
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            last_seen, version = record
            if version != self._version_provider():
                del self._sessions[session_id]  # password changed/reset since login
                return False
            if now - last_seen > self.idle_seconds:
                del self._sessions[session_id]
                return False
            self._sessions[session_id] = (now, version)  # sliding expiry
            return True

    def logout(self, session_id: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)

    def invalidate_all_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()

    def restamp_session(self, session_id: Optional[str]) -> None:
        """Re-stamp one session to the current credentials version -
        used by change-password so the caller's own session survives
        while every other session dies by version mismatch."""
        if not session_id:
            return
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id] = (time.time(), self._version_provider())

    def verify_step_up(self, presented: str) -> bool:
        """Fresh proof for a sensitive operation (spec 6)."""
        return self._step_up(presented or "")
