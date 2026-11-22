"""Hot-editable agent registry (spec sections 3, 4.2-4.5, 8.1).

The registry is the hub's live agent state: profiles are kept in a JSON
state file that the hub reads at routing time, so add / edit / pause /
hard-delete take effect WITHOUT rebuilding or restarting anything.
On first boot (no state file yet) the registry can be seeded from the
legacy env-style configuration the hub used before (AGENT_KEYS plus
per-agent <KEY>_* fields), which makes migration a one-time import.

Bot tokens are NEVER stored here. The registry only knows whether a
token is configured, its length and when it was last set; the values
live in secrets_store.SecretStore.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

RESERVED_KEYS = {"hub"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MIN_RECEIVE_DEADLINE = 180  # seconds, per current hub rules
SEAT_TYPES = ("live", "api")


class RegistryError(ValueError):
    """Raised for any invalid registry operation."""


@dataclass
class AgentProfile:
    key: str
    display_name: str
    bot_id: str = ""
    bot_username: str = ""
    call_tokens: List[str] = field(default_factory=list)
    receive_all: bool = False
    seat_type: str = "live"  # "live" = own runner/sentinel; "api" = hub-side API runner
    role: str = ""
    description: str = ""
    enabled: bool = False  # new agents start paused (spec 4.2)
    pause_reason: str = ""
    paused_by: str = ""
    paused_at: Optional[float] = None
    receive_deadline: Optional[int] = None  # None = hub default
    # SSH key the admin page installed for this agent on the
    # coordination VM (keydrop flow). Fingerprint only - the public
    # key itself lives in the VM's authorized_keys, never here.
    ssh_key_fingerprint: str = ""
    ssh_key_added_at: Optional[float] = None
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def public_dict(self, token_meta: Optional[dict] = None) -> dict:
        """Serialization for the admin page. Never contains secrets."""
        data = asdict(self)
        meta = token_meta or {}
        data["token_configured"] = bool(meta.get("configured"))
        data["token_length"] = int(meta.get("length") or 0)
        data["token_updated_at"] = meta.get("updated_at")
        return data


class AgentRegistry:
    """Thread-safe, file-backed registry of agent profiles."""

    def __init__(self, state_path: str, secret_meta: Optional[Callable[[str], dict]] = None):
        self.state_path = state_path
        self._secret_meta = secret_meta or (lambda key: {})
        self._lock = threading.RLock()
        self._agents: Dict[str, AgentProfile] = {}
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.state_path):
            self._agents = {}
            return
        with open(self.state_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self._agents = {k: AgentProfile(**v) for k, v in raw.get("agents", {}).items()}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        payload = {"agents": {k: asdict(v) for k, v in sorted(self._agents.items())}}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)  # atomic: readers never see a half file

    # ------------------------------------------------------------------
    # seeding from the legacy env-style configuration
    # ------------------------------------------------------------------
    def seed_from_env(self, env: Dict[str, str]) -> List[str]:
        """Import AGENT_KEYS + <KEY>_* fields once. Returns imported keys.

        Only imports when the registry is empty, so a restart never
        overwrites live edits made through the admin page.
        """
        with self._lock:
            if self._agents:
                return []
            keys = [k.strip().lower() for k in env.get("AGENT_KEYS", "").split(",") if k.strip()]
            imported = []
            for key in keys:
                prefix = key.upper()
                call_tokens = []
                raw_tokens = env.get(f"{prefix}_CALL_TOKENS", "")
                if raw_tokens:
                    try:
                        call_tokens = [str(t) for t in json.loads(raw_tokens)]
                    except (ValueError, TypeError):
                        call_tokens = [t.strip() for t in raw_tokens.split(",") if t.strip()]
                deadline = env.get(f"{prefix}_RECEIVE_DEADLINE")
                profile = AgentProfile(
                    key=key,
                    display_name=env.get(f"{prefix}_DISPLAY_NAME", key),
                    bot_id=env.get(f"{prefix}_BOT_ID", ""),
                    call_tokens=call_tokens,
                    enabled=True,  # env-listed agents were live by definition
                    receive_deadline=int(deadline) if deadline else None,
                    created_at=time.time(),
                    updated_at=time.time(),
                )
                self._validate(profile, ignore_key=None)
                self._agents[key] = profile
                imported.append(key)
            if imported:
                self._save()
            return imported

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def _validate(self, profile: AgentProfile, ignore_key: Optional[str]) -> None:
        if not KEY_RE.match(profile.key or ""):
            raise RegistryError(f"invalid agent key: {profile.key!r}")
        if profile.key in RESERVED_KEYS:
            raise RegistryError(f"agent key {profile.key!r} is reserved")
        if not (profile.display_name or "").strip():
            raise RegistryError("display_name must not be empty")
        if profile.seat_type not in SEAT_TYPES:
            raise RegistryError(f"seat_type must be one of {SEAT_TYPES}")
        if profile.receive_deadline is not None and profile.receive_deadline < MIN_RECEIVE_DEADLINE:
            raise RegistryError(
                f"receive_deadline must be >= {MIN_RECEIVE_DEADLINE}s or unset"
            )
        seen: Dict[str, str] = {}
        for other_key, other in self._agents.items():
            if other_key == ignore_key:
                continue
            for token in other.call_tokens:
                seen[token.casefold()] = other_key
        for token in profile.call_tokens:
            folded = token.casefold()
            if folded in seen:
                raise RegistryError(
                    f"call token {token!r} already used by agent {seen[folded]!r}"
                )
            seen[folded] = profile.key

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def list_agents(self) -> List[AgentProfile]:
        with self._lock:
            return [self._agents[k] for k in sorted(self._agents)]

    def get(self, key: str) -> AgentProfile:
        with self._lock:
            try:
                return self._agents[key]
            except KeyError:
                raise RegistryError(f"unknown agent: {key!r}") from None

    def public_list(self) -> List[dict]:
        return [p.public_dict(self._secret_meta(p.key)) for p in self.list_agents()]

    def public_get(self, key: str) -> dict:
        profile = self.get(key)
        return profile.public_dict(self._secret_meta(profile.key))

    def create(self, data: dict) -> AgentProfile:
        with self._lock:
            key = (data.get("key") or "").strip().lower()
            if key in self._agents:
                raise RegistryError(f"agent {key!r} already exists")
            now = time.time()
            profile = AgentProfile(
                key=key,
                display_name=data.get("display_name", ""),
                bot_id=str(data.get("bot_id", "")),
                bot_username=data.get("bot_username", ""),
                call_tokens=list(data.get("call_tokens") or []),
                receive_all=bool(data.get("receive_all", False)),
                seat_type=data.get("seat_type", "live"),
                role=data.get("role", ""),
                description=data.get("description", ""),
                enabled=False,  # spec 4.2: new agents start paused
                receive_deadline=data.get("receive_deadline"),
                notes=data.get("notes", ""),
                created_at=now,
                updated_at=now,
            )
            self._validate(profile, ignore_key=None)
            self._agents[key] = profile
            self._save()
            return profile

    _EDITABLE = {
        "display_name", "bot_id", "bot_username", "call_tokens", "receive_all",
        "seat_type", "role", "description", "receive_deadline", "notes",
    }

    def update(self, key: str, data: dict) -> AgentProfile:
        with self._lock:
            profile = self.get(key)
            for name, value in data.items():
                if name == "key":
                    continue  # agent id is immutable (spec 4.3)
                if name not in self._EDITABLE:
                    raise RegistryError(f"field {name!r} is not editable")
                setattr(profile, name, value)
            self._validate(profile, ignore_key=key)
            profile.updated_at = time.time()
            self._save()
            return profile

    def record_ssh_key(self, key: str, fingerprint: str) -> AgentProfile:
        """Stamp the fingerprint of the SSH key the admin page just
        installed for this agent on the coordination VM. Called ONLY
        after the keydrop submit succeeded - a failed submit must
        leave the profile untouched."""
        with self._lock:
            profile = self.get(key)
            profile.ssh_key_fingerprint = fingerprint
            profile.ssh_key_added_at = time.time()
            profile.updated_at = time.time()
            self._save()
            return profile

    def set_enabled(self, key: str, enabled: bool, *, by: str, reason: str = "") -> AgentProfile:
        """Pause / resume at hub level (spec 4.4). Hot: no restart."""
        with self._lock:
            profile = self.get(key)
            profile.enabled = enabled
            if enabled:
                profile.pause_reason = ""
                profile.paused_by = ""
                profile.paused_at = None
            else:
                profile.pause_reason = reason
                profile.paused_by = by
                profile.paused_at = time.time()
            profile.updated_at = time.time()
            self._save()
            return profile

    def hard_delete(self, key: str, queue_probe: Callable[[str], dict]) -> AgentProfile:
        """Hard delete (spec 4.5, decided 2026-11-22: no soft delete).

        Refuses while the agent still has pending messages or held
        leases; the caller must drain / pause first. Journal and audit
        history are NOT touched - they belong to the whole hub.
        """
        with self._lock:
            profile = self.get(key)
            state = queue_probe(key) or {}
            pending = int(state.get("pending", 0))
            leases = int(state.get("leases", 0))
            if pending or leases:
                raise RegistryError(
                    f"cannot delete {key!r}: {pending} pending, {leases} leased; "
                    "drain or pause the agent first"
                )
            del self._agents[key]
            self._save()
            return profile

    # ------------------------------------------------------------------
    # routing view used by the hub core
    # ------------------------------------------------------------------
    def routing_table(self) -> Dict[str, dict]:
        """Snapshot the hub's router consumes: only enabled agents route."""
        with self._lock:
            return {
                key: {
                    "call_tokens": list(p.call_tokens),
                    "receive_all": p.receive_all,
                    "receive_deadline": p.receive_deadline,
                    "seat_type": p.seat_type,
                }
                for key, p in self._agents.items()
                if p.enabled
            }
