from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import enums, security
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.identity import (
    AdminUser,
    Group,
    GroupMembership,
    Student,
    StudentAccessKey,
    StudentSession,
)
from app.schemas import student as student_schemas
from app.services import audit_service, auth_service

router = APIRouter(prefix="/students", tags=["students"])

_SUGGEST_STRIP = re.compile(r"[^a-z0-9]+")


def _suggest_username(name: str, surname: str) -> str:
    base = f"{_SUGGEST_STRIP.sub('', name.lower())}{_SUGGEST_STRIP.sub('', surname.lower())}"
    return base or "student"


async def _group_ids(db: AsyncSession, student_id: UUID) -> list[UUID]:
    result = await db.execute(select(GroupMembership.group_id).where(GroupMembership.student_id == student_id))
    return [row[0] for row in result.all()]


async def _active_key_prefix(db: AsyncSession, student_id: UUID) -> str | None:
    result = await db.execute(
        select(StudentAccessKey.key_prefix).where(
            StudentAccessKey.student_id == student_id,
            StudentAccessKey.status == enums.AccessKeyStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none()


def _to_read(student: Student, gids: list[UUID], prefix: str | None) -> student_schemas.StudentRead:
    return student_schemas.StudentRead(
        id=student.id,
        name=student.name,
        surname=student.surname,
        username=student.username,
        status=student.status.value if hasattr(student.status, "value") else student.status,
        ui_language=student.ui_language,
        created_at=student.created_at,
        active_key_prefix=prefix,
        group_ids=gids,
    )


@router.get("/suggest-username", response_model=None)
async def suggest_username(
    name: str = Query(...), surname: str = Query(...), db: AsyncSession = Depends(get_db)
) -> dict:
    base = _suggest_username(name, surname)
    suffix = 0
    while True:
        # `base`, then `base1`, `base2`, ... - no gap in the sequence, and the
        # username column is unique so a free candidate always exists.
        candidate = base if suffix == 0 else f"{base}{suffix}"
        exists = (
            await db.execute(select(Student.id).where(Student.username == candidate))
        ).scalar_one_or_none()
        if not exists:
            return {"username": candidate}
        suffix += 1


@router.get("", response_model=None)
async def list_students(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    status: str | None = None,
    group_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    page_size = min(page_size, 200)
    stmt = select(Student).where(Student.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Student.name.ilike(like)) | (Student.surname.ilike(like)) | (Student.username.ilike(like))
        )
    if status:
        stmt = stmt.where(Student.status == status)
    if group_id:
        stmt = stmt.where(Student.id.in_(select(GroupMembership.student_id).where(GroupMembership.group_id == group_id)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(Student.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    items = [
        _to_read(s, await _group_ids(db, s.id), await _active_key_prefix(db, s.id)) for s in rows
    ]
    return {"items": [i.model_dump(mode="json") for i in items], "total": total, "page": page, "page_size": page_size}


@router.post("", response_model=None, status_code=201)
async def create_student(
    payload: student_schemas.StudentCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    username = payload.username.strip()
    exists = (await db.execute(select(Student.id).where(Student.username == username))).scalar_one_or_none()
    if exists:
        raise Conflict("username_taken", "That username is already in use")
    if payload.group_ids:
        found = (
            await db.execute(select(Group.id).where(Group.id.in_(payload.group_ids)))
        ).scalars().all()
        missing = set(payload.group_ids) - set(found)
        if missing:
            raise ValidationFailed("One or more group_ids do not exist")
    student = Student(name=payload.name.strip(), surname=payload.surname.strip(), username=username)
    db.add(student)
    try:
        await db.flush()
    except IntegrityError:
        # The pre-check above is only an optimisation; `student.username` has a
        # UNIQUE index, so a concurrent create loses here - and must not 500.
        raise Conflict("username_taken", "That username is already in use") from None
    for gid in payload.group_ids:
        db.add(GroupMembership(group_id=gid, student_id=student.id))
    await db.flush()
    access_key = await auth_service.issue_student_access_key(db, student)
    await audit_service.record_audit(
        db, action="student.created", actor_id=admin.id, target_type="student", target_id=student.id
    )
    return {
        "student": _to_read(student, await _group_ids(db, student.id), await _active_key_prefix(db, student.id)).model_dump(mode="json"),
        # shown ONCE; only the hash is stored afterwards
        "access_key": access_key,
    }


@router.get("/{student_id}", response_model=None)
async def get_student(
    student_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    student = await db.get(Student, student_id)
    if not student or student.deleted_at is not None:
        raise NotFound("Student not found")
    return _to_read(student, await _group_ids(db, student.id), await _active_key_prefix(db, student.id)).model_dump(mode="json")


@router.patch("/{student_id}", response_model=None)
async def update_student(
    student_id: UUID,
    payload: student_schemas.StudentUpdate,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    student = await db.get(Student, student_id)
    if not student:
        raise NotFound("Student not found")
    data = payload.model_dump(exclude_unset=True)
    if "username" in data and data["username"]:
        clash = (
            await db.execute(select(Student.id).where(Student.username == data["username"], Student.id != student_id))
        ).scalar_one_or_none()
        if clash:
            raise Conflict("username_taken", "That username is already in use")
    for k, v in data.items():
        setattr(student, k, v)
    await db.flush()
    return _to_read(student, await _group_ids(db, student.id), await _active_key_prefix(db, student.id)).model_dump(mode="json")


@router.post("/{student_id}/status", response_model=None)
async def set_status(
    student_id: UUID,
    payload: student_schemas.StudentStatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    student = await db.get(Student, student_id)
    if not student:
        raise NotFound("Student not found")
    try:
        new_status = enums.StudentStatus(payload.status)
    except ValueError:
        raise ValidationFailed("Invalid status")
    student.status = new_status
    if new_status == enums.StudentStatus.ARCHIVED:
        student.archived_at = security.utcnow()
        # archiving is a soft delete (recycle bin), never a hard delete
        student.deleted_at = security.utcnow()
    elif new_status == enums.StudentStatus.ACTIVE:
        # Reactivating must clear the soft-delete markers so the student is
        # visible/queryable again; leaving deleted_at set would keep them hidden.
        student.deleted_at = None
        student.archived_at = None
    # A student who is no longer ACTIVE must not keep live sessions.
    if new_status != enums.StudentStatus.ACTIVE:
        await auth_service.terminate_student_sessions(db, student.id)
    await db.flush()
    await audit_service.record_audit(
        db, action="student.status.changed", actor_id=admin.id, target_type="student", target_id=student_id, after={"status": payload.status}
    )
    return {"ok": True, "status": payload.status}


@router.post("/{student_id}/access-key/rotate", response_model=None)
async def rotate_access_key(
    student_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    student = await db.get(Student, student_id)
    if not student:
        raise NotFound("Student not found")
    try:
        access_key = await auth_service.issue_student_access_key(db, student)
    except IntegrityError:
        # `uq_student_access_key_active` lost a race against a concurrent
        # rotation: the transaction is rolled back, so no second ACTIVE key exists.
        raise Conflict(
            "rotation_in_progress", "Another key rotation just won; retry the request"
        ) from None
    await auth_service.terminate_student_sessions(db, student_id)
    await audit_service.record_audit(
        db, action="student.access_key.rotated", actor_id=admin.id, target_type="student", target_id=student_id
    )
    return {"access_key": access_key}


@router.post("/{student_id}/access-key/revoke", response_model=None)
async def revoke_access_key(
    student_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await db.get(Student, student_id):
        raise NotFound("Student not found")
    revoked = await auth_service.revoke_student_access_keys(db, student_id)
    # Revoking the key should also end live sessions so access is cut immediately.
    await auth_service.terminate_student_sessions(db, student_id)
    await audit_service.record_audit(
        db,
        action="student.access_key.revoked",
        actor_id=admin.id,
        target_type="student",
        target_id=student_id,
        after={"revoked_keys": revoked},
    )
    return {"ok": True, "revoked_keys": revoked}


@router.get("/{student_id}/sessions", response_model=None)
async def list_sessions(
    student_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(StudentSession)
        .where(StudentSession.student_id == student_id, StudentSession.terminated_at.is_(None))
        .order_by(StudentSession.last_seen_at.desc())
    )
    sessions = result.scalars().all()
    return {
        "items": [
            {
                "session_id": s.session_id,
                "ip": s.ip,
                "user_agent": s.user_agent,
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                "current_location": s.current_location,
            }
            for s in sessions
        ]
    }


@router.post("/{student_id}/sessions/terminate", response_model=None)
async def terminate_sessions(
    student_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    count = await auth_service.terminate_student_sessions(db, student_id)
    await audit_service.record_audit(
        db, action="student.sessions.terminated", actor_id=admin.id, target_type="student", target_id=student_id, after={"count": count}
    )
    return {"ok": True, "terminated": count}
