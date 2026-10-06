from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, ValidationFailed
from app.core.security import hash_secret, verify_secret
from app.models.identity import AdminUser
from app.schemas import auth as auth_schemas
from app.services import audit_service, auth_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/recovery-codes", response_model=auth_schemas.RecoveryCodesResponse)
async def regenerate_recovery_codes(
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> auth_schemas.RecoveryCodesResponse:
    """Generate 5 one-time codes. Invalidates the previous set. Shown once."""
    codes = await auth_service.generate_recovery_codes(db, admin)
    return auth_schemas.RecoveryCodesResponse(codes=codes)


@router.post("/password", response_model=auth_schemas.Message)
async def change_password(
    payload: auth_schemas.PasswordChangeRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> auth_schemas.Message:
    if payload.current_password and not verify_secret(admin.password_hash, payload.current_password):
        raise ValidationFailed("Current password is incorrect")
    if payload.new_password == admin.username:
        raise ValidationFailed("Password must differ from username")
    admin.password_hash = hash_secret(payload.new_password)
    await db.flush()
    # Changing the password invalidates all existing admin sessions.
    await auth_service.bump_admin_session_epoch(db, admin)
    await audit_service.record_audit(db, action="admin.password.changed", actor_id=admin.id)
    return auth_schemas.Message(message="Password updated")


@router.patch("/username", response_model=auth_schemas.Message)
async def change_username(
    payload: auth_schemas.UsernameChangeRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> auth_schemas.Message:
    new_username = payload.username.strip()
    if not new_username:
        raise ValidationFailed("Username is required")
    if new_username == admin.username:
        return auth_schemas.Message(message="Username unchanged")
    existing = await auth_service.get_admin_by_username(db, new_username)
    if existing and existing.id != admin.id:
        raise Conflict("username_taken", "That username is already in use")
    old = admin.username
    admin.username = new_username
    await db.flush()
    await audit_service.record_audit(
        db, action="admin.username.changed", actor_id=admin.id, before={"username": old}, after={"username": new_username}
    )
    return auth_schemas.Message(message="Username updated")
