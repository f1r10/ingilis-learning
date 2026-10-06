from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.identity import AdminUser, Group, GroupMembership, Student
from app.schemas import student as student_schemas
from app.services import audit_service

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_model=None)
async def list_groups(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    groups = (
        await db.execute(select(Group).where(Group.deleted_at.is_(None)).order_by(Group.name))
    ).scalars().all()
    items = []
    for g in groups:
        count = (
            await db.execute(
                select(func.count()).select_from(GroupMembership).where(GroupMembership.group_id == g.id)
            )
        ).scalar_one()
        items.append(
            student_schemas.GroupRead(
                id=g.id, name=g.name, description=g.description, color=g.color, member_count=count
            ).model_dump(mode="json")
        )
    return {"items": items}


@router.post("", response_model=None, status_code=201)
async def create_group(
    payload: student_schemas.GroupCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = Group(name=payload.name, description=payload.description, color=payload.color)
    db.add(group)
    await db.flush()
    await audit_service.record_audit(db, action="group.created", actor_id=admin.id, target_type="group", target_id=group.id)
    return student_schemas.GroupRead(id=group.id, name=group.name, description=group.description, color=group.color).model_dump(mode="json")


@router.patch("/{group_id}", response_model=None)
async def update_group(
    group_id: UUID,
    payload: student_schemas.GroupUpdate,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = await db.get(Group, group_id)
    if not group:
        raise NotFound("Group not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(group, k, v)
    await db.flush()
    return student_schemas.GroupRead(id=group.id, name=group.name, description=group.description, color=group.color).model_dump(mode="json")


@router.delete("/{group_id}", response_model=None)
async def archive_group(
    group_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = await db.get(Group, group_id)
    if not group:
        raise NotFound("Group not found")
    from app.core.security import utcnow

    group.deleted_at = utcnow()
    await db.flush()
    await audit_service.record_audit(db, action="group.archived", actor_id=admin.id, target_type="group", target_id=group_id)
    return {"ok": True}


async def _get_group(db: AsyncSession, group_id: UUID) -> Group:
    group = await db.get(Group, group_id)
    if not group or group.deleted_at is not None:
        raise NotFound("Group not found")
    return group


@router.get("/{group_id}/members", response_model=None)
async def list_members(
    group_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_group(db, group_id)
    rows = (
        await db.execute(
            select(Student)
            .join(GroupMembership, GroupMembership.student_id == Student.id)
            .where(GroupMembership.group_id == group_id, Student.deleted_at.is_(None))
            .order_by(Student.surname, Student.name)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": str(s.id),
                "name": s.name,
                "surname": s.surname,
                "username": s.username,
                "status": s.status.value if hasattr(s.status, "value") else s.status,
            }
            for s in rows
        ]
    }


@router.post("/{group_id}/members", response_model=None, status_code=201)
async def add_member(
    group_id: UUID,
    payload: student_schemas.GroupMemberAdd,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_group(db, group_id)
    student = await db.get(Student, payload.student_id)
    if not student or student.deleted_at is not None:
        raise ValidationFailed("Student not found")
    existing = (
        await db.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.student_id == payload.student_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise Conflict("already_member", "Student is already in this group")
    db.add(GroupMembership(group_id=group_id, student_id=payload.student_id))
    try:
        await db.flush()
    except IntegrityError:
        # uq_group_student is the real guard against double membership: two
        # concurrent adds must not both create a row.
        raise Conflict("already_member", "Student is already in this group") from None
    await audit_service.record_audit(
        db, action="group.member.added", actor_id=admin.id, target_type="group", target_id=group_id,
        after={"student_id": str(payload.student_id)},
    )
    return {"ok": True}


@router.delete("/{group_id}/members/{student_id}", response_model=None)
async def remove_member(
    group_id: UUID,
    student_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_group(db, group_id)
    membership = (
        await db.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.student_id == student_id,
            )
        )
    ).scalar_one_or_none()
    if not membership:
        raise NotFound("Membership not found")
    await db.delete(membership)
    await db.flush()
    await audit_service.record_audit(
        db, action="group.member.removed", actor_id=admin.id, target_type="group", target_id=group_id,
        before={"student_id": str(student_id)},
    )
    return {"ok": True}
