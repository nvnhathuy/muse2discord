"""RFC 6238 TOTP (stdlib only) for admin step-up authentication.

Sensitive operations on the admin page require a fresh step-up proof.
In integrated mode that proof is a real TOTP code: the admin enrolls a
secret in the page once (stored in the 0600 credentials file), and
afterwards presents the current 6-digit code from their authenticator
app in the X-Admin-Reauth header. Verification accepts the previous /
current / next 30-second step to tolerate small clock skew.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def generate_secret() -> str:
    """160-bit random secret, base32 (authenticator-app format)."""
    return base64.b32encode(_secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padding = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + padding)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** DIGITS)).zfill(DIGITS)


def totp_now(secret_b32: str, at: Optional[float] = None) -> str:
    at = time.time() if at is None else at
    return _hotp(secret_b32, int(at // STEP_SECONDS))


def verify_totp(secret_b32: str, code: str, at: Optional[float] = None, window: int = 1) -> bool:
    code = (code or "").strip()
    if not code.isdigit() or len(code) != DIGITS:
        return False
    at = time.time() if at is None else at
    counter = int(at // STEP_SECONDS)
    return any(
        hmac.compare_digest(_hotp(secret_b32, counter + delta), code)
        for delta in range(-window, window + 1)
    )


def otpauth_uri(secret_b32: str, account: str = "admin", issuer: str = "Hub Admin") -> str:
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SECONDS}"
    )
