"""Security primitives: password/key hashing and signed session tokens.

- Argon2id for passwords, student access keys and recovery codes (only hashes
  are ever stored).
- itsdangerous signed, time-limited session cookies. The random session id also
  has a row in the database so the server can terminate sessions.
- Double-submit CSRF token signed separately from the session.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timezone
from enum import Enum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

_settings = get_settings()

# time_cost/parallelism tuned for a single-teacher server; still Argon2id.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32)

_session_serializer = URLSafeTimedSerializer(_settings.session_secret, salt="session")
_csrf_serializer = URLSafeTimedSerializer(_settings.csrf_secret, salt="csrf")


class SubjectType(str, Enum):
    ADMIN = "admin"
    STUDENT = "student"


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def hash_secret(value: str) -> str:
    """Hash a password, access key or recovery code (Argon2id)."""
    return _hasher.hash(value)


def verify_secret(hash_: str, value: str) -> bool:
    try:
        return _hasher.verify(hash_, value)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hash_: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hash_)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------- #
# High-entropy secret generation
# --------------------------------------------------------------------------- #
# 32 url-safe segments -> >=192 bits of entropy.
def generate_secret_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def generate_access_key(username: str) -> tuple[str, str]:
    """Return (display_key, hash). Display format: username@LONG_RANDOM_SECRET."""
    secret = generate_secret_token(32)
    display = f"{username}@{secret}"
    return display, hash_secret(display)


# Length of the secret fingerprint kept in plaintext for lookup.
KEY_FINGERPRINT_CHARS = 6


def access_key_fingerprint(display_key: str) -> str:
    """Non-secret lookup fingerprint: ``username@<first chars of the secret>``.

    Stored so a login can find its candidate key with one equality lookup instead
    of Argon2-verifying every key in the table. It is derived from the displayed
    key itself (never from the current username), so renaming a student does not
    silently invalidate an already-issued key.
    """
    username, _, secret = display_key.rpartition("@")
    return f"{username}@{secret[:KEY_FINGERPRINT_CHARS]}"


def generate_recovery_codes(count: int = 5) -> list[tuple[str, str]]:
    """Return list of (display_code, hash) for one-time recovery codes.

    Codes are grouped like XXXX-XXXX-XXXX for easy manual entry.
    """
    out: list[tuple[str, str]] = []
    for _ in range(count):
        raw = secrets.token_hex(8).upper()  # 16 hex chars
        display = "-".join(raw[i : i + 4] for i in range(0, 16, 4))
        out.append((display, hash_secret(display)))
    return out


# --------------------------------------------------------------------------- #
# Signed session / CSRF tokens
# --------------------------------------------------------------------------- #
def create_session_token(
    subject_type: SubjectType, session_id: str, epoch: int | None = None
) -> str:
    payload = {"t": subject_type.value, "sid": session_id}
    if epoch is not None:
        payload["e"] = epoch
    return _session_serializer.dumps(payload)


def read_session_token(token: str, max_age_seconds: int) -> dict | None:
    try:
        data = _session_serializer.loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def create_csrf_token(session_id: str) -> str:
    return _csrf_serializer.dumps({"sid": session_id})


def verify_csrf_token(token: str, session_id: str, max_age_seconds: int = 86400) -> bool:
    try:
        data = _csrf_serializer.loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and hmac.compare_digest(data.get("sid", ""), session_id)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
