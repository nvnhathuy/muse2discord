"""Temporary access grants for agents (spec 5b, decided 2026-11-22).

Agents have NO standing access to the admin page. The owner (admin
principal) may issue a temporary grant: a one-off token, scoped and
time-boxed, revocable at any moment with immediate effect.

Hard limits baked into the model:
- scopes are "read", "operate" and "full". A "full" grant is
  admin-equivalent (owner decision, 2026-11-22 09:50): the holder acts
  for the admin during an incident, INCLUDING sensitive operations.
  "read"/"operate" grants still never reach SENSITIVE_OPS.
- only the admin principal issues or revokes grants - even a "full"
  grant holder cannot mint new grants (no self-perpetuating access).
- every grant has an expiry (default 1 hour, maximum 24 hours).
- the token plaintext is returned exactly once, at issue time; only
  its SHA-256 hash is stored.
- revocation is immediate: authenticate() checks the revoked flag on
  every call, nothing is cached.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

SCOPES = ("read", "operate", "full")
DEFAULT_TTL = 3600          # 1 hour
MAX_TTL = 24 * 3600         # 24 hours

# Spec section 6: sensitive operations. Reachable by the admin
# principal and by holders of a still-valid "full" grant only.
SENSITIVE_OPS = {
    "delete_agent",
    "set_bot_token",
    "set_ssh_key",
    "set_agent_lock",
    "reset_cursor",
    "ack_on_behalf",
    "force_release_bulk",
    "edit_ops_config",
}

# What an "operate" grant may do (normal operations, spec section 6).
OPERATE_OPS = {"pause_agent", "resume_agent", "edit_agent_descriptive", "release_lease"}
READ_OPS = {"view"}


def scope_allows(scope: str, op: str) -> bool:
    if scope == "full":
        return True  # admin-equivalent temporary grant (2026-11-22 decision)
    if op in SENSITIVE_OPS:
        return False  # read/operate grants never reach sensitive ops
    if op in READ_OPS:
        return scope in SCOPES
    if op in OPERATE_OPS:
        return scope == "operate"
    return False


@dataclass
class Grant:
    id: str
    agent_key: str
    scope: str
    reason: str
    issued_at: float
    expires_at: float
    token_hash: str
    revoked_at: Optional[float] = None
    issued_by: str = "admin"

    def is_active(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return self.revoked_at is None and now < self.expires_at

    def public_dict(self, now: Optional[float] = None) -> dict:
        data = asdict(self)
        data.pop("token_hash", None)
        data["active"] = self.is_active(now)
        data["seconds_left"] = max(0, int(self.expires_at - (now or time.time())))
        return data


class GrantStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._grants: Dict[str, Grant] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._grants = {gid: Grant(**g) for gid, g in raw.get("grants", {}).items()}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        payload = {"grants": {gid: asdict(g) for gid, g in self._grants.items()}}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        agent_key: str,
        scope: str,
        reason: str,
        ttl_seconds: Optional[int] = None,
        issued_by: str = "admin",
    ) -> tuple[Grant, str]:
        """Create a grant. Returns (grant, plaintext_token) - the token
        is shown once and never stored."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        ttl = DEFAULT_TTL if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0 or ttl > MAX_TTL:
            raise ValueError(f"ttl must be within 1..{MAX_TTL} seconds")
        if not reason:
            raise ValueError("a reason is required (it goes into the audit log)")
        token = _secrets.token_urlsafe(32)
        now = time.time()
        grant = Grant(
            id=uuid.uuid4().hex[:12],
            agent_key=agent_key,
            scope=scope,
            reason=reason,
            issued_at=now,
            expires_at=now + ttl,
            token_hash=self._hash(token),
            issued_by=issued_by,
        )
        with self._lock:
            self._grants[grant.id] = grant
            self._save()
        return grant, token

    def authenticate(self, token: str) -> Optional[Grant]:
        """Resolve a presented token to an ACTIVE grant, or None.
        Revocation and expiry take effect on the very next call."""
        if not token:
            return None
        digest = self._hash(token)
        with self._lock:
            for grant in self._grants.values():
                if grant.token_hash == digest and grant.is_active():
                    return grant
        return None

    def revoke(self, grant_id: str) -> Grant:
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                raise ValueError(f"unknown grant: {grant_id!r}")
            if grant.revoked_at is None:
                grant.revoked_at = time.time()
                self._save()
            return grant

    def list_grants(self, active_only: bool = False) -> List[Grant]:
        with self._lock:
            grants = sorted(self._grants.values(), key=lambda g: g.issued_at, reverse=True)
        if active_only:
            grants = [g for g in grants if g.is_active()]
        return grants
