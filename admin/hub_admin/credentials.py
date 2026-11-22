"""Admin password credentials: strong hashing, recovery code, version.

Storage is a single JSON file (mode 0600) holding ONLY hashes:
- password: PBKDF2-HMAC-SHA256, per-credential random salt, 600k rounds.
- recovery code: same treatment; a code is shown to the admin exactly
  once, is single-use, and generating a new one invalidates the old.
- version: bumped on EVERY password set (change, recovery reset, CLI
  reset, initial seed). Sessions stamp the version at login, so a
  version bump kills every pre-existing session on its next check -
  that is what makes "reset kills all sessions" work even when the
  reset happens from the CLI while the server keeps running.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import threading
import time
from typing import Optional

ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8


def _hash(secret: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations).hex()


class AdminCredentials:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Optional[dict] = None
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def exists(self) -> bool:
        return self._data is not None

    def version(self) -> int:
        """Read fresh each call: a CLI reset must be seen immediately."""
        self._load()
        return int(self._data["version"]) if self._data else 0

    # ------------------------------------------------------------------
    def set_password(self, new_password: str) -> None:
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        with self._lock:
            self._load()
            salt = _secrets.token_bytes(16)
            self._data = {
                "algo": "pbkdf2_sha256",
                "iterations": ITERATIONS,
                "salt": salt.hex(),
                "hash": _hash(new_password, salt),
                "version": (int(self._data["version"]) + 1) if self._data else 1,
                "updated_at": time.time(),
                "recovery": self._data.get("recovery") if self._data else None,
            }
            self._save()

    def verify(self, presented: str) -> bool:
        with self._lock:
            self._load()
            if not self._data:
                return False
            candidate = _hash(presented or "", bytes.fromhex(self._data["salt"]),
                              int(self._data["iterations"]))
            return hmac.compare_digest(candidate, self._data["hash"])

    # ------------------------------------------------------------------
    def generate_recovery_code(self) -> str:
        """Create a fresh recovery code. The plaintext is returned ONCE;
        only its hash is stored. Any previous code is invalidated."""
        code = "-".join(_secrets.token_hex(2).upper() for _ in range(4))  # e.g. AB12-CD34-EF56-7890
        with self._lock:
            self._load()
            if not self._data:
                raise ValueError("no admin password set yet")
            salt = _secrets.token_bytes(16)
            self._data["recovery"] = {
                "salt": salt.hex(),
                "hash": _hash(code, salt, int(self._data["iterations"])),
                "created_at": time.time(),
                "used_at": None,
            }
            self._save()
        return code

    def has_active_recovery_code(self) -> bool:
        self._load()
        recovery = (self._data or {}).get("recovery")
        return bool(recovery and recovery.get("used_at") is None)

    def consume_recovery_code(self, presented: str) -> bool:
        """Check a recovery code and burn it on success (single-use)."""
        with self._lock:
            self._load()
            if not self._data:
                return False
            recovery = self._data.get("recovery")
            if not recovery or recovery.get("used_at") is not None:
                return False
            candidate = _hash(presented or "", bytes.fromhex(recovery["salt"]),
                              int(self._data["iterations"]))
            if not hmac.compare_digest(candidate, recovery["hash"]):
                return False
            recovery["used_at"] = time.time()
            self._save()
            return True
