"""Login rate limiting against real Redis (spec S7) - with the production limits.

Nothing here lowers a threshold to make a test pass: the endpoint is hammered with
the same 8/60s and 10/60s policies the deployment uses, and recovery is proven by
the mechanism the code actually relies on (a fixed window that expires), not by
disabling the guard.
"""
from __future__ import annotations

import asyncio

import pytest
from helpers import error_of

from app.core.exceptions import APIError
from app.core.rate_limit import enforce_login_rate_limit
from app.core.redis_client import get_redis

ADMIN_LIMIT = 8  # app/api/v1/endpoints/auth.py: admin login
STUDENT_LIMIT = 10  # app/api/v1/endpoints/auth.py: student login


async def _login(c, username: str, password: str) -> int:
    r = await c._c.post("/api/v1/auth/admin/login", json={"username": username, "password": password})
    return r.status_code


async def test_repeated_invalid_admin_logins_hit_the_published_ceiling(
    session_factory, clean_db, redis_ready
):
    c = session_factory()
    codes = [await _login(c, "rate-limit-target", "wrong-pass-1") for _ in range(ADMIN_LIMIT)]
    assert codes == [401] * ADMIN_LIMIT, (
        f"the first {ADMIN_LIMIT} attempts must all be ordinary auth failures: {codes}"
    )

    tripped = [await _login(c, "rate-limit-target", "wrong-pass-1") for _ in range(2)]
    assert tripped == [429, 429], f"attempt {ADMIN_LIMIT + 1} onward must be limited: {tripped}"

    blocked = await c._c.post(
        "/api/v1/auth/admin/login", json={"username": "rate-limit-target", "password": "x2"}
    )
    assert error_of(blocked)["code"] == "too_many_requests"


async def test_the_ceiling_is_per_username_so_it_does_not_lock_out_everyone(
    session_factory, clean_db, admin_credentials, redis_ready
):
    username, password = admin_credentials
    c = session_factory()
    for _ in range(ADMIN_LIMIT + 2):
        await _login(c, "another-rate-limit-target", "wrong-pass-1")

    # The real admin is a different key, so legitimate access is unaffected.
    assert await _login(c, username, password) == 200


async def test_a_tripped_window_carries_a_ttl_and_success_returns_after_it(
    session_factory, clean_db, redis_ready
):
    """The block is temporary by construction: Redis holds a live expiry."""
    c = session_factory()
    name = "expiring-window-target"
    for _ in range(ADMIN_LIMIT + 1):
        await _login(c, name, "wrong-pass-1")

    redis = get_redis()
    ttl = await redis.ttl(f"rl:login:admin:{name}")
    assert 0 < ttl <= 60, f"the limiter must schedule an expiry inside its window, got {ttl}"

    # Same mechanism, short window, dedicated key: proves a window really does
    # reset and access returns. The endpoint's 8/60s policy is not modified.
    probe = "short-window-probe"

    async def _attempt() -> int:
        try:
            await enforce_login_rate_limit(probe, limit=2, window_seconds=1)
        except APIError as exc:
            return exc.http_status
        return 200

    assert [await _attempt() for _ in range(3)] == [200, 200, 429]
    await asyncio.sleep(1.2)
    assert await _attempt() == 200, "the fixed window must reset and let traffic through"
    assert await redis.ttl(f"rl:login:{probe}") > 0, "the new window must also self-expire"


async def test_student_login_is_limited_on_the_same_contract(session_factory, clean_db, redis_ready):
    c = session_factory()
    ghost_key = "ghost@00000000000000000000000000000000"
    codes = []
    for _ in range(STUDENT_LIMIT):
        r = await c._c.post("/api/v1/auth/student/login", json={"access_key": ghost_key})
        codes.append(r.status_code)
    assert codes == [401] * STUDENT_LIMIT, codes

    over = await c._c.post("/api/v1/auth/student/login", json={"access_key": ghost_key})
    assert over.status_code == 429, over.text
    assert error_of(over)["code"] == "too_many_requests"


async def test_recovery_login_window_is_tighter_than_password_login(
    session_factory, clean_db, admin_credentials, redis_ready
):
    """The recovery endpoint is separately limited (5/300s) - verified, not assumed."""
    username, _password = admin_credentials
    c = session_factory()

    statuses = [
        (
            await c._c.post(
                "/api/v1/auth/admin/recovery/login",
                json={"username": username, "code": f"DEAD-BEEF-{i:04d}"},
            )
        ).status_code
        for i in range(5)
    ]
    assert statuses == [401] * 5, statuses

    sixth = await c._c.post(
        "/api/v1/auth/admin/recovery/login",
        json={"username": username, "code": "DEAD-BEEF-9999"},
    )
    assert sixth.status_code == 429, sixth.text
    assert error_of(sixth)["code"] == "too_many_requests"

    ttl = await get_redis().ttl(f"rl:login:recovery:{username}")
    assert 0 < ttl <= 300, ttl


@pytest.mark.parametrize("bad_input", [None, ""])
async def test_blank_credentials_fail_validation_without_consuming_a_slot(
    session_factory, clean_db, bad_input, redis_ready
):
    """Schema validation runs before the limiter, so a malformed body costs no attempt."""
    c = session_factory()
    r = await c._c.post("/api/v1/auth/admin/login", json={"username": bad_input, "password": "x"})
    assert r.status_code == 422, r.text

    redis = get_redis()
    leftover = [k async for k in redis.scan_iter(match="rl:login:*", count=100)]
    assert leftover == [], f"a rejected body must not create a limiter window: {leftover}"
