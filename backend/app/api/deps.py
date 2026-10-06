"""FastAPI auth dependencies: resolve current admin or student from session cookie."""
from __future__ import annotations

import uuid

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums, security
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import Forbidden, Unauthorized
from app.models.identity import AdminUser, Student, StudentSession

_settings = get_settings()


async def _active_student_session(
    request: Request, db: AsyncSession
) -> tuple[StudentSession, Student]:
    token = request.cookies.get(constants.SESSION_COOKIE)
    if not token:
        raise Unauthorized("Missing session")
    payload = security.read_session_token(
        token, max_age_seconds=_settings.student_session_ttl_hours * 3600
    )
    if not payload or payload.get("t") != security.SubjectType.STUDENT.value:
        raise Unauthorized("Invalid session")

    result = await db.execute(
        select(StudentSession).where(StudentSession.session_id == payload["sid"])
    )
    session = result.scalar_one_or_none()
    if not session or session.terminated_at is not None:
        raise Unauthorized("Session terminated")
    if session.expires_at < security.utcnow():
        raise Unauthorized("Session expired")

    student = await db.get(Student, session.student_id)
    if not student or student.deleted_at is not None:
        raise Unauthorized("Student no longer exists")
    return session, student


async def get_current_student(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Student:
    _, student = await _active_student_session(request, db)
    if student.status != enums.StudentStatus.ACTIVE:
        raise Forbidden("Student account is not active")
    return student


async def get_current_student_session(
    request: Request, db: AsyncSession = Depends(get_db)
) -> StudentSession:
    session, _student = await _active_student_session(request, db)
    return session


async def get_current_admin(
    request: Request, db: AsyncSession = Depends(get_db)
) -> AdminUser:
    token = request.cookies.get(constants.SESSION_COOKIE)
    if not token:
        raise Unauthorized("Missing session")
    payload = security.read_session_token(
        token, max_age_seconds=_settings.admin_session_ttl_hours * 3600
    )
    if not payload or payload.get("t") != security.SubjectType.ADMIN.value:
        raise Unauthorized("Invalid session")
    admin = await db.get(AdminUser, uuid.UUID(payload["sid"]))
    if not admin:
        raise Unauthorized("Admin session no longer valid")
    # Bumping session_epoch (logout / password change) invalidates old admin cookies.
    if payload.get("e") != admin.session_epoch:
        raise Unauthorized("Session invalidated")
    return admin


def set_session_cookies(
    response: Response, session_token: str, csrf_token: str, *, student: bool
) -> None:
    max_age = (
        _settings.student_session_ttl_hours
        if student
        else _settings.admin_session_ttl_hours
    ) * 3600
    common = dict(
        httponly=True,
        secure=_settings.cookie_secure,
        samesite="lax",
        domain=_settings.cookie_domain,
        max_age=max_age,
        path="/",
    )
    response.set_cookie(constants.SESSION_COOKIE, session_token, **common)
    # CSRF cookie is readable by JS (double-submit); it is NOT httponly on purpose.
    response.set_cookie(constants.CSRF_COOKIE, csrf_token, httponly=False, secure=_settings.cookie_secure, samesite="lax", domain=_settings.cookie_domain, max_age=max_age, path="/")


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(constants.SESSION_COOKIE, domain=_settings.cookie_domain, path="/")
    response.delete_cookie(constants.CSRF_COOKIE, domain=_settings.cookie_domain, path="/")
