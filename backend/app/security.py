"""Demo-grade auth: pbkdf2 password hashing + HMAC-signed bearer tokens.

Explicitly out of scope: password recovery, captchas, refresh tokens, CSRF —
spec §0 excludes real authentication security.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from .config import settings

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_token(user_id: int) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + settings.auth_token_expire_hours * 3600}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str) -> int | None:
    try:
        body, sig = token.split(".")
        expected = _b64e(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return int(payload["uid"])
    except Exception:
        return None


def mask_email(email: str) -> str:
    """For the seller-side buyer list: enough to tell buyers apart without
    exposing the full email address."""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        shown = local[:1]
    else:
        shown = local[:2]
    return f"{shown}***@{domain}"
