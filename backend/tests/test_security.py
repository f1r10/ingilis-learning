"""Pure crypto / token tests: no DB, no network."""
from __future__ import annotations

import re

from app.core import security


def test_hash_and_verify_roundtrip():
    h = security.hash_secret("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert security.verify_secret(h, "correct horse battery staple") is True
    assert security.verify_secret(h, "wrong") is False


def test_verify_against_non_hash_is_false_not_error():
    # A malformed/legacy stored value must not raise, just fail closed.
    assert security.verify_secret("not-an-argon2-hash", "whatever") is False


def test_needs_rehash_after_param_change():
    h = security.hash_secret("pw")
    # Same parameters as the module hasher -> no rehash required.
    assert security.needs_rehash(h) is False


def test_access_key_format_and_uniqueness():
    display, key_hash = security.generate_access_key("jdoe")
    assert display.startswith("jdoe@")
    secret = display.split("@", 1)[1]
    assert len(secret) >= 32  # token_urlsafe(32)
    # Stored value is a hash, never the plaintext.
    assert secret not in key_hash
    # Two generated keys for the same user differ.
    other, _ = security.generate_access_key("jdoe")
    assert other != display


def test_recovery_codes_format_grouping():
    codes = security.generate_recovery_codes(5)
    assert len(codes) == 5
    for display, key_hash in codes:
        # 16 hex chars grouped XXXX-XXXX-XXXX-XXXX for easy manual entry.
        assert re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}", display)
        assert display not in key_hash


def test_session_token_embeds_and_roundtrips_epoch():
    token = security.create_session_token(security.SubjectType.ADMIN, "sid-1", epoch=7)
    payload = security.read_session_token(token, max_age_seconds=3600)
    assert payload == {"t": "admin", "sid": "sid-1", "e": 7}


def test_session_token_without_epoch_has_no_e_key():
    token = security.create_session_token(security.SubjectType.STUDENT, "sid-2")
    payload = security.read_session_token(token, max_age_seconds=3600)
    assert "e" not in payload
    assert payload["t"] == "student"


def test_session_token_tampered_rejected():
    token = security.create_session_token(security.SubjectType.ADMIN, "sid-3", epoch=1)
    # A flipped character breaks the signature and must be rejected.
    assert security.read_session_token(token + "x", max_age_seconds=3600) is None
    # A token from the *other* serializer (CSRF secret) is not a valid session token.
    csrf = security.create_csrf_token("sid-3")
    assert security.read_session_token(csrf, max_age_seconds=3600) is None


def test_csrf_token_bound_to_subject():
    csrf = security.create_csrf_token("sid-9")
    assert security.verify_csrf_token(csrf, "sid-9") is True
    # A CSRF token minted for one subject must not validate for another.
    assert security.verify_csrf_token(csrf, "sid-other") is False
    assert security.verify_csrf_token("garbage", "sid-9") is False
