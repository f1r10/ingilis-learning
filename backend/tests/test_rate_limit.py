"""Fixed-window login rate limiter: exercised with a fake Redis so no server is
needed. Verifies the limit trips AND that it fails OPEN (never locks users out)
when Redis is unreachable."""
from __future__ import annotations

import pytest

from app.core import rate_limit
from app.core.exceptions import APIError


class _FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expired: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expired[key] = seconds


class _BrokenRedis:
    async def incr(self, key: str) -> int:
        raise ConnectionError("redis down")

    async def expire(self, key: str, seconds: int) -> None:
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_limit_trips_after_window(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)

    # First `limit` attempts allowed, the next blocked.
    for _ in range(3):
        await rate_limit.enforce_login_rate_limit("admin:x", limit=3, window_seconds=60)
    with pytest.raises(APIError) as ei:
        await rate_limit.enforce_login_rate_limit("admin:x", limit=3, window_seconds=60)
    assert ei.value.http_status == 429
    assert ei.value.code == "too_many_requests"
    # Window was set on the first hit.
    assert fake.expired["rl:login:admin:x"] == 60


@pytest.mark.asyncio
async def test_fail_open_when_redis_down(monkeypatch):
    monkeypatch.setattr(rate_limit, "get_redis", lambda: _BrokenRedis())
    # Must NOT raise: availability wins over strictness when Redis is unreachable.
    await rate_limit.enforce_login_rate_limit("admin:y", limit=1, window_seconds=60)
    await rate_limit.enforce_login_rate_limit("admin:y", limit=1, window_seconds=60)
