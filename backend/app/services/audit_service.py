from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ops import AuditLog


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    actor_type: str = "admin",
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Persist a security-relevant audit entry (score override, key regen, etc.)."""
    db.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            ip=ip,
            detail=detail,
        )
    )
    await db.flush()


def current_ip() -> str | None:
    from app.core.middleware import request_context

    ctx = request_context.get()
    return ctx.ip
