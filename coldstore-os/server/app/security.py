"""Authentication & authorization.

- Passwords: PBKDF2-HMAC-SHA256 (stdlib hashlib, 210k iterations, 128-bit salt).
  Format: pbkdf2$<iterations>$<salt_hex>$<hash_hex>. Stdlib chosen deliberately
  (no extra native deps on a Windows office server); algorithm/params documented.
- Sessions: opaque 256-bit tokens; only sha256(token) stored; 12h expiry; revocable.
- RBAC: role → permission scopes, enforced SERVER-SIDE only (api/deps.py).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from .config import PASSWORD_MIN_LEN, PBKDF2_ITERATIONS, SESSION_HOURS

ALGO = "pbkdf2"

ROLES = [
    "system_admin", "facility_manager", "intake_operator", "inventory_operator",
    "billing_operator", "dispatch_operator", "ca_monitor", "auditor",
]

# scope strings: domain.verb ; "admin" = everything; auditors = all reads + audit
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "system_admin": {"admin"},
    "facility_manager": {
        "masters.read", "masters.write", "parties.read", "parties.write",
        "intake.read", "intake.write", "intake.verify", "intake.correct",
        "lots.read", "lots.write", "chambers.read", "chambers.write",
        "movements.write", "dispatch.read", "dispatch.write", "dispatch.void",
        "adjustments.create", "adjustments.approve",
        "billing.read", "billing.write", "billing.void", "payments.write",
        "reports.read", "reports.financial", "audit.read", "backups.manage",
        "users.read",
        "telemetry.read", "telemetry.ack", "telemetry.config",
    },
    "intake_operator": {
        "masters.read", "parties.read", "parties.write",
        "intake.read", "intake.write", "intake.verify",
        "lots.read", "chambers.read", "reports.read",
    },
    "inventory_operator": {
        "masters.read", "parties.read", "lots.read", "lots.write",
        "chambers.read", "movements.write", "adjustments.create", "reports.read",
    },
    "billing_operator": {
        "masters.read", "parties.read", "lots.read", "chambers.read",
        "billing.read", "billing.write", "payments.write", "reports.read", "reports.financial",
        "intake.read", "dispatch.read",
    },
    "dispatch_operator": {
        "masters.read", "parties.read", "lots.read", "chambers.read",
        "dispatch.read", "dispatch.write", "reports.read",
    },
    "ca_monitor": {
        "chambers.read", "masters.read",
        "telemetry.read", "telemetry.ack",
    },
    # telemetry scopes wired into the remaining roles:
    "auditor": {
        "masters.read", "parties.read", "intake.read", "lots.read", "chambers.read",
        "dispatch.read", "billing.read", "reports.read", "reports.financial",
        "audit.read", "users.read", "telemetry.read",
    },
}


def hash_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LEN:
        raise ValueError(f"password must be >= {PASSWORD_MIN_LEN} chars")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{ALGO}${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 256-bit


# ---------------------------------------------------------------- TOTP (RFC 6238)
import base64
import struct
import time

TOTP_STEP = 30
TOTP_DIGITS = 6


def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_code_now(secret_b32: str, when: int | None = None) -> str:
    return _totp_at(secret_b32, int((when if when is not None else time.time())) // TOTP_STEP)


def totp_verify(secret_b32: str, code: str, last_step: int,
                when: int | None = None, window: int = 1) -> tuple[bool, int]:
    """Verify a 6-digit code within ±window steps. Returns (ok, accepted_step) —
    the caller must persist accepted_step and reject steps <= it (replay guard)."""
    step = int((when if when is not None else time.time())) // TOTP_STEP
    for s in range(step - window, step + window + 1):
        if s <= last_step:
            continue
        if hmac.compare_digest(_totp_at(secret_b32, s), code.strip()):
            return True, s
    return False, last_step


def totp_uri(secret_b32: str, username: str, issuer: str = "ColdStore OS") -> str:
    return (f"otpauth://totp/{issuer}:{username}?secret={secret_b32}"
            f"&issuer={issuer.replace(' ', '%20')}&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_expiry() -> datetime:
    return datetime.utcnow() + timedelta(hours=SESSION_HOURS)


def has_scope(role: str, scope: str) -> bool:
    perms = ROLE_PERMISSIONS.get(role, set())
    return "admin" in perms or scope in perms
