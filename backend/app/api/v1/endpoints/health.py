from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/ping")
async def ping(db: AsyncSession = Depends(get_db)) -> dict:
    """Liveness + datastore reachability (does not require auth)."""
    db_ok = True
    redis_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    try:
        await get_redis().ping()
    except Exception:
        redis_ok = False
    return {"status": "ok" if (db_ok and redis_ok) else "degraded", "database": db_ok, "redis": redis_ok}
