"""Agent SSH public-key intake, hub side (owner decision 2026-11-22).

The admin page itself installs an agent's SSH public key on the
coordination VM - no human intermediary. The hub holds its OWN SSH
keypair (hub_ssh_key in the data dir, mode 0600, generated on first
use) and talks to a locked-down "keydrop" account on the VM: that
account's authorized_keys forces every connection into an intake
script that only accepts "<agent-key>\\n<public-key>\\n" on stdin and
appends it to a pending file; a root-side installer on the VM then
validates again and appends to the operator's authorized_keys.

This module is the hub half of that handshake:
- validate_public_key(): strict single-line ssh-ed25519 validation,
  comment stripping and the OpenSSH SHA256 fingerprint.
- KeydropClient.submit(): sends the two-line payload over SSH to
  $HUB_KEYDROP_TARGET. Any transport failure raises KeydropError and
  NOTHING is recorded anywhere - the caller only writes the registry
  after a confirmed submit.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import subprocess
from typing import Callable, Optional, Tuple

# Same shape the VM-side intake enforces: type + base64 blob of sane
# length; an optional comment may follow and is always discarded.
KEY_LINE_RE = re.compile(r"^ssh-ed25519 ([A-Za-z0-9+/]{40,}={0,3})(?: .*)?$")
AGENT_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

KEY_FILENAME = "hub_ssh_key"


class KeydropError(Exception):
    """Transport/configuration failure while submitting a key.

    Deliberately NOT a RuntimeError subclass: the API layer maps
    RuntimeError to 400, while a failed keydrop submit is a 502."""


def key_fingerprint(blob_b64: str) -> str:
    """OpenSSH-style SHA256 fingerprint of a validated key blob."""
    try:
        raw = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("public key blob is not valid base64") from None
    # An SSH public-key blob is a sequence of length-prefixed fields;
    # the first field MUST be the algorithm name. This stops random
    # base64 (or another algorithm's blob) from passing as ed25519.
    if len(raw) < 4:
        raise ValueError("public key blob is too short")
    name_len = int.from_bytes(raw[:4], "big")
    if name_len != len(b"ssh-ed25519") or raw[4:4 + name_len] != b"ssh-ed25519":
        raise ValueError("public key blob is not an ssh-ed25519 key")
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")


def validate_public_key(raw) -> Tuple[str, str]:
    """Validate an agent-supplied public key.

    Returns (normalized_line, fingerprint) where normalized_line is
    exactly "ssh-ed25519 <blob>" - any comment the agent attached is
    dropped here, the system assigns its own comment downstream.
    Raises ValueError for anything else: wrong type, multiple lines,
    injection attempts, malformed blob.
    """
    if not isinstance(raw, str):
        raise ValueError("public_key must be a string")
    text = raw.strip()
    if not text or "\n" in text or "\r" in text:
        raise ValueError("public_key must be exactly one line")
    match = KEY_LINE_RE.match(text)
    if not match:
        raise ValueError("public_key must be a single ssh-ed25519 key line")
    blob = match.group(1)
    fingerprint = key_fingerprint(blob)  # raises ValueError if not really ed25519
    return f"ssh-ed25519 {blob}", fingerprint


def _subprocess_runner(argv, stdin_text: str, timeout: float) -> Tuple[int, str]:
    proc = subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stderr or "").strip()


# A runner takes (argv, stdin_text, timeout_seconds) and returns
# (returncode, stderr_text). Tests inject a fake; production uses SSH.
Runner = Callable[[list, str, float], Tuple[int, str]]


class KeydropClient:
    """Submits agent public keys to the VM keydrop account over SSH."""

    def __init__(
        self,
        state_dir: str,
        target: str = "",
        runner: Optional[Runner] = None,
        keygen_runner: Optional[Runner] = None,
    ):
        self.state_dir = state_dir
        self.target = (target or "").strip()
        self._runner = runner or _subprocess_runner
        self._keygen_runner = keygen_runner or _subprocess_runner

    @property
    def key_path(self) -> str:
        return os.path.join(self.state_dir, KEY_FILENAME)

    def ensure_keypair(self) -> str:
        """Generate the hub's own keypair on first use (0600 private).

        Returns the private key path. Idempotent: an existing key is
        never overwritten."""
        path = self.key_path
        if os.path.exists(path):
            return path
        os.makedirs(self.state_dir, exist_ok=True)
        rc, stderr = self._keygen_runner(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "hub-admin-keydrop",
             "-f", path],
            "",
            30,
        )
        if rc != 0 or not os.path.exists(path):
            raise KeydropError(f"could not generate the hub ssh keypair: {stderr}")
        os.chmod(path, 0o600)
        return path

    def submit(self, agent_key: str, public_key: str) -> str:
        """Validate and ship one agent key. Returns its fingerprint.

        Raises ValueError for a malformed key/agent key (caller maps
        to 400) and KeydropError for any transport problem (caller
        maps to 502; nothing has been recorded in that case)."""
        if not AGENT_KEY_RE.match(agent_key or ""):
            raise ValueError(f"invalid agent key: {agent_key!r}")
        normalized, fingerprint = validate_public_key(public_key)
        if not self.target:
            raise KeydropError(
                "HUB_KEYDROP_TARGET is not configured on this deployment")
        key_path = self.ensure_keypair()
        argv = [
            "ssh",
            "-i", key_path,
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8",
            # The container user has no writable HOME; keep the
            # known-hosts file inside the data dir instead.
            "-o", f"UserKnownHostsFile={os.path.join(self.state_dir, 'keydrop_known_hosts')}",
            self.target,
        ]
        payload = f"{agent_key}\n{normalized}\n"
        try:
            rc, stderr = self._runner(argv, payload, 20)
        except subprocess.TimeoutExpired:
            raise KeydropError("ssh to the keydrop account timed out") from None
        except OSError as exc:
            raise KeydropError(f"could not run ssh: {exc}") from None
        if rc != 0:
            raise KeydropError(
                f"keydrop intake refused the key (ssh rc={rc})"
                + (f": {stderr}" if stderr else ""))
        return fingerprint
