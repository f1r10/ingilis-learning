"""Admin authentication paths against a real Postgres built by the migration (spec S7).

Covers the flows the mock-free suite must prove: bootstrap/seed admin login,
invalid login, authenticated request, logout replay, password change killing old
sessions, and one-time recovery codes. Also checks the error envelope stays
consistent and non-revealing (S10). Skips via `clean_db` when Postgres is down.
"""
from __future__ import annotations

from helpers import error_of
from sqlalchemy import select

from app.core import security
from app.core.database import SessionLocal
from app.models.identity import AdminRecoveryCode, AdminUser


async def _admin_epoch(username: str) -> int:
    async with SessionLocal() as db:
        admin = (
            await db.execute(select(AdminUser).where(AdminUser.username == username))
        ).scalar_one()
        return admin.session_epoch


async def test_seeded_admin_logins_and_authorizes(client, admin_credentials):
    """The account `clean_db` seeds (mirroring scripts/seed) really authenticates."""
    username, password = admin_credentials
    async with SessionLocal() as db:
        seeded = (
            await db.execute(select(AdminUser).where(AdminUser.username == username))
        ).scalar_one()
    assert security.verify_secret(seeded.password_hash, password)

    await client.login_admin()
    me = await client.get("/api/v1/auth/me/admin")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["username"] == username
    assert body["id"] == str(seeded.id)
    assert body["subject_type"] == "admin"


async def test_invalid_credentials_never_reveal_whether_the_user_exists(
    session_factory, clean_db, admin_credentials
):
    """S10: one 401 envelope for a wrong password and for an unknown username."""
    username, _password = admin_credentials
    c = session_factory()

    wrong_pw = await c._c.post(
        "/api/v1/auth/admin/login", json={"username": username, "password": "totally-wrong-1"}
    )
    unknown = await c._c.post(
        "/api/v1/auth/admin/login",
        json={"username": "no-such-admin-at-all", "password": "totally-wrong-1"},
    )

    assert wrong_pw.status_code == 401, wrong_pw.text
    assert unknown.status_code == 401, unknown.text
    assert error_of(wrong_pw) == error_of(unknown), (
        "login errors must not distinguish 'unknown user' from 'wrong password'"
    )
    assert error_of(wrong_pw)["code"] == "unauthorized"
    # The answer has to stay generic. Naming both halves together is the safe form;
    # what must never appear is a message that identifies WHICH one was wrong,
    # because that confirms a username exists (or does not).
    message = error_of(wrong_pw)["message"].lower()
    assert "username or password" in message, message
    for leak in ("no such", "not found", "does not exist", "unknown user", "wrong username", "wrong password"):
        assert leak not in message, f"the error reveals which half failed: {message}"


async def test_replayed_cookie_is_rejected_after_logout(session_factory, clean_db):
    """Logout must invalidate server-side, not merely clear the browser cookie."""
    c = session_factory()
    await c.login_admin()
    captured = c.session_cookie()
    assert captured

    assert (await c.get("/api/v1/auth/me/admin")).status_code == 200
    assert (await c.post("/api/v1/auth/logout")).status_code == 200

    # Re-apply the exact cookie the client used to hold: it must no longer work.
    c._c.cookies.set("llp_session", captured, path="/")
    after = await c.get("/api/v1/auth/me/admin")
    assert after.status_code == 401, after.text
    assert error_of(after)["message"] == "Session invalidated"


async def test_password_change_invalidates_every_session_including_the_callers(
    session_factory, clean_db, admin_credentials
):
    username, password = admin_credentials
    old_pw, new_pw = password, "rotated-admin-pw-9876"

    session_a = session_factory()
    session_b = session_factory()
    await session_a.login(username, old_pw)
    await session_b.login(username, old_pw)

    changed = await session_a.post(
        "/api/v1/admin/password",
        json={"current_password": old_pw, "new_password": new_pw},
    )
    assert changed.status_code == 200, changed.text

    # Both pre-change sessions are dead - including the one that made the change,
    # which is why the client must treat this as "log in again".
    for label, sess in (("caller", session_a), ("other", session_b)):
        r = await sess.get("/api/v1/auth/me/admin")
        assert r.status_code == 401, f"{label} session still alive: {r.text}"

    old_pw_login = await session_factory().post(
        "/api/v1/auth/admin/login", json={"username": username, "password": old_pw}
    )
    assert old_pw_login.status_code == 401, old_pw_login.text

    fresh = session_factory()
    await fresh.login(username, new_pw)
    assert (await fresh.get("/api/v1/auth/me/admin")).status_code == 200


async def test_wrong_current_password_is_rejected_without_touching_the_epoch(
    client, admin_credentials
):
    username, _password = admin_credentials
    before = await _admin_epoch(username)
    await client.login_admin()

    r = await client.post(
        "/api/v1/admin/password",
        json={"current_password": "not-the-password-1", "new_password": "another-pw-12345"},
    )
    assert r.status_code == 422, r.text
    assert error_of(r)["code"] == "validation_failed"
    assert await _admin_epoch(username) == before
    # the session is untouched and the password unchanged
    assert (await client.get("/api/v1/auth/me/admin")).status_code == 200
    again = await client._c.post(
        "/api/v1/auth/admin/login", json={"username": username, "password": "another-pw-12345"}
    )
    assert again.status_code == 401, again.text


async def test_recovery_code_signs_in_exactly_once(client, session_factory, clean_db, admin_credentials):
    username, _password = admin_credentials
    await client.login_admin()
    generated = await client.post("/api/v1/admin/recovery-codes")
    assert generated.status_code == 200, generated.text
    codes = generated.json()["codes"]
    assert len(codes) == 5

    login = session_factory()
    first = await login._c.post(
        "/api/v1/auth/admin/recovery/login", json={"username": username, "code": codes[0]}
    )
    assert first.status_code == 200, first.text
    assert (await login.get("/api/v1/auth/me/admin")).status_code == 200

    reuse = await session_factory()._c.post(
        "/api/v1/auth/admin/recovery/login", json={"username": username, "code": codes[0]}
    )
    assert reuse.status_code == 401, reuse.text
    assert error_of(reuse)["code"] == "unauthorized"

    async with SessionLocal() as db:
        used = (
            await db.execute(
                select(AdminRecoveryCode).where(AdminRecoveryCode.used_at.is_not(None))
            )
        ).scalars().all()
    assert len(used) == 1, "a recovery code must be single-use"


async def test_regenerated_codes_supersede_the_previous_set(client, session_factory, clean_db, admin_credentials):
    username, _password = admin_credentials
    await client.login_admin()
    first_set = (await client.post("/api/v1/admin/recovery-codes")).json()["codes"]
    second_set = (await client.post("/api/v1/admin/recovery-codes")).json()["codes"]
    assert set(first_set).isdisjoint(second_set)

    stale = await session_factory()._c.post(
        "/api/v1/auth/admin/recovery/login", json={"username": username, "code": first_set[0]}
    )
    assert stale.status_code == 401, stale.text

    live = await session_factory()._c.post(
        "/api/v1/auth/admin/recovery/login", json={"username": username, "code": second_set[1]}
    )
    assert live.status_code == 200, live.text

    unknown_user = await session_factory()._c.post(
        "/api/v1/auth/admin/recovery/login", json={"username": "ghost-admin", "code": second_set[2]}
    )
    assert unknown_user.status_code == 401, unknown_user.text


async def test_recovery_codes_are_stored_hashed_only(client, clean_db):
    await client.login_admin()
    codes = (await client.post("/api/v1/admin/recovery-codes")).json()["codes"]

    async with SessionLocal() as db:
        rows = (await db.execute(select(AdminRecoveryCode))).scalars().all()
    assert rows, "the codes must be persisted, not only returned"
    stored = [r.code_hash for r in rows]
    assert set(codes).isdisjoint(stored), "plaintext recovery code found in the database"
    assert all(h.startswith("$argon2id$") for h in stored), (
        f"recovery codes must be stored as Argon2 hashes: {stored[:1]}"
    )
    for plain in codes:
        assert any(security.verify_secret(h, plain.strip().upper()) for h in stored), (
            "issued code has no matching hash"
        )
