from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Unauthorized
from app.core.rate_limit import enforce_login_rate_limit
from app.models.identity import AdminUser, Student
from app.schemas import auth as auth_schemas
from app.services import auth_service, settings_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/bootstrap", response_model=None)
async def bootstrap(db: AsyncSession = Depends(get_db)) -> dict:
    """Unauthenticated branding + UI config to render the login screen."""
    branding = await settings_service.get_branding_public(db)
    ui = await settings_service.get_ui_config(db)
    return {"branding": branding, "ui": ui}


@router.post("/admin/login", response_model=auth_schemas.SessionInfo)
async def admin_login(
    payload: auth_schemas.AdminLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> auth_schemas.SessionInfo:
    await enforce_login_rate_limit(f"admin:{payload.username}", limit=8, window_seconds=60)
    admin = await auth_service.verify_admin_password(db, payload.username, payload.password)
    if not admin:
        raise Unauthorized("Invalid username or password")
    token, csrf = auth_service.issue_admin_session_token(admin)
    deps.set_session_cookies(response, token, csrf, student=False)
    return auth_schemas.SessionInfo(subject_type="admin", csrf_token=csrf)


@router.post("/admin/recovery/login", response_model=auth_schemas.SessionInfo)
async def admin_recovery_login(
    payload: auth_schemas.RecoveryCodeLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> auth_schemas.SessionInfo:
    await enforce_login_rate_limit(f"recovery:{payload.username}", limit=5, window_seconds=300)
    admin = await auth_service.login_with_recovery_code(db, payload.username, payload.code)
    token, csrf = auth_service.issue_admin_session_token(admin)
    deps.set_session_cookies(response, token, csrf, student=False)
    return auth_schemas.SessionInfo(subject_type="admin", csrf_token=csrf)


@router.post("/student/login", response_model=auth_schemas.SessionInfo)
async def student_login(
    payload: auth_schemas.StudentLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> auth_schemas.SessionInfo:
    await enforce_login_rate_limit(f"student:{payload.access_key[:24]}", limit=10, window_seconds=60)
    student = await auth_service.authenticate_student_access_key(db, payload.access_key)
    if not student:
        raise Unauthorized("Invalid access key")
    _session, token, csrf = await auth_service.create_student_session(db, student)
    deps.set_session_cookies(response, token, csrf, student=True)
    return auth_schemas.SessionInfo(subject_type="student", csrf_token=csrf)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Invalidate server-side first: bump admin epoch / terminate student session,
    # so the old cookie can never be reused even if it is still in a browser.
    await auth_service.logout_current(request, db)
    deps.clear_session_cookies(response)
    return {"ok": True}


@router.get("/me/admin", response_model=None)
async def me_admin(admin: AdminUser = Depends(deps.get_current_admin)) -> dict:
    return {
        "subject_type": "admin",
        "id": str(admin.id),
        "username": admin.username,
        "display_name": admin.display_name,
    }


@router.get("/me/student", response_model=None)
async def me_student(student: Student = Depends(deps.get_current_student)) -> dict:
    return {
        "subject_type": "student",
        "id": str(student.id),
        "name": student.name,
        "surname": student.surname,
        "username": student.username,
        "ui_language": student.ui_language,
    }
