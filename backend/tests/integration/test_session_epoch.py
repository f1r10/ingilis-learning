"""`admin_user.session_epoch` semantics against real Postgres (spec S9).

The epoch is the single value that makes stateless admin cookies revocable, so
this file pins each property the spec asks for: a sensible non-null default, the
epoch embedded at login, verification on every authenticated request, correct
incrementing at logout and password change, monotonicity under concurrency, and a
freshly bootstrapped admin working end to end.
"""
from __future__ import annotations

import asyncio

import pytest
from helpers import error_of
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core import security
from app.core.database import SessionLocal
from app.models.identity import AdminUser
from app.services import auth_service

BOOTSTRAP_PW = "epoch-bootstrap-pw-123"


async def _epoch(username: str) -> int:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(AdminUser.session_epoch).where(AdminUser.username == username)
            )
        ).scalar_one()


async def _admin_id(username: str):
    async with SessionLocal() as db:
        return (
            await db.execute(select(AdminUser.id).where(AdminUser.username == username))
        ).scalar_one()


async def test_database_column_defaults_to_zero_and_rejects_null(db_ready):
    """The default is a real server-side DEFAULT 0, not only an ORM convenience."""
    async with SessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO admin_user (username, password_hash) "
                "VALUES ('default-epoch-admin', '$argon2id$placeholder')"
            )
        )
        epoch = (
            await db.execute(
                text("SELECT session_epoch FROM admin_user WHERE username = 'default-epoch-admin'")
            )
        ).scalar_one()
        assert epoch == 0, "a row inserted without the column must default to epoch 0"
        await db.commit()

    # ...and the column is genuinely NOT NULL: Postgres, not Python, refuses NULL.
    with pytest.raises(IntegrityError):
        async with SessionLocal() as db:
            await db.execute(
                text(
                    "INSERT INTO admin_user (username, password_hash, session_epoch) "
                    "VALUES ('null-epoch-admin', '$argon2id$placeholder', NULL)"
                )
            )


async def test_login_token_embeds_the_current_epoch(session_factory, clean_db, admin_credentials):
    username, password = admin_credentials
    c = session_factory()
    await c.login(username, password)

    token = c.session_cookie()
    payload = security.read_session_token(token, max_age_seconds=3600)
    assert payload is not None, "session token must be readable by the app's own serializer"
    assert payload["t"] == security.SubjectType.ADMIN.value
    assert payload["e"] == await _epoch(username), "login must embed the live epoch"
    assert payload["sid"] == str(await _admin_id(username))


async def test_every_authenticated_request_revalidates_the_epoch(session_factory, clean_db, admin_credentials):
    """A cookie is only good while its epoch still matches the row."""
    username, _password = admin_credentials
    admin_id = await _admin_id(username)
    c = session_factory()

    forged = security.create_session_token(security.SubjectType.ADMIN, str(admin_id), epoch=99)
    c._c.cookies.set("llp_session", forged, path="/")
    rejected = await c.get("/api/v1/auth/me/admin")
    assert rejected.status_code == 401, rejected.text
    assert error_of(rejected)["message"] == "Session invalidated"

    # A token for the right epoch passes; flipping the row's epoch revokes it.
    live = security.create_session_token(security.SubjectType.ADMIN, str(admin_id), epoch=0)
    c._c.cookies.set("llp_session", live, path="/")
    assert (await c.get("/api/v1/auth/me/admin")).status_code == 200

    async with SessionLocal() as db:
        admin = (
            await db.execute(select(AdminUser).where(AdminUser.username == username))
        ).scalar_one()
        await auth_service.bump_admin_session_epoch(db, admin)
        await db.commit()

    after = await c.get("/api/v1/auth/me/admin")
    assert after.status_code == 401, after.text
    assert error_of(after)["message"] == "Session invalidated"

    # And a well-formed token for an admin that does not exist is refused too.
    ghost = security.create_session_token(
        security.SubjectType.ADMIN, "00000000-0000-0000-0000-000000000001", epoch=0
    )
    c._c.cookies.set("llp_session", ghost, path="/")
    missing = await c.get("/api/v1/auth/me/admin")
    assert missing.status_code == 401, missing.text


async def test_epoch_grows_monotonically_through_login_logout_cycles(
    session_factory, clean_db, admin_credentials
):
    username, password = admin_credentials
    assert await _epoch(username) == 0

    c = session_factory()
    await c.login(username, password)
    await c.post("/api/v1/auth/logout")
    after_first = await _epoch(username)
    assert after_first == 1, f"logout must raise the epoch by exactly one, got {after_first}"

    # A logout with no live session (cookie already cleared) must not bump again.
    idle = session_factory()
    assert (await idle.post("/api/v1/auth/logout")).status_code == 200
    assert await _epoch(username) == 1

    await c.login(username, password)
    token = security.read_session_token(c.session_cookie(), max_age_seconds=3600)
    assert token["e"] == 1, "a re-login must carry the new epoch, not a stale one"
    assert (await c.get("/api/v1/auth/me/admin")).status_code == 200
    await c.post("/api/v1/auth/logout")
    assert await _epoch(username) == 2


async def test_password_change_raises_the_epoch_by_exactly_one(client, admin_credentials):
    username, password = admin_credentials
    await client.login_admin()
    r = await client.post(
        "/api/v1/admin/password",
        json={"current_password": password, "new_password": "epoch-rotated-pw-1"},
    )
    assert r.status_code == 200, r.text
    assert await _epoch(username) == 1, "one change = one invalidation, no double bump"


async def test_concurrent_invalidations_never_lose_or_reset_the_epoch(db_ready):
    """S9: five simultaneous bumps must land as five, not as one lost update.

    A Python read-modify-write (`epoch = row.epoch + 1`) would let each transaction
    compute the same value from a stale read and leave sessions alive.
    """
    async with SessionLocal() as db:
        admin = AdminUser(username="race-epoch-admin", password_hash=security.hash_secret("pw-12345678"))
        db.add(admin)
        await db.commit()
        admin_id = admin.id
        assert admin.session_epoch == 0

    async def _bump() -> int:
        async with SessionLocal() as db:
            target = await db.get(AdminUser, admin_id)
            return await auth_service.bump_admin_session_epoch(db, target)

    observed = await asyncio.gather(*(_bump() for _ in range(5)))
    final = await _epoch("race-epoch-admin")

    assert final == 5, f"five concurrent invalidations must produce epoch 5, got {final}"
    assert sorted(observed) == [1, 2, 3, 4, 5], (
        f"each bump must see a strictly larger epoch: {observed}"
    )
    assert final >= max(observed), "the stored epoch may never move backwards"


async def test_freshly_bootstrapped_admin_works(db_ready, session_factory, monkeypatch):
    """A brand-new deployment (seed, no prior rows) authenticates with epoch 0."""
    import scripts.seed as seedmod

    monkeypatch.setattr(seedmod.settings, "bootstrap_admin_username", "epoch-admin", raising=True)
    monkeypatch.setattr(seedmod.settings, "bootstrap_admin_password", BOOTSTRAP_PW, raising=True)
    await seedmod.seed()

    assert await _epoch("epoch-admin") == 0
    c = session_factory()
    await c.login("epoch-admin", BOOTSTRAP_PW)
    token = security.read_session_token(c.session_cookie(), max_age_seconds=3600)
    assert token["e"] == 0
    assert (await c.get("/api/v1/auth/me/admin")).status_code == 200

    await c.post("/api/v1/auth/logout")
    assert (await c.get("/api/v1/auth/me/admin")).status_code == 401
