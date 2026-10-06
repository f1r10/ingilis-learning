"""Fixed-window login rate limiting backed by Redis (brute-force protection)."""
from __future__ import annotations

from app.core.exceptions import APIError
from app.core.redis_client import get_redis

_http_429 = 429


async def enforce_login_rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    redis = get_redis()
    redis_key = f"rl:login:{key}"
    try:
        count = await redis.incr(redis_key)
        if count == 1:
            await redis.expire(redis_key, window_seconds)
    except Exception:
        # If Redis is unavailable, fail open for availability but log elsewhere.
        return
    if count > limit:
        raise APIError(
            "too_many_requests",
            "Too many login attempts. Please wait before trying again.",
            _http_429,
        )
