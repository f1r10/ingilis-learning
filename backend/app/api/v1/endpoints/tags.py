"""Tag API: free-form labels that sit alongside the topic hierarchy.

Tags are flat and optional. A tag is only deletable while nothing references it, so
removing one can never quietly rewrite the history of a question that still carries
it.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.content import QuestionTag, Tag
from app.models.identity import AdminUser
from app.schemas import question as q_schemas
from app.services import audit_service

router = APIRouter(prefix="/tags", tags=["tags"])


async def _counts(db: AsyncSession, tag_ids: list[UUID]) -> dict[UUID, int]:
    if not tag_ids:
        return {}
    rows = (
        await db.execute(
            select(QuestionTag.tag_id, func.count()).where(QuestionTag.tag_id.in_(tag_ids)).group_by(QuestionTag.tag_id)
        )
    ).all()
    return {tag_id: int(count) for tag_id, count in rows}


def _read(tag: Tag, counts: dict[UUID, int]) -> dict:
    return q_schemas.TagRead(
        id=tag.id, name=tag.name, color=tag.color, question_count=counts.get(tag.id, 0)
    ).model_dump(mode="json")


@router.get("", response_model=None)
async def list_tags(
    _admin: AdminUser = Depends(deps.get_current_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    tags = (await db.execute(select(Tag).order_by(func.lower(Tag.name)))).scalars().all()
    counts = await _counts(db, [tag.id for tag in tags])
    return {"items": [_read(tag, counts) for tag in tags], "total": len(tags)}


@router.post("", response_model=None, status_code=201)
async def create_tag(
    payload: q_schemas.TagCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise ValidationFailed("a tag needs a name")
    duplicate = (
        await db.execute(select(Tag).where(func.lower(Tag.name) == name.lower()))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise Conflict("tag_exists", f"A tag named '{duplicate.name}' already exists")

    tag = Tag(name=name, color=payload.color)
    db.add(tag)
    try:
        await db.flush()
    except IntegrityError:
        # The unique index settled a race between two identical requests.
        await db.rollback()
        raise Conflict("tag_exists", f"A tag named '{name}' already exists") from None
    await audit_service.record_audit(
        db, action="tag.created", actor_id=admin.id, target_type="tag", target_id=tag.id, after={"name": name}
    )
    return _read(tag, {})


@router.patch("/{tag_id}", response_model=None)
async def update_tag(
    tag_id: UUID,
    payload: q_schemas.TagUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise NotFound("Tag not found")
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise ValidationFailed("a tag needs a name")
        clash = (
            await db.execute(
                select(Tag).where(func.lower(Tag.name) == name.lower(), Tag.id != tag.id)
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict("tag_exists", f"A tag named '{clash.name}' already exists")
        tag.name = name
    if "color" in changes:
        tag.color = changes["color"]

    await db.flush()
    await audit_service.record_audit(
        db,
        action="tag.updated",
        actor_id=admin.id,
        target_type="tag",
        target_id=tag.id,
        after={"fields": sorted(changes)},
    )
    counts = await _counts(db, [tag.id])
    return _read(tag, counts)


@router.delete("/{tag_id}", response_model=None)
async def delete_tag(
    tag_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise NotFound("Tag not found")
    counts = await _counts(db, [tag_id])
    if counts.get(tag_id, 0):
        raise Conflict("tag_in_use", f"'{tag.name}' still labels {counts[tag_id]} question(s)")

    name = tag.name
    await db.delete(tag)
    await db.flush()
    await audit_service.record_audit(
        db, action="tag.deleted", actor_id=admin.id, target_type="tag", target_id=tag_id, before={"name": name}
    )
    return {"ok": True, "deleted": str(tag_id)}
